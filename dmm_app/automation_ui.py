from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys
import uuid

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTabWidget, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget, QStyledItemDelegate,
)

from dmm_app.automation import ACTIONS, InstrumentController, SequenceRunner, load_sequence, validate_sequence
from dmm_app.models import InstrumentType
from dmm_app.poller import PollingWorker

MODEL_PROFILES = {'owon_spe6103': InstrumentType.OWON_SPE6103,
                  'keithley_2281s': InstrumentType.KEITHLEY_2281S}
STEP_FIELDS = ('label', 'instrument', 'action', 'value', 'settle_s', 'capture_s')


def examples_directory():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    return root / 'docs' / 'sequences'


class StepDelegate(QStyledItemDelegate):
    def __init__(self, tab):
        super().__init__(tab.steps)
        self.tab = tab

    def createEditor(self, parent, option, index):
        document = self.tab.document
        choices = None
        if document and index.column() == 1:
            choices = list(document['instruments'])
        elif document and index.column() == 2:
            role = index.siblingAtColumn(1).data()
            config = document['instruments'].get(role)
            if config:
                choices = ['hold', *ACTIONS[config['model']]]
        elif index.column() == 3:
            action = index.siblingAtColumn(2).data()
            choices = {'output': ['true', 'false'], 'hold': ['null'],
                       'method': ['"static"', '"dynamic"']}.get(action)
        if choices is None:
            return super().createEditor(parent, option, index)
        editor = QComboBox(parent)
        editor.addItems(choices)
        return editor

    def setEditorData(self, editor, index):
        if isinstance(editor, QComboBox):
            editor.setCurrentText(index.data())
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        if isinstance(editor, QComboBox):
            model.setData(index, editor.currentText(), Qt.EditRole)
        else:
            super().setModelData(editor, model, index)


