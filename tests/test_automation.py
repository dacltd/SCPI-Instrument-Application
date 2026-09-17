import copy
import csv
import json
import threading
import time
from contextlib import nullcontext
from unittest.mock import Mock, patch

import pytest
from PySide6.QtWidgets import QApplication

from dmm_app.automation import InstrumentController, SequenceRunner, load_sequence, validate_sequence
from dmm_app.automation_ui import examples_directory
from dmm_app.clock import AcquisitionClock
from dmm_app.gui import DMMAppWindow
from dmm_app.logging_util import CsvLogger
from dmm_app.models import InstrumentType
from dmm_app.scpi import SCPIClient


@pytest.fixture
def document():
    return load_sequence(examples_directory() / 'gsmiv-capture-check.json')


def fast(document):
    document = copy.deepcopy(document)
    for step in document['steps']:
        step['settle_s'] = step['capture_s'] = 0
    return document


class FakeController:
    def __init__(self, model):
        self.model = model
        self.calls = []

    def preflight(self, steps):
        return {'identity': self.model, 'output': False}

    def apply(self, action, value):
        self.calls.append((action, value))
        return {'readback': value}


def runner_for(document, tmp_path, callback=lambda e: None):
    controllers = {r: FakeController(c['model']) for r, c in document['instruments'].items()}
    events = []
    def emit(event):
        events.append(event)
        callback(event)
    runner = SequenceRunner(document, controllers, AcquisitionClock(), tmp_path / 'events.jsonl', emit)
    return runner, controllers, events


def test_bundled_examples_valid_and_confirmed_voltage():
    for path in examples_directory().glob('*.json'):
        document = load_sequence(path)
        assert document['required_observers'] == ['gsmiv_power']
        assert max(s['value'] for s in document['steps'] if s['action'] == 'voltage') == 10
        assert document['on_abort'][1]['action'] == 'hold'


@pytest.mark.parametrize('field,value', [('capture_s', -1), ('settle_s', float('nan')),
                                       ('capture_s', True), ('value', '10;OUTP ON')])
def test_invalid_steps_rejected(document, field, value):
    document['steps'][0][field] = value
    with pytest.raises(ValueError):
        validate_sequence(document)


def test_reject_unknown_actions_out_of_bounds_and_cleanup(document):
    for mutate in (
        lambda d: d['steps'][0].update(action='raw_scpi'),
        lambda d: d['steps'][0].update(value=11),
        lambda d: d['instruments']['source']['limits'].update(voltage=[0, 61]),
        lambda d: d['steps'][0].update(unexpected='ignored?'),
        lambda d: d['on_abort'].pop(),
        lambda d: d['on_abort'][0].update(value=True),
    ):
        changed = copy.deepcopy(document)
        mutate(changed)
        with pytest.raises(ValueError):
            validate_sequence(changed)


def test_output_enable_requires_explicit_limits_and_voltage(document):
    document['steps'] = [document['steps'][7]]
    with pytest.raises(ValueError, match='before enabling'):
        validate_sequence(document)


def test_reject_simulator_limit_change_while_running(document):
    document['steps'].append(copy.deepcopy(document['steps'][3]))
    with pytest.raises(ValueError, match='off before changing'):
        validate_sequence(document)


def test_runner_records_phases_order_and_cleanup(document, tmp_path):
    runner, controllers, events = runner_for(fast(document), tmp_path)
    runner.run()
    assert runner.result == 'complete'
    assert controllers['source'].calls[-1] == ('output', False)
    assert controllers['battery'].calls[-1] == ('hold', None)
    names = [e['event'] for e in events]
    assert names.index('command_start') < names.index('command_complete') < names.index('settle') < names.index('capture')
    disk = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    assert disk[-1]['outcome'] == 'complete'
    assert all(e['acquisition_run'] == 0 for e in disk)


