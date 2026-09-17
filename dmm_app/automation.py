"""Validated timed sequences. Instrument I/O shares the acquisition SCPI sessions."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import re
import threading
import time

from dmm_app.poller import parse_primary_value

MODELS = {"owon_spe6103", "keithley_2281s"}
# Command, absolute device range (additional per-run limits are mandatory), tolerance.
ACTIONS = {
    "owon_spe6103": {
        "voltage": ("VOLTage", (0, 60), .005),
        "current_limit": ("CURRent", (0, 10), .002),
        "output": ("OUTPut", None, 0),
    },
    "keithley_2281s": {
        "voc": (":BATTery:SIMulator:VOC", (0, 20), .005),
        "soc": (":BATTery:SIMulator:SOC", (0, 100), .05),
        "current_limit": (":BATTery:SIMulator:CURRent:LIMit", (0, 6.1), .002),
        "current_protection": (":BATTery:SIMulator:CURRent:PROTection", (.1, 6.1), .002),
        "voltage_protection": (":BATTery:SIMulator:TVOLtage:PROTection", (.5, 21), .005),
        "method": (":BATTery:SIMulator:METHod", None, 0),
        "output": (":BATTery:OUTPut", None, 0),
    },
}


def number(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label} must be between {low} and {high}")
    return value


def keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys():
        raise ValueError(f"Expected object with {', '.join(required)}")
    extra = value.keys() - set(required) - set(optional)
    if extra:
        raise ValueError(f"Unknown fields: {', '.join(sorted(extra))}")


def validate_sequence(document):
    keys(document, ("version", "name", "notes", "instruments", "steps", "on_complete", "on_abort"), ("required_observers",))
    if type(document['version']) is not int or document['version'] != 1:
        raise ValueError("Unsupported sequence version; expected 1")
    for field in ('name', 'notes'):
        if not isinstance(document[field], str) or not document[field].strip():
            raise ValueError(f"{field} must be nonempty text")
    observers = document.get('required_observers', [])
    if not isinstance(observers, list) or any(x != 'gsmiv_power' for x in observers):
        raise ValueError('Unsupported required observer')
    roles = document['instruments']
    if not isinstance(roles, dict) or not 1 <= len(roles) <= 16:
        raise ValueError("Define 1–16 instrument roles")
    for role, config in roles.items():
        if not isinstance(role, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', role):
            raise ValueError("Role names must use lowercase letters, digits and underscores")
        keys(config, ('model', 'limits'))
        model = config['model']
        if not isinstance(model, str) or model not in MODELS or not isinstance(config['limits'], dict):
            raise ValueError(f"Unsupported instrument or limits for {role}")
        for action, bounds in config['limits'].items():
            spec = ACTIONS[model].get(action)
            if not spec or spec[1] is None or not isinstance(bounds, list) or len(bounds) != 2:
                raise ValueError(f"Invalid bounds for {role}.{action}")
            lo, hi = spec[1]
            number(bounds[0], action, lo, hi)
            number(bounds[1], action, bounds[0], hi)

    def validate_action(step, cleanup=False):
        keys(step, ('instrument', 'action', 'value'))
        role, action, value = step['instrument'], step['action'], step['value']
        if not isinstance(role, str) or role not in roles or not isinstance(action, str):
            raise ValueError("Unknown instrument role or action")
        if action == 'hold':
            if value is not None:
                raise ValueError("Hold has a null value")
            return
        spec = ACTIONS[roles[role]['model']].get(action)
        if spec is None:
            raise ValueError(f"Unsupported action {role}.{action}")
        if cleanup and (action != 'output' or value is not False):
            raise ValueError("End/abort actions must be hold or output=false")
        if action == 'output':
            if type(value) is not bool:
                raise ValueError("Output must be true or false")
        elif action == 'method':
            if value not in ('static', 'dynamic'):
                raise ValueError("Method must be static or dynamic")
        else:
            bounds = roles[role]['limits'].get(action)
            if bounds is None:
                raise ValueError(f"Define allowed limits for {role}.{action}")
            number(value, f'{role}.{action}', *bounds)

    steps = document['steps']
    if not isinstance(steps, list) or not 1 <= len(steps) <= 2000:
        raise ValueError("Provide 1–2000 steps")
    configured = {role: set() for role in roles}
    powered = {role: False for role in roles}
    for step in steps:
        keys(step, ('label', 'instrument', 'action', 'value', 'settle_s', 'capture_s'))
        if not isinstance(step['label'], str) or not step['label'].strip():
            raise ValueError("Each step needs a label")
        validate_action({key: step[key] for key in ('instrument', 'action', 'value')})
        role, action = step['instrument'], step['action']
        model = roles[role]['model']
        if action == 'output' and step['value']:
            required = {'voltage', 'current_limit'} if model == 'owon_spe6103' else {'voc', 'current_limit'}
            if not required <= configured[role]:
                raise ValueError(f'Set {sorted(required)} before enabling {role}')
        if model == 'keithley_2281s' and powered[role] and action in (
            'current_limit', 'current_protection', 'voltage_protection', 'method'
        ):
            raise ValueError(f'Turn {role} off before changing {action}')
        configured[role].add(action)
        if action == 'output':
            powered[role] = step['value']
        for field in ('settle_s', 'capture_s'):
            number(step[field], field, 0, 86400)
    for field in ('on_complete', 'on_abort'):
        actions = document[field]
        if not isinstance(actions, list):
            raise ValueError(f"{field} must be a list")
        for action in actions:
            validate_action(action, cleanup=True)
        if sorted(a['instrument'] for a in actions) != sorted(roles):
            raise ValueError(f"{field} must specify each instrument exactly once")
    return copy.deepcopy(document)


def load_sequence(path):
    if Path(path).stat().st_size > 2_000_000:
        raise ValueError("Sequence file is too large")
    return validate_sequence(json.loads(Path(path).read_text(encoding='utf-8')))


class InstrumentController:
    def __init__(self, model, scpi):
        self.model = model
        self.scpi = scpi
        self.identity = ''

    def query_number(self, command):
        response = self.scpi.query(command)
        value = parse_primary_value(response)
        if value is None:
            raise ValueError(f"Invalid response to {command}: {response!r}")
        return value

    def output_state(self):
        raw = self.scpi.query(ACTIONS[self.model]['output'][0] + '?').strip().upper()
        states = {'0': False, 'OFF': False, '1': True, 'ON': True, '2': False, 'DIS': False}
        if raw not in states:
            raise ValueError(f"Unknown output state: {raw!r}")
        return states[raw]

    def preflight(self, steps):
        with self.scpi.transaction():
            self.identity = self.scpi.query('*IDN?')
            expected = 'SPE6103' if self.model == 'owon_spe6103' else '2281S-20-6'
            if expected not in self.identity.upper():
                raise ValueError(f"Expected {expected}; received {self.identity}")
            if self.output_state():
                raise ValueError(f"{expected}: turn output off before starting a sequence")
            snapshot = {'identity': self.identity, 'output': False}
            if self.model == 'keithley_2281s':
                mode = self.scpi.query(':ENTRy:FUNCtion?').strip().strip('"').upper()
                if mode not in ('SIM', 'SIMULATOR'):
                    raise ValueError("Select battery simulator mode on the 2281S first")
                lo = self.query_number(':BATTery:SIMulator:VOC? MINimum')
                hi = self.query_number(':BATTery:SIMulator:VOC? MAXimum')
                snapshot.update(voc_min=lo, voc_max=hi)
                for step in steps:
                    if step['action'] == 'voc' and not lo <= step['value'] <= hi:
                        raise ValueError(f"VOC {step['value']} is outside selected model range {lo}–{hi} V")
                self.check_errors()
            return snapshot

    def check_errors(self):
        # OWON's published command set has no SYST:ERR?; use setting read-back.
        if self.model == 'keithley_2281s':
            response = self.scpi.query(':SYSTem:ERRor?')
            try:
                code = int(response.split(',')[0])
            except ValueError as exc:
                raise ValueError(f"Invalid error response: {response}") from exc
            if code:
                raise ValueError(f"2281S error: {response}")

    def apply(self, action, value):
        if action == 'hold':
            return {'action': 'hold', 'readback': 'unchanged; no command sent'}
        command, _, tolerance = ACTIONS[self.model][action]
        with self.scpi.transaction():
            if self.model == 'keithley_2281s' and action in (
                'current_limit', 'current_protection', 'voltage_protection', 'method'
            ) and self.output_state():
                raise ValueError(f"Turn 2281S output off before changing {action}")
            if self.model == 'owon_spe6103':
                self.scpi.write('SYSTem:REMote')
            parameter = ('ON' if value else 'OFF') if action == 'output' else str(value)
            self.scpi.write(f'{command} {parameter}')
            # Output-off must still be attempted if the instrument has a queued error.
            self.check_errors()
            if action == 'output':
                actual = self.output_state()
                matches = actual == value
            elif action == 'method':
                actual = self.scpi.query(command + '?').strip().strip('"').lower()
                matches = actual in (value, value[:4] if value == 'static' else 'dyn')
            else:
                actual = self.query_number(command + '?')
                matches = math.isclose(actual, value, rel_tol=0, abs_tol=tolerance)
            if not matches:
                raise ValueError(f'{command}: requested {value}, read back {actual}')
            return {'command': f'{command} {parameter}', 'readback': actual}


class SequenceAborted(Exception):
    pass


class SequenceRunner(threading.Thread):
    """Sequential completion-relative dwells; never catch up by skipping dwell time."""
    def __init__(self, document, controllers, clock, event_path, on_event,
                 ready_events=(), ready_timeout_seconds=75.0):
        super().__init__(daemon=True)
        self.document = validate_sequence(document)
        if set(controllers) != set(self.document['instruments']):
            raise ValueError('Assign every instrument role')
        for role, controller in controllers.items():
            if controller.model != self.document['instruments'][role]['model']:
                raise ValueError(f'Wrong instrument model for {role}')
        self.ready_events = tuple(ready_events)
        self.ready_timeout_seconds = number(ready_timeout_seconds, "Ready timeout", .01, 3600)
        self.controllers = controllers
        self.clock = clock
        self.event_path = Path(event_path)
        self.on_event = on_event
        self._condition = threading.Condition()
        self._emit_lock = threading.RLock()
        self._aborted = False
        self._paused = False
        self._reason = 'Operator aborted'
        self._file = None
        self._log_failed = False
        self.result = None

    def abort(self, reason='Operator aborted'):
        with self._condition:
            self._aborted = True
            self._reason = reason
            self._condition.notify_all()

    def pause(self, paused):
        with self._condition:
            self._paused = paused
            self._condition.notify_all()
        self.emit('pause' if paused else 'resume')

    def emit(self, event, **data):
        with self._emit_lock:
            self._emit(event, **data)

    def _emit(self, event, **data):
        timestamp, elapsed, run = self.clock.capture()
        record = dict(timestamp=timestamp.isoformat(timespec='microseconds'),
                      elapsed_seconds=elapsed, acquisition_run=run, event=event, **data)
        if event != 'progress' and self._file and not self._log_failed:
            try:
                self._file.write(json.dumps(record, allow_nan=False) + '\n')
                self._file.flush()
            except OSError as exc:
                self._log_failed = True
                self.abort(f'Event logging failed: {exc}')
                record['logging_error'] = str(exc)
        self.on_event(record)

    def checkpoint(self):
        with self._condition:
            while self._paused and not self._aborted:
                self._condition.wait(.1)
            if self._aborted:
                raise SequenceAborted(self._reason)

    def dwell(self, phase, seconds, index):
        remaining = float(seconds)
        self.emit(phase, step=index, duration_s=seconds)
        while remaining > 0:
            self.checkpoint()
            with self._condition:
                if self._paused:
                    continue
                before = time.monotonic()
                self._condition.wait(min(.1, remaining))
                remaining = max(0, remaining - (time.monotonic() - before))
            self.emit('progress', step=index, phase=phase, remaining_s=remaining)
        self.checkpoint()

    def run(self):
        armed = False
        outcome, detail = 'complete', ''
        cleanup_errors = []
        try:
            self._file = self.event_path.open('x', encoding='utf-8')
            self.emit('run_start', name=self.document['name'])
            if self.ready_events:
                self.emit('acquisition_wait', detail='Waiting for first post-settling DMM reading')
                deadline = time.monotonic() + self.ready_timeout_seconds
                while not all(event.is_set() for event in self.ready_events):
                    self.checkpoint()
                    if time.monotonic() >= deadline:
                        raise TimeoutError('No post-settling DMM reading before acquisition timeout')
                    with self._condition:
                        self._condition.wait(.05)
                self.emit('acquisition_ready')
            for role, controller in self.controllers.items():
                self.checkpoint()
                snapshot = controller.preflight([s for s in self.document['steps'] if s['instrument'] == role])
                self.emit('preflight', instrument=role, **snapshot)
            self.checkpoint()
            armed = True
            for index, step in enumerate(self.document['steps'], 1):
                self.checkpoint()
                self.emit('command_start', step=index, **step)
                self.checkpoint()
                result = self.controllers[step['instrument']].apply(step['action'], step['value'])
                self.emit('command_complete', step=index, instrument=step['instrument'], **result)
                self.dwell('settle', step['settle_s'], index)
                self.dwell('capture', step['capture_s'], index)
        except SequenceAborted as exc:
            outcome, detail = 'aborted', str(exc)
        except Exception as exc:
            outcome, detail = 'failed', str(exc)
        finally:
            if armed:
                if self._aborted and outcome == 'complete':
                    outcome, detail = 'aborted', self._reason
                field = 'on_complete' if outcome == 'complete' else 'on_abort'
                def cleanup(actions):
                    for action in actions:
                        try:
                            self.emit('cleanup_start', **action)
                            result = self.controllers[action['instrument']].apply(action['action'], action['value'])
                            self.emit('cleanup_complete', instrument=action['instrument'], **result)
                        except Exception as exc:
                            cleanup_errors.append(f"{action['instrument']}: {exc}")
                            self.emit('cleanup_error', instrument=action['instrument'], error=str(exc))
                cleanup(self.document[field])
                if field == 'on_complete' and (cleanup_errors or self._log_failed or self._aborted):
                    cleanup(self.document['on_abort'])
                    outcome, detail = 'failed', 'Completion interrupted; abort actions attempted'
            if cleanup_errors or self._log_failed:
                outcome = 'failed'
            self.result = outcome
            self.emit('run_end', outcome=outcome, detail=detail, cleanup_errors=cleanup_errors,
                      cleanup_attempted=armed)
            if self._file:
                self._file.close()