class AutomationTab(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.document = None
        self.runner = None
        self.panels = []
        self.assignments = {}
        self.paused = False
        self._run_end_seen = False
        self._run_outcome = None
        self._end_summary = ""
        self._stopping_capture = False
        self._build_ui()

    @property
    def active(self):
        # Retain ownership until the worker is fully finished, including cleanup.
        return self.runner is not None or self._stopping_capture

    def _build_ui(self):
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.load_button = QPushButton('Load sequence…')
        self.load_button.clicked.connect(self.load_file)
        self.save_button = QPushButton('Save sequence…')
        self.save_button.clicked.connect(self.save_file)
        self.example_combo = QComboBox()
        self.example_combo.addItem('USB capture check', 'gsmiv-capture-check.json')
        self.example_combo.addItem('Battery policy plateaus', 'gsmiv-policy-plateaus.json')
        self.example_button = QPushButton('Load example')
        self.example_button.clicked.connect(self.load_example)
        for widget in (self.load_button, self.save_button, self.example_combo, self.example_button):
            controls.addWidget(widget)
        controls.addStretch()
        layout.addLayout(controls)
        self.pages = QTabWidget()
        sequence_page = QWidget()
        sequence_layout = QVBoxLayout(sequence_page)
        self.name = QLineEdit()
        self.name.setPlaceholderText('Sequence name')
        sequence_layout.addWidget(self.name)
        self.steps = self.table(('Step / purpose', 'Instrument role', 'Action', 'Value', 'Settle (s)', 'Capture (s)', 'Read-back ±'))
        self.steps.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.steps.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.steps.setWordWrap(False)
        self.steps.setItemDelegate(StepDelegate(self))
        sequence_layout.addWidget(self.steps, 1)
        edit_row = QHBoxLayout()
        for label, callback in (('Add step', self.add_step), ('Remove step', self.remove_step),
                                ('Move up', lambda: self.move_step(-1)),
                                ('Move down', lambda: self.move_step(1))):
            button = QPushButton(label)
            button.clicked.connect(callback)
            edit_row.addWidget(button)
        edit_row.addStretch()
        sequence_layout.addLayout(edit_row)
        hint = QLabel('Each step sends one action, checks read-back, then settles and captures. '
                      'All readings are logged throughout. Blank Read-back ± uses the Setup default; a number overrides it for that step.')
        hint.setWordWrap(True)
        sequence_layout.addWidget(hint)
        self.pages.addTab(sequence_page, 'Sequence')

        setup = QWidget()
        setup_layout = QVBoxLayout(setup)
        self.mapping_widget = QWidget()
        self.mapping = QFormLayout(self.mapping_widget)
        setup_layout.addWidget(self.mapping_widget)
        refresh = QPushButton('Refresh instrument assignments')
        refresh.clicked.connect(self.refresh_assignments)
        setup_layout.addWidget(refresh)
        self.limits = self.table(('Instrument role', 'Setting', 'Minimum', 'Maximum', 'Read-back ±'))
        setup_layout.addWidget(QLabel('Allowed settings and absolute read-back tolerance (V, A or % for SOC)'))
        setup_layout.addWidget(self.limits, 1)
        self.endings = self.table(('Instrument role', 'On completion', 'On abort / error'))
        self.endings.setMaximumHeight(130)
        setup_layout.addWidget(self.endings)
        self.notes = QTextEdit()
        self.notes.setMaximumHeight(150)
        setup_layout.addWidget(self.notes)
        self.pages.addTab(setup, 'Setup')

        run_page = QWidget()
        run_layout = QVBoxLayout(run_page)
        self.run_notes = QTextEdit()
        self.run_notes.setMaximumHeight(110)
        self.run_notes.setPlaceholderText('Run notes: board serial/revision, firmware/build, battery model, '
                                          'wiring, temperature, reference calibration and load state')
        run_layout.addWidget(self.run_notes)
        self.reviewed = QCheckBox('I reviewed the wiring, current limits, model range and end/abort actions')
        run_layout.addWidget(self.reviewed)
        self.history = QTextEdit()
        self.history.setReadOnly(True)
        self.history.document().setMaximumBlockCount(2000)
        run_layout.addWidget(self.history, 1)
        self.pages.addTab(run_page, 'Run')
        layout.addWidget(self.pages, 1)
        self.status = QLabel('Load a sequence to begin.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        run_controls = QHBoxLayout()
        self.start_button = QPushButton('Start sequence…')
        self.start_button.clicked.connect(self.start)
        self.pause_button = QPushButton('Pause')
        self.pause_button.clicked.connect(self.toggle_pause)
        self.abort_button = QPushButton('Abort sequence')
        self.abort_button.clicked.connect(self.abort)
        self.stop_capture_button = QPushButton('Stop capture')
        self.stop_capture_button.setToolTip('Stop measurement logging. During a sequence, abort and finish cleanup first.')
        self.stop_capture_button.clicked.connect(self.stop_capture)
        self.stop_capture_button.setEnabled(False)
        for widget in (self.start_button, self.pause_button, self.abort_button, self.stop_capture_button):
            run_controls.addWidget(widget)
        layout.addLayout(run_controls)
        self.pause_button.setEnabled(False)
        self.abort_button.setEnabled(False)
        self.steps.itemChanged.connect(lambda: self.reviewed.setChecked(False))
        self.limits.itemChanged.connect(lambda: self.reviewed.setChecked(False))

    @staticmethod
    def table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        return table

    def fill_step(self, row, step):
        self.steps.insertRow(row)
        for column, field in enumerate(STEP_FIELDS):
            value = step[field]
            text = json.dumps(value) if field == 'value' else str(value)
            item = QTableWidgetItem(text)
            item.setToolTip(text)
            self.steps.setItem(row, column, item)
        tolerance = QTableWidgetItem(str(step['readback_tolerance']) if 'readback_tolerance' in step else '')
        tolerance.setToolTip('Blank: use Setup default. Number: absolute tolerance for this step in V, A or %; 0 requires exact equality.')
        self.steps.setItem(row, 6, tolerance)

    def set_document(self, document):
        if self.active:
            raise ValueError('Finish the current sequence first')
        self.document = validate_sequence(document)
        self.name.setText(document['name'])
        self.notes.setPlainText(document['notes'])
        self.steps.setRowCount(0)
        for step in document['steps']:
            self.fill_step(self.steps.rowCount(), step)
        self.limits.setRowCount(0)
        self.endings.setRowCount(0)
        for role, config in document['instruments'].items():
            for action, bounds in config['limits'].items():
                row = self.limits.rowCount()
                self.limits.insertRow(row)
                tolerance = config.get('readback_tolerances', {}).get(action, ACTIONS[config['model']][action][2])
                for column, value in enumerate((role, action, *bounds, tolerance)):
                    item = QTableWidgetItem(str(value))
                    if column < 2:
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    self.limits.setItem(row, column, item)
            row = self.endings.rowCount()
            self.endings.insertRow(row)
            item = QTableWidgetItem(role)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.endings.setItem(row, 0, item)
            for column, field in ((1, 'on_complete'), (2, 'on_abort')):
                combo = QComboBox()
                combo.addItem('Keep settings / output unchanged', 'hold')
                combo.addItem('Turn output off', 'output')
                action = next(a for a in document[field] if a['instrument'] == role)
                combo.setCurrentIndex(combo.findData(action['action']))
                combo.currentIndexChanged.connect(lambda: self.reviewed.setChecked(False))
                self.endings.setCellWidget(row, column, combo)
        self.refresh_assignments()
        self.reviewed.setChecked(False)
        total = sum(s['settle_s'] + s['capture_s'] for s in document['steps'])
        self.status.setText(f"{len(document['steps'])} steps · minimum dwell {total / 60:.1f} min + command time. "
                            'Review Setup, then Run. Outputs must initially be off.')
        self.pages.setCurrentIndex(0)

    def refresh_assignments(self):
        if self.active or not self.document:
            return
        previous = {role: combo.currentData() for role, combo in self.assignments.items()}
        while self.mapping.rowCount():
            self.mapping.removeRow(0)
        self.assignments = {}
        for role, config in self.document['instruments'].items():
            combo = QComboBox()
            combo.addItem('Choose instrument…', None)
            for panel in self.window._panels:
                if panel._selected_instrument() == MODEL_PROFILES[config['model']]:
                    combo.addItem(f'{panel.instrument_index + 1} · {panel._device_idn} · '
                                  f'{panel._endpoint_combo.currentText()}', panel.instrument_index)
            if previous.get(role) is not None:
                combo.setCurrentIndex(max(0, combo.findData(previous[role])))
            elif combo.count() == 2:
                combo.setCurrentIndex(1)
            combo.currentIndexChanged.connect(lambda: self.reviewed.setChecked(False))
            self.assignments[role] = combo
            self.mapping.addRow(role, combo)

    def edited_document(self):
        if self.document is None:
            raise ValueError('Load a sequence first')
        document = json.loads(json.dumps(self.document))
        document.update(name=self.name.text(), notes=self.notes.toPlainText(), steps=[])
        for row in range(self.steps.rowCount()):
            values = [self.steps.item(row, col).text() for col in range(len(STEP_FIELDS))]
            values[3] = json.loads(values[3])
            values[4:] = [float(value) for value in values[4:]]
            step = dict(zip(STEP_FIELDS, values))
            tolerance = self.steps.item(row, 6).text().strip()
            if tolerance:
                step['readback_tolerance'] = float(tolerance)
            document['steps'].append(step)
        for row in range(self.limits.rowCount()):
            role, action, low, high, tolerance = [self.limits.item(row, col).text() for col in range(5)]
            config = document['instruments'][role]
            config['limits'][action] = [float(low), float(high)]
            value = float(tolerance)
            if action in config.get('readback_tolerances', {}) or value != ACTIONS[config['model']][action][2]:
                config.setdefault('readback_tolerances', {})[action] = value
        # Preserve declared cleanup ordering (which may differ from role order).
        for row in range(self.endings.rowCount()):
            role = self.endings.item(row, 0).text()
            for col, field in ((1, 'on_complete'), (2, 'on_abort')):
                action = self.endings.cellWidget(row, col).currentData()
                existing = next(a for a in document[field] if a['instrument'] == role)
                existing.update(action=action, value=None if action == 'hold' else False)
        return validate_sequence(document)

    def add_step(self):
        if self.document:
            self.fill_step(self.steps.rowCount(), dict(label='Hold', instrument=next(iter(self.document['instruments'])),
                           action='hold', value=None, settle_s=5, capture_s=60))

    def remove_step(self):
        self.steps.removeRow(self.steps.currentRow())
        self.reviewed.setChecked(False)

    def move_step(self, offset):
        row = self.steps.currentRow()
        target = row + offset
        if not 0 <= target < self.steps.rowCount() or row < 0:
            return
        for col in range(self.steps.columnCount()):
            a, b = self.steps.takeItem(row, col), self.steps.takeItem(target, col)
            self.steps.setItem(row, col, b)
            self.steps.setItem(target, col, a)
        self.steps.selectRow(target)
        self.reviewed.setChecked(False)

    def load_example(self):
        self._load(examples_directory() / self.example_combo.currentData())

    def load_file(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Load test sequence', '', 'Test sequences (*.json)')
        if path:
            self._load(path)

    def _load(self, path):
        try:
            self.set_document(load_sequence(path))
        except (ValueError, OSError, TypeError, KeyError) as exc:
            QMessageBox.warning(self, 'Cannot load sequence', str(exc))

    def save_file(self):
        try:
            document = self.edited_document()
            path, _ = QFileDialog.getSaveFileName(self, 'Save test sequence', 'sequence.json', 'Test sequences (*.json)')
            if path:
                destination = Path(path)
                temporary = destination.with_name(destination.name + '.tmp-' + uuid.uuid4().hex)
                try:
                    temporary.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            QMessageBox.warning(self, 'Cannot save sequence', str(exc))

    def start(self):
        if self.active:
            return
        started = []
        try:
            document = self.edited_document()
            if not self.reviewed.isChecked() or not self.run_notes.toPlainText().strip():
                raise ValueError('On Run, enter the board/run notes and confirm the setup review.')
            if any(p.is_running or p.is_scope_tools_busy for p in self.window._panels):
                raise ValueError('Stop the previous capture using Stop capture (or Stop all acquisition), then start again. Automation starts a new shared capture.')
            for config in document['instruments'].values():
                tolerances = config.setdefault('readback_tolerances', {})
                for action in config['limits']:
                    tolerances.setdefault(action, ACTIONS[config['model']][action][2])
            controllers, assignments = {}, {}
            for role, combo in self.assignments.items():
                index = combo.currentData()
                if index is None or index >= len(self.window._panels):
                    raise ValueError(f'Assign a connected instrument to {role} on Setup')
                panel = self.window._panels[index]
                model = document['instruments'][role]['model']
                if not panel.is_connected or not panel._scpi or panel._selected_instrument() != MODEL_PROFILES[model]:
                    raise ValueError(f'{role}: connect the correct instrument first')
                if index in assignments.values():
                    raise ValueError('Assign distinct physical instruments to each role')
                assignments[role] = index
                controllers[role] = InstrumentController(model, panel._scpi)
            connected = [p for p in self.window._panels if p.is_connected]
            if 'gsmiv_power' in document.get('required_observers', []) and not any(
                p._selected_instrument() == InstrumentType.GSMIV_POWER for p in connected
            ):
                raise ValueError('Connect GSMIV power telemetry for this battery-policy capture')
            endpoints = [p._endpoint_combo.currentText() for p in connected]
            if len(endpoints) != len(set(endpoints)):
                raise ValueError('Each instrument must have a distinct connection')
            if any(p._selected_instrument() not in (*MODEL_PROFILES.values(), InstrumentType.GSMIV_POWER,
                                                   InstrumentType.MP730889, InstrumentType.PICOLOG_TC08)
                   for p in connected):
                raise ValueError('This version records scalar instruments. Disconnect scope/raw-serial panels first.')
            folder = QFileDialog.getExistingDirectory(self, 'Choose parent folder for this capture')
            if not folder:
                return
            run_dir = Path(folder) / (datetime.now().strftime('battery-test-%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6])
            run_dir.mkdir()
            (run_dir / 'sequence.json').write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
            (run_dir / 'run-notes.txt').write_text(self.run_notes.toPlainText() + '\n', encoding='utf-8')
            (run_dir / 'assignments.json').write_text(json.dumps(assignments, indent=2), encoding='utf-8')
            self.window._process_events()  # drain prior capture before changing clock/log destination
            self.window._close_shared_logger()
            self.window._close_panel_loggers(clear_paths=False)
            self.window._shared_log_path = str(run_dir / 'readings.csv')
            self.window._log_path_label.setText(self.window._shared_log_path)
            with QSignalBlocker(self.window._log_checkbox):
                self.window._log_checkbox.setChecked(True)
            for panel in self.window._panels:
                with QSignalBlocker(panel._logging_checkbox):
                    panel._logging_checkbox.setChecked(panel in connected)
            self.window._update_loggers()
            self.window._write_configuration_file(str(run_dir / 'workspace.json'))
            (run_dir / 'instruments.json').write_text(json.dumps([
                dict(slot=p.instrument_index, model=p._selected_instrument().value,
                     endpoint=p._endpoint_combo.currentText(), identity=p._device_idn)
                for p in connected
            ], indent=2), encoding='utf-8')
            self.window._clock.reset()
            for panel in connected:
                if not panel.start_acquisition(announce=False):
                    raise ValueError(f'Could not start acquisition on instrument {panel.instrument_index + 1}')
                started.append(panel)
            self.panels = connected
            self.runner = SequenceRunner(document, controllers, self.window._clock, run_dir / 'events.jsonl',
                                         lambda data: self.window._enqueue_event('automation', -1, data),
                                         ready_events=[p._worker.ready for p in connected
                                                       if p._selected_instrument() == InstrumentType.MP730889
                                                       and isinstance(p._worker, PollingWorker)])
            self.history.clear()
            self.history.append(f'Capture folder: {run_dir}')
            self.paused = False
            self._run_end_seen = False
            self._run_outcome = None
            self._end_summary = ""
            self.lock_controls(True)
            self.pages.setCurrentIndex(2)
            self.runner.start()
        except (ValueError, OSError, TypeError, KeyError, RuntimeError) as exc:
            for panel in started:
                panel.stop_acquisition(announce=False)
            self.runner = None
            self.lock_controls(False)
            QMessageBox.warning(self, 'Cannot start sequence', str(exc))

    def lock_controls(self, locked):
        for widget in (self.load_button, self.save_button, self.example_button, self.example_combo,
                       self.start_button, self.run_notes, self.reviewed,
                       self.window._load_config_button, self.window._add_instrument_button):
            widget.setEnabled(not locked)
        for index in (0, 1):
            self.pages.widget(index).setEnabled(not locked)
        self.pause_button.setEnabled(locked)
        self.abort_button.setEnabled(locked and self.runner is not None)
        self.stop_capture_button.setEnabled(locked or any(p.is_running for p in self.panels))
        for panel in self.window._panels:
            panel._automation_locked = locked
            panel._detail_tabs.widget(1).setEnabled(not locked)
            panel._refresh_controls()

    def toggle_pause(self):
        if self.runner:
            self.paused = not self.paused
            self.runner.pause(self.paused)
            self.pause_button.setText('Resume' if self.paused else 'Pause')
            self.history.append('Pause requested: settings held; logging continues.' if self.paused else 'Resumed.')
            self.status.setText('Paused: settings held; logging continues.' if self.paused else 'Resuming…')

    def abort(self, reason='Operator aborted'):
        if self.runner:
            self.runner.abort(reason if isinstance(reason, str) else 'Operator aborted')
            self.pause_button.setEnabled(False)
            self.status.setText('Aborting: waiting for current I/O and end actions. Logging continues.')

    def handle_event(self, data):
        if data['event'] == 'progress':
            if not self.paused:
                self.status.setText(f"Step {data['step']} · {data['phase']} · {data['remaining_s']:.1f} s remaining")
            return
        if data['event'] == 'command_start':
            self.steps.selectRow(data['step'] - 1)
        self.history.append(f"+{data['elapsed_seconds']:.3f} s  {data['event']}  " +
                            json.dumps({k: v for k, v in data.items() if k not in ('timestamp', 'elapsed_seconds', 'acquisition_run', 'event')}))
        if data['event'] == 'run_end':
            self._run_end_seen = True
            self._run_outcome = data['outcome']
            self._end_summary = f"Sequence {data['outcome']}. {data['detail']} "
            if data['cleanup_errors']:
                self._end_summary += 'Cleanup errors: ' + '; '.join(data['cleanup_errors']) + '. '
            if not data['cleanup_attempted']:
                self._end_summary += 'No sequence settings were applied. '
            self.status.setText(self._end_summary + 'Finishing run…')

    def stop_capture(self):
        if self.runner:
            self.window._stop_after_automation = True
            self.abort('Stop capture requested')
            return
        self._begin_capture_stop()

    def _begin_capture_stop(self):
        # Request every stop first, then let tick observe completion. Never drop
        # worker/session ownership or block the UI waiting for a serial timeout.
        self._stopping_capture = True
        self.lock_controls(True)
        self.pause_button.setEnabled(False)
        self.abort_button.setEnabled(False)
        self.stop_capture_button.setEnabled(False)
        self.status.setText(self._end_summary + 'Stopping capture; waiting for pending instrument I/O…')
        for panel in self.panels:
            panel._stream_requested = False
            panel._plot.set_stream_expected(False)
            if panel._worker is not None:
                panel._worker.stop()

    def _capture_stopped(self):
        for panel in self.panels:
            panel.stop_acquisition(announce=False)
        self._stopping_capture = False
        self.lock_controls(False)
        self.stop_capture_button.setEnabled(False)
        self.status.setText(self._end_summary +
                            'Capture stopped. Resolve any reported instrument error, review setup and start again. '
                            'Stopping capture does not change instrument outputs.')
        stamp, elapsed, run = self.window._clock.capture()
        self.window._enqueue_event('automation', -1, dict(
            timestamp=stamp.isoformat(timespec='microseconds'), elapsed_seconds=elapsed,
            acquisition_run=run, event='capture_stopped'))
        if self.window._stop_after_automation:
            self.window._stop_after_automation = False
            self.window._stop_all()

    def tick(self):
        if self.runner and not self.runner.is_alive() and self._run_end_seen:
            self.runner = None
            self.pause_button.setText('Pause')
            self.reviewed.setChecked(False)
            if self._run_outcome != 'complete' or self.window._stop_after_automation:
                self._begin_capture_stop()
            else:
                self.lock_controls(False)
                self.status.setText(self._end_summary +
                                    'Capture continues. Use Stop capture before starting another sequence. '
                                    'Review output states before disconnecting.')
        elif self.runner:
            for panel in self.panels:
                if not panel.is_connected or not panel.is_running:
                    self.abort(f'Acquisition stopped on instrument {panel.instrument_index + 1}')
        if self._stopping_capture and not any(p.is_running for p in self.panels):
            self._capture_stopped()
        elif not self.active:
            self.stop_capture_button.setEnabled(any(p.is_running for p in self.panels))