def test_preflight_failure_sends_no_commands(document, tmp_path):
    runner, controllers, events = runner_for(fast(document), tmp_path)
    controllers['battery'].preflight = Mock(side_effect=ValueError('model range'))
    runner.run()
    assert runner.result == 'failed'
    assert not any(c.calls for c in controllers.values())
    assert events[-1]['cleanup_attempted'] is False


def test_abort_interrupts_dwell_and_runs_end_actions(document, tmp_path):
    document = fast(document)
    document['steps'][0]['capture_s'] = 30
    started = threading.Event()
    runner, controllers, events = runner_for(document, tmp_path,
                                            lambda e: started.set() if e['event'] == 'capture' else None)
    runner.start()
    assert started.wait(1)
    runner.abort()
    runner.join(1)
    assert not runner.is_alive()
    assert runner.result == 'aborted'
    assert controllers['source'].calls == [('voltage', 10), ('output', False)]
    assert controllers['battery'].calls == [('hold', None)]


def test_pause_retains_remaining_dwell_and_abort_works(document, tmp_path):
    document = fast(document)
    document['steps'][0]['capture_s'] = .25
    reached = threading.Event()
    runner, controllers, events = runner_for(document, tmp_path,
        lambda e: reached.set() if e['event'] == 'capture' else None)
    runner.start()
    assert reached.wait(1)
    runner.pause(True)
    time.sleep(.35)
    assert controllers['source'].calls == [('voltage', 10)]
    runner.pause(False)
    runner.join(2)
    assert runner.result == 'complete'
    assert {'pause', 'resume'} <= {json.loads(line)['event'] for line in (tmp_path / 'events.jsonl').read_text().splitlines()}


def test_failure_stops_sequence_and_attempts_all_cleanup(document, tmp_path):
    runner, controllers, events = runner_for(fast(document), tmp_path)
    controllers['source'].apply = Mock(side_effect=OSError('USB disconnected'))
    runner.run()
    assert runner.result == 'failed'
    assert controllers['source'].apply.call_count == 2
    assert controllers['battery'].calls == [('hold', None)]
    assert events[-1]['cleanup_errors']


def test_event_log_open_failure_prevents_writes(document, tmp_path):
    runner, controllers, events = runner_for(fast(document), tmp_path)
    (tmp_path / 'events.jsonl').write_text('existing evidence')
    runner.run()
    assert runner.result == 'failed'
    assert not any(c.calls for c in controllers.values())
    assert (tmp_path / 'events.jsonl').read_text() == 'existing evidence'


def test_log_failure_aborts_and_cleanup_still_runs(document, tmp_path):
    runner, controllers, events = runner_for(fast(document), tmp_path)
    real_emit = runner.on_event
    def break_log(event):
        real_emit(event)
        if event['event'] == 'command_start':
            runner._file.close()
            runner._file = Mock(write=Mock(side_effect=OSError('disk full')))
    runner.on_event = break_log
    runner.run()
    assert runner.result == 'failed'
    assert controllers['source'].calls[-1] == ('output', False)
    assert len(controllers['source'].calls) == 2


def scpi_mock(responses):
    client = Mock()
    client.transaction = nullcontext
    client.query.side_effect = lambda command: responses[command]
    return client


def test_owon_uses_current_setting_not_ocp_and_verifies():
    client = scpi_mock({'CURRent?': '0.050'})
    controller = InstrumentController('owon_spe6103', client)
    assert controller.apply('current_limit', .05)['readback'] == .05
    assert [c.args[0] for c in client.write.call_args_list] == ['SYSTem:REMote', 'CURRent 0.05']
    with pytest.raises(ValueError, match='read back'):
        controller.apply('current_limit', .5)


def test_keithley_preflight_mode_model_range_and_output(document):
    responses = {'*IDN?':'KEITHLEY,2281S-20-6,SERIAL', ':BATTery:OUTPut?':'0',
                 ':ENTRy:FUNCtion?':'SIM', ':BATTery:SIMulator:VOC? MINimum':'3.2',
                 ':BATTery:SIMulator:VOC? MAXimum':'4.2', ':SYSTem:ERRor?':'0,"No error"'}
    client = scpi_mock(responses)
    controller = InstrumentController('keithley_2281s', client)
    controller.preflight([s for s in document['steps'] if s['instrument'] == 'battery'])
    with pytest.raises(ValueError, match='model range'):
        controller.preflight([{'action':'voc', 'value':3.1}])
    responses[':ENTRy:FUNCtion?'] = 'POWER'
    with pytest.raises(ValueError, match='simulator mode'):
        controller.preflight([])
    responses[':BATTery:OUTPut?'] = '1'
    with pytest.raises(ValueError, match='output off'):
        controller.preflight([])
    client.write.assert_not_called()


def test_keithley_runtime_limit_guard_and_errors():
    responses = {':BATTery:OUTPut?':'1', ':SYSTem:ERRor?':'704,"Running"'}
    client = scpi_mock(responses)
    controller = InstrumentController('keithley_2281s', client)
    with pytest.raises(ValueError, match='output off'):
        controller.apply('current_limit', 2)
    client.write.assert_not_called()
    with pytest.raises(ValueError, match='704'):
        controller.apply('voc', 3.8)
    # Off must be sent even when an error is pending, with uncertainty reported.
    with pytest.raises(ValueError):
        controller.apply('output', False)
    assert client.write.call_args.args[0] == ':BATTery:OUTPut OFF'


def test_transactions_prevent_query_interleaving():
    transport = Mock(read_until=Mock(return_value=b'1\n'))
    client = SCPIClient(transport)
    ready = threading.Event()
    done = threading.Event()
    def polling():
        ready.set()
        client.query('MEAS?')
        done.set()
    with client.transaction():
        client.write('VOLT 1')
        thread = threading.Thread(target=polling)
        thread.start()
        assert ready.wait(1)
        assert not done.wait(.02)
        client.query('VOLT?')
    thread.join(1)
    assert [c.args[0] for c in transport.write.call_args_list] == [b'VOLT 1\n', b'VOLT?\n', b'MEAS?\n']


def test_csv_markers_retain_clock_and_schema(tmp_path):
    logger = CsvLogger(str(tmp_path/'readings.csv'))
    logger.write_event(dict(timestamp='2026-09-17', elapsed_seconds=3.2, acquisition_run=2,
                           event='capture', step=1))
    logger.close()
    rows = list(csv.DictReader((tmp_path/'readings.csv').open()))
    assert rows[0]['device_name'] == 'Automation'
    assert rows[0]['acquisition_run'] == '2'
    assert json.loads(rows[0]['raw_response'])['step'] == 1


@pytest.fixture
def window():
    app = QApplication.instance() or QApplication([])
    window = DMMAppWindow(discover_endpoints=False)
    yield window
    if window._automation.runner:
        window._automation.runner.abort()
        window._automation.runner.join(2)
        window._process_events()
    window.close()
    app.processEvents()


def test_editor_roundtrip_and_tab_cannot_close(window, document):
    tab = window._automation
    tab.set_document(document)
    assert tab.edited_document() == document
    tab.steps.item(0, 4).setText('20')
    assert tab.edited_document()['steps'][0]['settle_s'] == 20
    window._remove_instrument_tab(window._tabs.indexOf(tab))
    assert window._tabs.indexOf(tab) == 1
    tab.steps.selectRow(0)
    tab.move_step(1)
    assert tab.edited_document()['steps'][1]['label'] == document['steps'][0]['label']


def test_gui_run_records_snapshot_locks_and_keeps_acquisition(window, document, tmp_path):
    document = fast(document)
    for profile, endpoint in ((InstrumentType.OWON_SPE6103, 'OWON'),
                              (InstrumentType.KEITHLEY_2281S, '2281'),
                              (InstrumentType.GSMIV_POWER, 'UART')):
        panel = window.add_instrument(profile)
        panel._transport = Mock(is_open=True)
        panel._scpi = Mock()
        panel._endpoint_combo.setCurrentText(endpoint)
        def start(panel=panel, **kwargs):
            panel._worker = Mock(is_alive=Mock(return_value=True))
            return True
        panel.start_acquisition = start
    tab = window._automation
    tab.set_document(document)
    tab.run_notes.setPlainText('Board 1, validation firmware, 25 C, calibrated reference')
    tab.reviewed.setChecked(True)
    with patch('dmm_app.automation_ui.InstrumentController', side_effect=lambda model, scpi: FakeController(model)), \
         patch('dmm_app.automation_ui.QFileDialog.getExistingDirectory', return_value=str(tmp_path)):
        tab.start()
    assert tab.active
    assert all(p._automation_locked for p in window._panels)
    tab.runner.join(2)
    window._process_events()
    assert not tab.active
    assert window._panels[0].is_running  # keep post-run capture
    folder = next(tmp_path.iterdir())
    assert {'sequence.json','workspace.json','assignments.json','run-notes.txt','readings.csv','events.jsonl'} <= {p.name for p in folder.iterdir()}
    assert 'run_end' in (folder/'readings.csv').read_text()
    for panel in window._panels:
        panel._worker = None


def test_abort_on_acquisition_error_and_close_defers_cleanup(window):
    tab = window._automation
    runner = Mock(is_alive=Mock(return_value=True))
    tab.runner = runner
    event = Mock()
    window.closeEvent(event)
    runner.abort.assert_called_once()
    event.ignore.assert_called_once()
    tab.runner = None


def test_completion_cleanup_failure_also_attempts_abort_plan(document, tmp_path):
    document = fast(document)
    document['on_complete'] = [dict(instrument='source', action='hold', value=None),
                               dict(instrument='battery', action='output', value=False)]
    runner, controllers, events = runner_for(document, tmp_path)
    original = controllers['battery'].apply
    def failed_battery_off(action, value):
        if action == 'output' and value is False:
            raise OSError('battery USB lost')
        return original(action, value)
    controllers['battery'].apply = failed_battery_off
    runner.run()
    assert runner.result == 'failed'
    assert controllers['source'].calls[-2:] == [('hold', None), ('output', False)]
    assert controllers['battery'].calls[-1] == ('hold', None)


def test_stop_all_defers_acquisition_stop_until_cleanup(window):
    tab = window._automation
    tab.runner = Mock(is_alive=Mock(return_value=True))
    panel = window._panels[0]
    panel.stop_acquisition = Mock()
    window._stop_all()
    tab.runner.abort.assert_called_once()
    panel.stop_acquisition.assert_not_called()
    tab.runner.is_alive.return_value = False
    tab._run_end_seen = True
    tab.tick()
    panel.stop_acquisition.assert_called_once()
    assert not window._stop_after_automation


def test_aborting_while_paused_is_prompt(document, tmp_path):
    runner, controllers, events = runner_for(fast(document), tmp_path)
    runner.pause(True)
    runner.start()
    runner.abort()
    runner.join(1)
    assert runner.result == 'aborted'
    assert not any(c.calls for c in controllers.values())


def test_dmm_readiness_precedes_any_setting_write(document, tmp_path):
    ready = threading.Event()
    waiting = threading.Event()
    runner, controllers, events = runner_for(fast(document), tmp_path,
        lambda e: waiting.set() if e['event'] == 'acquisition_wait' else None)
    runner.ready_events = (ready,)
    runner.start()
    assert waiting.wait(1)
    assert not any(c.calls for c in controllers.values())
    ready.set()
    runner.join(2)
    assert runner.result == 'complete'
    names = [e['event'] for e in events]
    assert names.index('acquisition_ready') < names.index('command_start')


def test_missing_dmm_readiness_times_out_without_mutations(document, tmp_path):
    runner, controllers, events = runner_for(fast(document), tmp_path)
    runner.ready_events = (threading.Event(),)
    runner.ready_timeout_seconds = .01
    runner.run()
    assert runner.result == 'failed'
    assert not any(c.calls for c in controllers.values())
    assert not events[-1]['cleanup_attempted']
