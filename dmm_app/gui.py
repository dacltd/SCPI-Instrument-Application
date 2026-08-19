from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from dmm_app.clock import AcquisitionClock
from dmm_app.commands import INSTRUMENT_PROFILES, InstrumentProfile, idn_matches_profile
from dmm_app.logging_util import CsvLogger
from dmm_app.models import (
    ConnectionKind,
    InstrumentType,
    MeasurementFunction,
    Reading,
    SerialSettings,
    VisaSettings,
)
from dmm_app.poller import PollRequest, PollingWorker, RawSerialWorker, parse_primary_value
from dmm_app.oscilloscope import (
    DHO804_MEMORY_POINTS,
    OscilloscopeSetup,
    RepeatedWaveformCaptureWorker,
    WaveformCaptureProgress,
    WaveformCaptureWorker,
    build_dho804_setup_commands,
    optimise_waveform_logging,
)
from dmm_app.scpi import SCPIClient
from dmm_app.transport import SerialTransport, Transport, VisaTransport

BAUD_RATES = ["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"]
LINE_ENDINGS = {"LF (\\n)": b"\n", "CRLF (\\r\\n)": b"\r\n", "CR (\\r)": b"\r"}


@dataclass
class MeasurementRow:
    container: QWidget
    function_combo: QComboBox
    source_combo: QComboBox
    latest_label: QLabel
    remove_button: QPushButton
    last_valid_function: MeasurementFunction
    last_valid_source: str


class CollapsibleSection(QWidget):
    def __init__(self, title: str, content: QWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self._content = content
        self._title = title
        section_layout = QVBoxLayout(self)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(3)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title_label = QLabel(f"<b>{title}</b>")
        header.addWidget(title_label)
        header.addStretch(1)
        self.toggle_button = QPushButton("−")
        self.toggle_button.setFixedWidth(28)
        self.toggle_button.setToolTip(f"Minimise {title.lower()} section")
        self.toggle_button.setAccessibleName(f"Toggle {title} section")
        self.toggle_button.clicked.connect(self.toggle)
        header.addWidget(self.toggle_button)
        section_layout.addLayout(header)
        section_layout.addWidget(content)

    @property
    def is_expanded(self) -> bool:
        return not self._content.isHidden()

    def toggle(self) -> None:
        self.set_expanded(not self.is_expanded)

    def set_expanded(self, expanded: bool) -> None:
        self._content.setVisible(expanded)
        self.toggle_button.setText("−" if expanded else "+")
        action = "Minimise" if expanded else "Expand"
        self.toggle_button.setToolTip(f"{action} {self._title.lower()} section")


class InstrumentPanel(QGroupBox):
    def __init__(
        self,
        instrument_index: int,
        clock: AcquisitionClock,
        event_sink,
        refresh_sink,
        logging_selection_sink,
        waveform_output_directory_provider,
        initial_instrument: InstrumentType,
        parent: QWidget | None = None,
    ):
        super().__init__(f"Instrument {instrument_index + 1}", parent)
        self.instrument_index = instrument_index
        self._clock = clock
        self._event_sink = event_sink
        self._refresh_sink = refresh_sink
        self._logging_selection_sink = logging_selection_sink
        self._waveform_output_directory_provider = waveform_output_directory_provider
        self._transport: Transport | None = None
        self._scpi: SCPIClient | None = None
        self._worker: (
            PollingWorker
            | RawSerialWorker
            | WaveformCaptureWorker
            | RepeatedWaveformCaptureWorker
            | None
        ) = None
        self._scope_setup_applied: OscilloscopeSetup | None = None
        self._device_idn = "UNKNOWN"
        self._measurement_rows: list[MeasurementRow] = []

        self._build_ui()
        with QSignalBlocker(self._instrument_combo):
            self._instrument_combo.setCurrentText(initial_instrument.value)
        self._reload_profile()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        connection_content = QGroupBox()
        connection = QGridLayout(connection_content)
        connection.setContentsMargins(6, 6, 6, 6)
        connection.setHorizontalSpacing(6)
        connection.setVerticalSpacing(3)
        connection.addWidget(QLabel("Profile"), 0, 0)
        self._instrument_combo = QComboBox()
        self._instrument_combo.addItems([item.value for item in InstrumentType])
        self._instrument_combo.currentIndexChanged.connect(self._on_instrument_changed)
        connection.addWidget(self._instrument_combo, 1, 0, 1, 2)

        self._endpoint_label = QLabel("Serial port")
        connection.addWidget(self._endpoint_label, 0, 2)
        self._endpoint_combo = QComboBox()
        self._endpoint_combo.setEditable(True)
        self._endpoint_combo.setMinimumWidth(170)
        connection.addWidget(self._endpoint_combo, 1, 2, 1, 2)

        self._baud_label = QLabel("Baud")
        connection.addWidget(self._baud_label, 0, 4)
        self._baud_combo = QComboBox()
        self._baud_combo.addItems(BAUD_RATES)
        self._baud_combo.setCurrentText("9600")
        connection.addWidget(self._baud_combo, 1, 4)

        self._ending_label = QLabel("Line ending")
        connection.addWidget(self._ending_label, 0, 5)
        self._ending_combo = QComboBox()
        self._ending_combo.addItems(LINE_ENDINGS)
        connection.addWidget(self._ending_combo, 1, 5)

        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(self._refresh_sink)
        connection.addWidget(self._refresh_button, 1, 6)

        self._connect_button = QPushButton("Connect")
        self._connect_button.clicked.connect(self._toggle_connection)
        connection.addWidget(self._connect_button, 1, 7)

        self._idn_button = QPushButton("*IDN?")
        self._idn_button.clicked.connect(self._request_idn)
        connection.addWidget(self._idn_button, 1, 8)

        self._status_label = QLabel("Disconnected")
        self._status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        connection.addWidget(self._status_label, 2, 0, 1, 9)
        self._profile_section = CollapsibleSection("Profile & Connection", connection_content)
        layout.addWidget(self._profile_section)

        measurement_content = QGroupBox()
        measurement_layout = QVBoxLayout(measurement_content)
        measurement_layout.setContentsMargins(6, 6, 6, 6)
        measurement_layout.setSpacing(4)

        header = QHBoxLayout()
        header.addWidget(QLabel("Measurement"), stretch=2)
        header.addWidget(QLabel("Source"), stretch=1)
        header.addWidget(QLabel("Latest"), stretch=2)
        header.addSpacing(62)
        measurement_layout.addLayout(header)

        self._measurement_rows_layout = QVBoxLayout()
        self._measurement_rows_layout.setSpacing(3)
        measurement_layout.addLayout(self._measurement_rows_layout)

        controls = QHBoxLayout()
        self._acquisition_mode_label = QLabel("Output")
        controls.addWidget(self._acquisition_mode_label)
        self._acquisition_mode_combo = QComboBox()
        self._acquisition_mode_combo.addItem("Measurement values", "measurements")
        self._acquisition_mode_combo.addItem("Repeated RAW waveforms", "waveforms")
        self._acquisition_mode_combo.setToolTip(
            "Choose scalar measurement polling or repeated single-trigger RAW captures"
        )
        self._acquisition_mode_combo.currentIndexChanged.connect(
            self._on_acquisition_mode_changed
        )
        controls.addWidget(self._acquisition_mode_combo)
        self._interval_label = QLabel("Interval (ms)")
        controls.addWidget(self._interval_label)
        self._interval_input = QLineEdit("1000")
        self._interval_input.setMaximumWidth(74)
        controls.addWidget(self._interval_input)

        self._start_button = QPushButton("Start")
        self._start_button.clicked.connect(self.start_acquisition)
        controls.addWidget(self._start_button)
        self._stop_button = QPushButton("Stop")
        self._stop_button.clicked.connect(self.stop_acquisition)
        controls.addWidget(self._stop_button)
        self._snapshot_button = QPushButton("Snapshot")
        self._snapshot_button.clicked.connect(self.take_snapshot)
        controls.addWidget(self._snapshot_button)
        self._add_button = QPushButton("Add")
        self._add_button.clicked.connect(self._add_measurement)
        controls.addWidget(self._add_button)
        self._logging_checkbox = QCheckBox("Log this instrument")
        self._logging_checkbox.setToolTip(
            "Include readings from this instrument in the selected CSV file"
        )
        self._logging_checkbox.toggled.connect(
            lambda enabled: self._logging_selection_sink(self.instrument_index, enabled)
        )
        controls.addWidget(self._logging_checkbox)
        controls.addStretch(1)
        measurement_layout.addLayout(controls)
        self._measurement_section = CollapsibleSection("Measurement", measurement_content)
        layout.addWidget(self._measurement_section)

        scope_content = QGroupBox()
        scope_layout = QGridLayout(scope_content)
        scope_layout.setContentsMargins(6, 6, 6, 6)
        scope_layout.setHorizontalSpacing(6)
        scope_layout.setVerticalSpacing(4)

        safety = QLabel(
            "Safety: DHO804 channel/USB grounds are earth-referenced. Verify the measured "
            "ground, probe rating and connection before applying settings."
        )
        safety.setWordWrap(True)
        scope_layout.addWidget(safety, 0, 0, 1, 8)
        self._scope_safety_checkbox = QCheckBox("Ground and probe safety verified")
        self._scope_safety_checkbox.toggled.connect(self._refresh_controls)
        scope_layout.addWidget(self._scope_safety_checkbox, 1, 0, 1, 3)
        self._scope_only_channel_checkbox = QCheckBox("Only selected channel")
        self._scope_only_channel_checkbox.setChecked(True)
        scope_layout.addWidget(self._scope_only_channel_checkbox, 1, 3, 1, 3)

        scope_layout.addWidget(QLabel("Channel"), 2, 0)
        self._scope_channel_combo = QComboBox()
        self._scope_channel_combo.addItems(("CHANnel1", "CHANnel2", "CHANnel3", "CHANnel4"))
        scope_layout.addWidget(self._scope_channel_combo, 2, 1)
        scope_layout.addWidget(QLabel("Probe"), 2, 2)
        self._scope_probe_combo = QComboBox()
        self._scope_probe_combo.addItems(("1", "10", "20", "50", "100", "500", "1000"))
        self._scope_probe_combo.setCurrentText("10")
        scope_layout.addWidget(self._scope_probe_combo, 2, 3)
        scope_layout.addWidget(QLabel("Coupling"), 2, 4)
        self._scope_coupling_combo = QComboBox()
        self._scope_coupling_combo.addItems(("DC", "AC", "GND"))
        scope_layout.addWidget(self._scope_coupling_combo, 2, 5)
        scope_layout.addWidget(QLabel("Bandwidth"), 2, 6)
        self._scope_bandwidth_combo = QComboBox()
        self._scope_bandwidth_combo.addItem("Full (70 MHz)", "OFF")
        self._scope_bandwidth_combo.addItem("20 MHz limit", "20M")
        scope_layout.addWidget(self._scope_bandwidth_combo, 2, 7)

        scope_layout.addWidget(QLabel("V/div"), 3, 0)
        self._scope_vertical_scale_input = QLineEdit("10")
        scope_layout.addWidget(self._scope_vertical_scale_input, 3, 1)
        scope_layout.addWidget(QLabel("Switch kHz"), 3, 2)
        self._scope_frequency_input = QLineEdit("500")
        scope_layout.addWidget(self._scope_frequency_input, 3, 3)
        scope_layout.addWidget(QLabel("Cycles"), 3, 4)
        self._scope_cycles_combo = QComboBox()
        self._scope_cycles_combo.addItems(("2", "3", "4", "5"))
        self._scope_cycles_combo.setCurrentText("5")
        scope_layout.addWidget(self._scope_cycles_combo, 3, 5)
        calculate_timebase = QPushButton("Calculate time/div")
        calculate_timebase.clicked.connect(self._calculate_scope_timebase)
        scope_layout.addWidget(calculate_timebase, 3, 6, 1, 2)

        scope_layout.addWidget(QLabel("Time/div (s)"), 4, 0)
        self._scope_time_scale_input = QLineEdit("1e-6")
        scope_layout.addWidget(self._scope_time_scale_input, 4, 1)
        scope_layout.addWidget(QLabel("Memory"), 4, 2)
        self._scope_memory_combo = QComboBox()
        self._scope_memory_combo.addItems(("100k", "1M", "5M", "10M", "25M"))
        self._scope_memory_combo.setCurrentText("1M")
        self._scope_memory_combo.currentTextChanged.connect(self._scope_memory_changed)
        scope_layout.addWidget(self._scope_memory_combo, 4, 3)
        scope_layout.addWidget(QLabel("Points"), 4, 4)
        self._scope_points_input = QLineEdit("1000000")
        scope_layout.addWidget(self._scope_points_input, 4, 5)
        scope_layout.addWidget(QLabel("Acquisition"), 4, 6)
        self._scope_acquisition_combo = QComboBox()
        self._scope_acquisition_combo.addItem("Normal", "NORMal")
        self._scope_acquisition_combo.addItem("Peak detect", "PEAK")
        self._scope_acquisition_combo.addItem("Ultra acquire", "ULTRa")
        scope_layout.addWidget(self._scope_acquisition_combo, 4, 7)

        scope_layout.addWidget(QLabel("Trigger edge"), 5, 0)
        self._scope_trigger_slope_combo = QComboBox()
        self._scope_trigger_slope_combo.addItem("Rising", "POSitive")
        self._scope_trigger_slope_combo.addItem("Falling", "NEGative")
        self._scope_trigger_slope_combo.addItem("Either", "RFALl")
        scope_layout.addWidget(self._scope_trigger_slope_combo, 5, 1)
        scope_layout.addWidget(QLabel("Level (V)"), 5, 2)
        self._scope_trigger_level_input = QLineEdit("-24")
        scope_layout.addWidget(self._scope_trigger_level_input, 5, 3)
        scope_layout.addWidget(QLabel("Timeout (s)"), 5, 4)
        self._scope_trigger_timeout_input = QLineEdit("30")
        scope_layout.addWidget(self._scope_trigger_timeout_input, 5, 5)
        self._scope_apply_button = QPushButton("Apply setup")
        self._scope_apply_button.clicked.connect(self._apply_scope_setup)
        scope_layout.addWidget(self._scope_apply_button, 5, 6)
        self._scope_capture_button = QPushButton("Single capture…")
        self._scope_capture_button.clicked.connect(self._capture_scope_waveform)
        scope_layout.addWidget(self._scope_capture_button, 5, 7)

        scope_layout.addWidget(QLabel("Logging goal"), 6, 0)
        self._scope_logging_fidelity_combo = QComboBox()
        self._scope_logging_fidelity_combo.addItem(
            "Maximum coverage (100 samples/cycle)", "coverage"
        )
        self._scope_logging_fidelity_combo.addItem(
            "Balanced (250 samples/cycle)", "balanced"
        )
        self._scope_logging_fidelity_combo.addItem(
            "Edge/ringing detail (maximum rate)", "edge"
        )
        self._scope_logging_fidelity_combo.setCurrentIndex(1)
        self._scope_logging_fidelity_combo.setToolTip(
            "Choose the minimum waveform detail that the optimiser must preserve"
        )
        scope_layout.addWidget(self._scope_logging_fidelity_combo, 6, 1, 1, 4)
        self._scope_optimise_button = QPushButton("Optimise logging coverage")
        self._scope_optimise_button.setToolTip(
            "Use the selected point count to maximise captured time at the chosen fidelity"
        )
        self._scope_optimise_button.clicked.connect(self._optimise_scope_logging)
        scope_layout.addWidget(self._scope_optimise_button, 6, 5, 1, 3)

        self._scope_logging_estimate_label = QLabel(
            "Choose a logging goal, then optimise to preview span, cycles and payload."
        )
        self._scope_logging_estimate_label.setWordWrap(True)
        self._scope_logging_estimate_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        scope_layout.addWidget(self._scope_logging_estimate_label, 7, 0, 1, 8)

        scope_layout.addWidget(
            QLabel(
                "Capture transfer: single trigger → wait for STOP → RAW WORD + JSON preamble; "
                "select Repeated RAW waveforms above for continuous capture logging"
            ),
            8,
            0,
            1,
            8,
        )
        self._scope_section = CollapsibleSection("Oscilloscope Setup", scope_content)
        self._scope_section.set_expanded(False)
        layout.addWidget(self._scope_section)

        self._output = QTextEdit()
        self._output.setReadOnly(True)
        self._output.setMinimumHeight(90)
        self._output.document().setMaximumBlockCount(2000)
        layout.addWidget(self._output, stretch=1)

    @property
    def is_connected(self) -> bool:
        return bool(self._transport and self._transport.is_open)

    @property
    def is_running(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    @property
    def logging_enabled(self) -> bool:
        return self._logging_checkbox.isChecked()

    @property
    def is_waveform_logging(self) -> bool:
        return isinstance(self._worker, RepeatedWaveformCaptureWorker) and self.is_running

    def _waveform_logging_selected(self) -> bool:
        return self._acquisition_mode_combo.currentData() == "waveforms"

    def _on_acquisition_mode_changed(self, _index: int) -> None:
        self._interval_label.setText(
            "Min interval (ms)" if self._waveform_logging_selected() else "Interval (ms)"
        )
        self._refresh_controls()

    def _selected_instrument(self) -> InstrumentType:
        return InstrumentType(self._instrument_combo.currentText())

    def _selected_profile(self) -> InstrumentProfile:
        return INSTRUMENT_PROFILES[self._selected_instrument()]

    def _on_instrument_changed(self, _index: int) -> None:
        if self.is_connected:
            return
        self._reload_profile()
        self._refresh_sink()

    def _reload_profile(self) -> None:
        profile = self._selected_profile()
        has_instrument = profile.instrument != InstrumentType.NONE
        is_scope = profile.instrument == InstrumentType.RIGOL_DHO804
        self._scope_setup_applied = None
        with QSignalBlocker(self._acquisition_mode_combo):
            self._acquisition_mode_combo.setCurrentIndex(0)
        self._interval_label.setText("Interval (ms)")
        logging_was_enabled = self._logging_checkbox.isChecked()
        with QSignalBlocker(self._logging_checkbox):
            self._logging_checkbox.setChecked(False)
        if logging_was_enabled:
            self._logging_selection_sink(self.instrument_index, False)
        self.setTitle(profile.instrument.value if has_instrument else "No Instrument Selected")
        self._clear_measurement_rows()
        if profile.commands:
            self._add_measurement_row(next(iter(profile.commands)), self._first_source(profile))
        is_serial = profile.connection_kind == ConnectionKind.SERIAL
        self._endpoint_label.setText("Serial port" if is_serial else "VISA resource")
        self._endpoint_label.setVisible(has_instrument)
        self._endpoint_combo.setVisible(has_instrument)
        self._baud_label.setVisible(has_instrument and is_serial)
        self._baud_combo.setVisible(has_instrument and is_serial)
        self._ending_label.setVisible(has_instrument and profile.is_raw_serial)
        self._ending_combo.setVisible(has_instrument and profile.is_raw_serial)
        self._refresh_button.setVisible(has_instrument)
        self._connect_button.setVisible(has_instrument)
        self._interval_label.setVisible(has_instrument and not profile.is_raw_serial)
        self._interval_input.setVisible(has_instrument and not profile.is_raw_serial)
        self._acquisition_mode_label.setVisible(is_scope)
        self._acquisition_mode_combo.setVisible(is_scope)
        self._idn_button.setVisible(has_instrument and profile.supports_identity_query)
        self._measurement_section.setVisible(has_instrument)
        self._logging_checkbox.setVisible(has_instrument)
        self._scope_section.setVisible(is_scope)
        self._status_label.setText("Disconnected" if has_instrument else "Select an instrument profile")
        if has_instrument:
            self._append_output(f"Loaded profile: {profile.instrument.value}.")
        self._refresh_controls()

    @staticmethod
    def _first_source(profile: InstrumentProfile) -> str:
        return profile.sources[0] if profile.sources else ""

    def refresh_endpoints(self, serial_ports: list[str], visa_resources: list[str]) -> None:
        selected = self._endpoint_combo.currentText().strip()
        endpoints = (
            serial_ports
            if self._selected_profile().connection_kind == ConnectionKind.SERIAL
            else visa_resources
        )
        self._endpoint_combo.clear()
        self._endpoint_combo.addItems(endpoints)
        if selected:
            if selected not in endpoints:
                self._endpoint_combo.addItem(selected)
            self._endpoint_combo.setCurrentText(selected)
        elif endpoints:
            self._endpoint_combo.setCurrentIndex(0)

    def _clear_measurement_rows(self) -> None:
        for row in self._measurement_rows:
            self._measurement_rows_layout.removeWidget(row.container)
            row.container.deleteLater()
        self._measurement_rows.clear()

    def _add_measurement_row(self, function: MeasurementFunction, source: str) -> None:
        profile = self._selected_profile()
        container = QWidget()
        row_layout = QHBoxLayout(container)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(5)

        function_combo = QComboBox()
        function_combo.addItems([item.value for item in profile.commands])
        function_combo.setCurrentText(function.value)
        row_layout.addWidget(function_combo, stretch=2)

        source_combo = QComboBox()
        source_combo.addItems(profile.sources or ("—",))
        source_combo.setCurrentText(source or "—")
        source_combo.setEnabled(bool(profile.sources))
        row_layout.addWidget(source_combo, stretch=1)

        latest_label = QLabel("--")
        latest_label.setMinimumWidth(100)
        latest_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row_layout.addWidget(latest_label, stretch=2)

        remove_button = QPushButton("Remove")
        row_layout.addWidget(remove_button)

        row = MeasurementRow(
            container=container,
            function_combo=function_combo,
            source_combo=source_combo,
            latest_label=latest_label,
            remove_button=remove_button,
            last_valid_function=function,
            last_valid_source=source,
        )
        function_combo.currentIndexChanged.connect(lambda _i, item=row: self._row_changed(item))
        source_combo.currentIndexChanged.connect(lambda _i, item=row: self._row_changed(item))
        remove_button.clicked.connect(lambda: self._remove_measurement_row(row))
        self._measurement_rows.append(row)
        self._measurement_rows_layout.addWidget(container)

    def _measurement_key(self, row: MeasurementRow) -> tuple[MeasurementFunction, str]:
        source = row.source_combo.currentText() if self._selected_profile().sources else ""
        return MeasurementFunction(row.function_combo.currentText()), source

    def _row_changed(self, row: MeasurementRow) -> None:
        function, source = self._measurement_key(row)
        duplicate = any(
            self._measurement_key(other) == (function, source)
            for other in self._measurement_rows
            if other is not row
        )
        if duplicate:
            with QSignalBlocker(row.function_combo), QSignalBlocker(row.source_combo):
                row.function_combo.setCurrentText(row.last_valid_function.value)
                row.source_combo.setCurrentText(row.last_valid_source or "—")
            QMessageBox.warning(self, "Duplicate Measurement", "Each measurement/source pair must be unique.")
            return
        row.last_valid_function = function
        row.last_valid_source = source

    def _next_available_measurement(self) -> tuple[MeasurementFunction, str] | None:
        profile = self._selected_profile()
        used = {self._measurement_key(row) for row in self._measurement_rows}
        sources = profile.sources or ("",)
        for source in sources:
            for function in profile.commands:
                if (function, source) not in used:
                    return function, source
        return None

    def _add_measurement(self) -> None:
        if self.is_running:
            return
        next_measurement = self._next_available_measurement()
        if next_measurement is None or len(self._measurement_rows) >= self._selected_profile().maximum_rows:
            QMessageBox.information(self, "No Additional Measurement", "All available measurement slots are in use.")
            return
        self._add_measurement_row(*next_measurement)
        self._refresh_controls()

    def _remove_measurement_row(self, row: MeasurementRow) -> None:
        if self.is_running or len(self._measurement_rows) <= 1 or row not in self._measurement_rows:
            return
        self._measurement_rows.remove(row)
        self._measurement_rows_layout.removeWidget(row.container)
        row.container.deleteLater()
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        profile = self._selected_profile()
        connected = self.is_connected
        running = self.is_running
        waveform_mode = (
            profile.instrument == InstrumentType.RIGOL_DHO804
            and self._waveform_logging_selected()
        )
        waveform_ready = (
            self._scope_safety_checkbox.isChecked() and self._scope_setup_applied is not None
        )
        self._start_button.setEnabled(
            connected and not running and (not waveform_mode or waveform_ready)
        )
        self._stop_button.setEnabled(running)
        self._snapshot_button.setEnabled(
            connected and not running and not profile.is_raw_serial and not waveform_mode
        )
        self._add_button.setEnabled(
            not running
            and not profile.is_raw_serial
            and not waveform_mode
            and len(self._measurement_rows) < profile.maximum_rows
            and self._next_available_measurement() is not None
        )
        for row in self._measurement_rows:
            row.function_combo.setEnabled(
                not running and not profile.is_raw_serial and not waveform_mode
            )
            row.source_combo.setEnabled(
                not running and bool(profile.sources) and not waveform_mode
            )
            row.remove_button.setEnabled(
                not running and not waveform_mode and len(self._measurement_rows) > 1
            )
        self._acquisition_mode_combo.setEnabled(not running)
        self._logging_checkbox.setEnabled(not running)
        is_scope = profile.instrument == InstrumentType.RIGOL_DHO804
        safety_verified = self._scope_safety_checkbox.isChecked()
        self._scope_apply_button.setEnabled(is_scope and connected and not running and safety_verified)
        self._scope_optimise_button.setEnabled(is_scope and not running)
        self._scope_capture_button.setEnabled(
            is_scope
            and connected
            and not running
            and safety_verified
            and self._scope_setup_applied is not None
        )

    def _calculate_scope_timebase(self) -> None:
        try:
            switching_hz = float(self._scope_frequency_input.text().strip()) * 1000
            cycles = int(self._scope_cycles_combo.currentText())
            if switching_hz <= 0:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "Timebase", "Switching frequency must be a positive number.")
            return
        seconds_per_division = cycles / (10 * switching_hz)
        self._scope_time_scale_input.setText(f"{seconds_per_division:.6g}")

    def _optimise_scope_logging(self) -> None:
        try:
            switching_hz = float(self._scope_frequency_input.text().strip()) * 1000
            waveform_points = int(self._scope_points_input.text().strip())
            plan = optimise_waveform_logging(
                switching_frequency_hz=switching_hz,
                waveform_points=waveform_points,
                fidelity=str(self._scope_logging_fidelity_combo.currentData()),
                only_selected_channel=self._scope_only_channel_checkbox.isChecked(),
            )
            maximum_points = DHO804_MEMORY_POINTS[self._scope_memory_combo.currentText()]
            if waveform_points > maximum_points:
                raise ValueError(
                    f"Waveform points ({waveform_points:,}) exceed selected memory depth "
                    f"({maximum_points:,})."
                )
        except (KeyError, TypeError, ValueError) as exc:
            QMessageBox.warning(
                self,
                "Logging coverage",
                str(exc) if str(exc) else "Switching frequency and points must be positive numbers.",
            )
            return

        self._scope_time_scale_input.setText(f"{plan.time_scale_seconds:.9g}")
        self._scope_acquisition_combo.setCurrentIndex(0)
        self._interval_input.setText("0")
        self._acquisition_mode_combo.setCurrentIndex(
            self._acquisition_mode_combo.findData("waveforms")
        )
        limitation = "; channel-count sample-rate limit applied" if plan.sample_rate_limited else ""
        estimate = (
            f"Plan: {plan.sample_rate_hz / 1e6:.3g} MSa/s, "
            f"{plan.samples_per_cycle:.0f} samples/cycle, "
            f"{plan.record_span_seconds * 1e3:.3g} ms / "
            f"{plan.cycles_per_capture:,.0f} cycles per capture, "
            f"{plan.payload_bytes / (1024 * 1024):.2f} MiB RAW WORD{limitation}. "
            "Apply setup; the app will then query the scope's actual sample rate."
        )
        self._scope_logging_estimate_label.setText(estimate)
        self._append_output(f"Logging coverage optimised. {estimate}")
        self._refresh_controls()

    def _scope_memory_changed(self, memory_depth: str) -> None:
        if memory_depth in DHO804_MEMORY_POINTS:
            self._scope_points_input.setText(str(DHO804_MEMORY_POINTS[memory_depth]))

    def _scope_setup_from_controls(self) -> OscilloscopeSetup:
        setup = OscilloscopeSetup(
            source=self._scope_channel_combo.currentText(),
            only_selected_channel=self._scope_only_channel_checkbox.isChecked(),
            probe_ratio=float(self._scope_probe_combo.currentText()),
            coupling=self._scope_coupling_combo.currentText(),
            bandwidth_limit=str(self._scope_bandwidth_combo.currentData()),
            vertical_scale_volts=float(self._scope_vertical_scale_input.text().strip()),
            time_scale_seconds=float(self._scope_time_scale_input.text().strip()),
            memory_depth=self._scope_memory_combo.currentText(),
            acquisition_type=str(self._scope_acquisition_combo.currentData()),
            trigger_slope=str(self._scope_trigger_slope_combo.currentData()),
            trigger_level_volts=float(self._scope_trigger_level_input.text().strip()),
            waveform_points=int(self._scope_points_input.text().strip()),
            trigger_timeout_seconds=float(self._scope_trigger_timeout_input.text().strip()),
        )
        if setup.trigger_timeout_seconds <= 0:
            raise ValueError("Trigger timeout must be positive.")
        build_dho804_setup_commands(setup)
        return setup

    def _apply_scope_setup(self) -> None:
        if not self._scpi or self._selected_instrument() != InstrumentType.RIGOL_DHO804:
            QMessageBox.warning(self, "Oscilloscope setup", "Connect a RIGOL DHO804 first.")
            return
        if not self._scope_safety_checkbox.isChecked():
            QMessageBox.warning(
                self,
                "Safety verification required",
                "Verify the scope ground connection and probe rating before applying setup.",
            )
            return
        try:
            setup = self._scope_setup_from_controls()
            for command in build_dho804_setup_commands(setup):
                self._scpi.write(command)
            self._scope_setup_applied = setup
            actual_sample_rate_hz: float | None = None
            try:
                actual_sample_rate_hz = float(self._scpi.query(":ACQuire:SRATe?"))
            except Exception:
                pass
            self._append_output(
                f"Scope setup applied: {setup.source}, {setup.probe_ratio:g}× probe, "
                f"{setup.time_scale_seconds:g} s/div, {setup.memory_depth}, "
                f"{setup.trigger_slope} edge at {setup.trigger_level_volts:g} V."
            )
            if actual_sample_rate_hz and actual_sample_rate_hz > 0:
                try:
                    switching_hz = float(self._scope_frequency_input.text().strip()) * 1000
                    if switching_hz <= 0:
                        raise ValueError
                    actual_span = setup.waveform_points / actual_sample_rate_hz
                    self._scope_logging_estimate_label.setText(
                        f"Applied actual: {actual_sample_rate_hz / 1e6:.3g} MSa/s, "
                        f"{actual_sample_rate_hz / switching_hz:.0f} samples/cycle, "
                        f"{actual_span * 1e3:.3g} ms / "
                        f"{actual_span * switching_hz:,.0f} cycles per capture, "
                        f"{setup.waveform_points * 2 / (1024 * 1024):.2f} MiB RAW WORD."
                    )
                except ValueError:
                    self._scope_logging_estimate_label.setText(
                        f"Applied actual sample rate: "
                        f"{actual_sample_rate_hz / 1e6:.3g} MSa/s."
                    )
        except Exception as exc:  # pragma: no cover - hardware dependency
            self._scope_setup_applied = None
            QMessageBox.critical(self, "Oscilloscope setup failed", str(exc))
        self._refresh_controls()

    def _capture_scope_waveform(self) -> None:
        if not self._scpi or self._selected_instrument() != InstrumentType.RIGOL_DHO804:
            QMessageBox.warning(self, "Waveform capture", "Connect a RIGOL DHO804 first.")
            return
        if self.is_running:
            QMessageBox.warning(self, "Waveform capture", "Stop the current acquisition first.")
            return
        try:
            current_setup = self._scope_setup_from_controls()
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, "Waveform capture", str(exc))
            return
        if self._scope_setup_applied != current_setup:
            QMessageBox.warning(
                self,
                "Apply setup first",
                "The scope settings have changed. Click Apply setup before capturing.",
            )
            return
        suggested = f"dho804_{current_setup.source.lower()}_{datetime.now():%Y%m%d_%H%M%S}.bin"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save RAW WORD waveform",
            suggested,
            "Binary waveform (*.bin);;All files (*)",
        )
        if not path:
            return
        if not path.lower().endswith(".bin"):
            path += ".bin"
        self._worker = WaveformCaptureWorker(
            scpi=self._scpi,
            clock=self._clock,
            instrument_index=self.instrument_index,
            connection=self._endpoint_combo.currentText().strip(),
            device_idn=self._device_idn,
            setup=current_setup,
            output_path=path,
            on_reading=lambda reading: self._event_sink("reading", self.instrument_index, reading),
            on_complete=lambda message: self._event_sink(
                "capture_complete", self.instrument_index, message
            ),
            on_error=lambda error: self._event_sink("error", self.instrument_index, error),
        )
        self._worker.start()
        self._append_output(
            f"Single trigger armed on {current_setup.source}; waiting up to "
            f"{current_setup.trigger_timeout_seconds:g} s for STOP."
        )
        self._refresh_controls()

    def _toggle_connection(self) -> None:
        if self.is_connected:
            self.disconnect_device()
        else:
            self.connect_device()

    def connect_device(self) -> bool:
        if self._selected_instrument() == InstrumentType.NONE:
            QMessageBox.information(self, "No instrument", "Select an instrument profile first.")
            return False
        endpoint = self._endpoint_combo.currentText().strip()
        if not endpoint:
            QMessageBox.warning(self, "Connection", "Select or enter a connection endpoint first.")
            return False
        profile = self._selected_profile()
        try:
            if profile.connection_kind == ConnectionKind.SERIAL:
                baud = int(self._baud_combo.currentText())
                timeout = 0.2 if profile.is_raw_serial else 1.0
                self._transport = SerialTransport(
                    SerialSettings(port=endpoint, baudrate=baud, timeout_seconds=timeout)
                )
                connection_description = f"{endpoint} @ {baud} baud"
            else:
                visa_timeout = (
                    60.0 if profile.instrument == InstrumentType.RIGOL_DHO804 else 2.0
                )
                self._transport = VisaTransport(
                    VisaSettings(resource_name=endpoint, timeout_seconds=visa_timeout)
                )
                connection_description = endpoint
            self._transport.open()
            self._scpi = None if profile.is_raw_serial else SCPIClient(self._transport)
            if profile.supports_identity_query:
                self._device_idn = self._query_device_idn(announce=False)
                if not self._validate_device_identity(profile):
                    self.disconnect_device()
                    return False
            else:
                self._device_idn = endpoint
            self._connect_button.setText("Disconnect")
            self._instrument_combo.setEnabled(False)
            self._endpoint_combo.setEnabled(False)
            self._baud_combo.setEnabled(False)
            self._ending_combo.setEnabled(False)
            self._status_label.setText(f"Connected: {connection_description}")
            self._append_output(f"Connected to {connection_description}. ID: {self._device_idn}")
            self._scope_setup_applied = None
            self._refresh_controls()
            return True
        except Exception as exc:  # pragma: no cover - hardware dependency
            self._close_transport()
            QMessageBox.critical(self, "Connection failed", str(exc))
            self._status_label.setText("Disconnected")
            self._refresh_controls()
            return False

    def _close_transport(self) -> None:
        if self._transport:
            try:
                self._transport.close()
            except Exception:
                pass
        self._transport = None
        self._scpi = None

    def disconnect_device(self, announce: bool = True) -> None:
        self.stop_acquisition(announce=announce)
        if self.is_running:
            if announce:
                QMessageBox.information(
                    self,
                    "Capture still stopping",
                    "The current waveform transfer must finish before disconnecting. "
                    "Try Disconnect again when the panel reports that logging has stopped.",
                )
            return
        self._close_transport()
        self._device_idn = "UNKNOWN"
        self._scope_setup_applied = None
        self._connect_button.setText("Connect")
        self._instrument_combo.setEnabled(True)
        self._endpoint_combo.setEnabled(True)
        self._baud_combo.setEnabled(True)
        self._ending_combo.setEnabled(True)
        self._status_label.setText("Disconnected")
        if announce:
            self._append_output("Disconnected.")
        self._refresh_controls()

    def _query_device_idn(self, announce: bool = True) -> str:
        if not self._scpi:
            return "UNKNOWN"
        try:
            response = self._scpi.query(self._selected_profile().idn_query).strip()
            result = response or "UNKNOWN"
            if announce:
                self._append_output(f"*IDN? -> {result}")
            return result
        except Exception as exc:  # pragma: no cover - hardware dependency
            if announce:
                self._append_output(f"*IDN? failed: {exc}")
            return "UNKNOWN"

    def _request_idn(self) -> None:
        if not self._scpi:
            QMessageBox.warning(self, "Not connected", "Connect to the instrument first.")
            return
        self._device_idn = self._query_device_idn()

    def _validate_device_identity(self, profile: InstrumentProfile) -> bool:
        if self._device_idn != "UNKNOWN" and idn_matches_profile(profile, self._device_idn):
            return True
        expected = ", ".join(profile.idn_expected_tokens)
        QMessageBox.critical(
            self,
            "Instrument identity mismatch",
            f"Expected {profile.instrument.value} (ID containing {expected}).\nReceived: {self._device_idn}",
        )
        return False

    def _build_poll_requests(self) -> tuple[list[PollRequest], list[str]]:
        profile = self._selected_profile()
        requests: list[PollRequest] = []
        setup_commands: list[str] = []
        for slot_index, row in enumerate(self._measurement_rows):
            function, source = self._measurement_key(row)
            command = profile.commands[function]
            requests.append(
                PollRequest(
                    slot_index=slot_index,
                    function=function,
                    query_command=command.query_for_source(source),
                    unit=command.unit,
                    source=source,
                )
            )
            setup_commands.extend(command.prepare_commands)
        return requests, list(dict.fromkeys(setup_commands))

    def _validated_interval_seconds(self) -> float | None:
        try:
            interval_ms = int(self._interval_input.text().strip())
            if self._waveform_logging_selected():
                if not 0 <= interval_ms <= 60_000:
                    raise ValueError
            elif not 200 <= interval_ms <= 60_000:
                raise ValueError
            return interval_ms / 1000.0
        except ValueError:
            valid_range = "0 to 60000" if self._waveform_logging_selected() else "200 to 60000"
            QMessageBox.critical(
                self,
                "Interval",
                f"Interval must be an integer from {valid_range} ms. "
                "For waveform logging, 0 means re-arm as soon as transfer permits.",
            )
            return None

    def start_acquisition(
        self,
        _checked: bool = False,
        start_gate: threading.Event | None = None,
        announce: bool = True,
    ) -> bool:
        if not self.is_connected:
            if announce:
                QMessageBox.warning(self, "Not connected", "Connect this instrument before starting.")
            return False
        if self.is_running:
            return True
        profile = self._selected_profile()
        endpoint = self._endpoint_combo.currentText().strip()
        repeated_output_directory: str | None = None
        if profile.is_raw_serial:
            self._worker = RawSerialWorker(
                transport=self._transport,
                clock=self._clock,
                instrument_index=self.instrument_index,
                connection=endpoint,
                terminator=LINE_ENDINGS[self._ending_combo.currentText()],
                on_reading=lambda reading: self._event_sink("reading", self.instrument_index, reading),
                on_error=lambda error: self._event_sink("error", self.instrument_index, error),
                start_gate=start_gate,
            )
        elif profile.instrument == InstrumentType.RIGOL_DHO804 and self._waveform_logging_selected():
            interval_seconds = self._validated_interval_seconds()
            if interval_seconds is None:
                return False
            try:
                current_setup = self._scope_setup_from_controls()
            except (TypeError, ValueError) as exc:
                QMessageBox.warning(self, "Waveform logging", str(exc))
                return False
            if self._scope_setup_applied != current_setup:
                QMessageBox.warning(
                    self,
                    "Apply setup first",
                    "The scope settings have changed. Click Apply setup before starting waveform logging.",
                )
                return False
            output_directory = self._waveform_output_directory_provider(self.instrument_index)
            if not output_directory:
                QMessageBox.warning(
                    self,
                    "Waveform log destination",
                    "Choose a CSV log file and enable logging for this instrument, or enable "
                    "Shared CSV log, before starting repeated waveform capture.",
                )
                return False
            repeated_output_directory = output_directory
            self._worker = RepeatedWaveformCaptureWorker(
                scpi=self._scpi,
                clock=self._clock,
                instrument_index=self.instrument_index,
                connection=endpoint,
                device_idn=self._device_idn,
                setup=current_setup,
                output_directory=output_directory,
                interval_seconds=interval_seconds,
                on_reading=lambda reading: self._event_sink(
                    "reading", self.instrument_index, reading
                ),
                on_complete=lambda message: self._event_sink(
                    "capture_complete", self.instrument_index, message
                ),
                on_error=lambda error: self._event_sink("error", self.instrument_index, error),
                on_progress=lambda progress: self._event_sink(
                    "capture_progress", self.instrument_index, progress
                ),
                start_gate=start_gate,
            )
        else:
            interval_seconds = self._validated_interval_seconds()
            if interval_seconds is None:
                return False
            try:
                requests, setup_commands = self._build_poll_requests()
                for command in setup_commands:
                    self._scpi.write(command)
            except Exception as exc:  # pragma: no cover - hardware dependency
                QMessageBox.critical(self, "Configuration failed", str(exc))
                return False
            self._worker = PollingWorker(
                scpi=self._scpi,
                clock=self._clock,
                instrument_index=self.instrument_index,
                connection=endpoint,
                instrument=profile.instrument,
                device_idn=self._device_idn,
                measurements=requests,
                interval_seconds=interval_seconds,
                on_reading=lambda reading: self._event_sink("reading", self.instrument_index, reading),
                on_error=lambda error: self._event_sink("error", self.instrument_index, error),
                start_gate=start_gate,
            )
        self._worker.start()
        if profile.is_raw_serial:
            message = "Listening started."
        elif isinstance(self._worker, RepeatedWaveformCaptureWorker):
            message = f"Repeated RAW waveform logging started. Files: {repeated_output_directory}"
        else:
            message = "Polling started."
        self._append_output(message)
        self._refresh_controls()
        return True

    def stop_acquisition(self, _checked: bool = False, announce: bool = True) -> None:
        if self._worker and self._worker.is_alive():
            self._worker.stop()
            self._worker.join(timeout=1.5)
            if isinstance(self._worker, RepeatedWaveformCaptureWorker) and self._worker.is_alive():
                if announce:
                    self._append_output(
                        "Stop requested; waiting for the current waveform transfer to finish."
                    )
                self._refresh_controls()
                return
            if announce:
                self._append_output("Acquisition stopped.")
        self._worker = None
        self._refresh_controls()

    def take_snapshot(self, _checked: bool = False) -> None:
        if not self._scpi:
            QMessageBox.warning(self, "Not connected", "Connect to this instrument first.")
            return
        try:
            requests, setup_commands = self._build_poll_requests()
            for command in setup_commands:
                self._scpi.write(command)
            profile = self._selected_profile()
            endpoint = self._endpoint_combo.currentText().strip()
            for request in requests:
                raw = self._scpi.query(request.query_command)
                timestamp, elapsed_seconds, acquisition_run = self._clock.capture()
                self._event_sink(
                    "reading",
                    self.instrument_index,
                    Reading(
                        timestamp=timestamp,
                        elapsed_seconds=elapsed_seconds,
                        acquisition_run=acquisition_run,
                        instrument_index=self.instrument_index,
                        slot_index=request.slot_index,
                        instrument=profile.instrument,
                        connection=endpoint,
                        device_idn=self._device_idn,
                        function=request.function,
                        source=request.source,
                        raw_response=raw,
                        value=parse_primary_value(raw),
                        unit=request.unit,
                    ),
                )
        except Exception as exc:  # pragma: no cover - hardware dependency
            QMessageBox.critical(self, "Snapshot failed", str(exc))

    def consume_reading(self, reading: Reading) -> None:
        if reading.function == MeasurementFunction.RAW_DATA:
            display = reading.raw_response
        elif reading.function == MeasurementFunction.WAVEFORM_CAPTURE:
            display = f"{int(reading.value or 0):,} points → {reading.raw_response}"
        else:
            display = reading.raw_response if reading.value is None else f"{reading.value:.7g} {reading.unit}"
        if 0 <= reading.slot_index < len(self._measurement_rows):
            self._measurement_rows[reading.slot_index].latest_label.setText(display)
        source = f" {reading.source}" if reading.source else ""
        self._append_output(
            f"{reading.timestamp.strftime('%H:%M:%S.%f')}  +{reading.elapsed_seconds:.6f}s | "
            f"{reading.function.value}{source}: {display}"
        )

    def handle_worker_error(self, error: object) -> None:
        self._append_output(f"Acquisition error: {error}")
        self.stop_acquisition(announce=False)

    def handle_capture_complete(self, message: object) -> None:
        self._append_output(str(message))
        self._worker = None
        self._refresh_controls()

    def handle_capture_progress(self, progress: WaveformCaptureProgress) -> None:
        cadence = "measuring cadence"
        if progress.start_to_start_seconds:
            coverage = progress.coverage_percent or 0.0
            cadence = (
                f"{progress.start_to_start_seconds * 1e3:.3g} ms start-to-start, "
                f"{progress.blind_gap_seconds * 1e3:.3g} ms estimated blind gap, "
                f"{coverage:.2f}% coverage, "
                f"{progress.captures_per_minute:,.1f} captures/min, "
                f"{progress.gibibytes_per_hour:.2f} GiB/hour"
            )
        throughput = progress.transfer_mibibytes_per_second
        transfer = f"RAW transfer {progress.transfer_seconds * 1e3:.3g} ms"
        if throughput is not None:
            transfer += f" ({throughput:.2f} MiB/s)"
        self._scope_logging_estimate_label.setText(
            f"Capture {progress.capture_sequence:,}: "
            f"{progress.record_span_seconds * 1e3:.3g} ms record, {cadence}; "
            f"{transfer}, "
            f"file write {progress.file_write_seconds * 1e3:.3g} ms, "
            f"{progress.payload_bytes / (1024 * 1024):.2f} MiB."
        )

    def _append_output(self, text: str) -> None:
        self._output.append(text)


class DMMAppWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SCPI Lab Instrument Monitor")
        self.resize(1500, 920)
        self.setMinimumSize(1120, 720)
        self._clock = AcquisitionClock()
        self._events: queue.Queue[tuple[str, int, object]] = queue.Queue()
        self._logger: CsvLogger | None = None
        self._panels: list[InstrumentPanel] = []
        self._build_ui()
        self._refresh_endpoints()

        self._event_timer = QTimer(self)
        self._event_timer.setInterval(50)
        self._event_timer.timeout.connect(self._process_events)
        self._event_timer.start()

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        refresh = QPushButton("Refresh all connections")
        refresh.clicked.connect(self._refresh_endpoints)
        toolbar.addWidget(refresh)
        start_all = QPushButton("Start all connected")
        start_all.clicked.connect(self._start_all)
        toolbar.addWidget(start_all)
        stop_all = QPushButton("Stop all")
        stop_all.clicked.connect(self._stop_all)
        toolbar.addWidget(stop_all)
        toolbar.addSpacing(12)
        self._log_checkbox = QCheckBox("Shared CSV log")
        self._log_checkbox.setToolTip(
            "Log every instrument to the selected CSV, overriding individual panel selections"
        )
        self._log_checkbox.toggled.connect(self._toggle_shared_logging)
        toolbar.addWidget(self._log_checkbox)
        self._choose_log_button = QPushButton("Choose log file")
        self._choose_log_button.clicked.connect(self._choose_log_file)
        toolbar.addWidget(self._choose_log_button)
        self._log_path_label = QLabel("")
        self._log_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        toolbar.addWidget(self._log_path_label, stretch=1)
        self._elapsed_label = QLabel("Session +0.000 s")
        toolbar.addWidget(self._elapsed_label)
        layout.addLayout(toolbar)

        grid = QGridLayout()
        grid.setSpacing(8)
        initial_profiles = (InstrumentType.NONE,) * 4
        for index, instrument in enumerate(initial_profiles):
            panel = InstrumentPanel(
                instrument_index=index,
                clock=self._clock,
                event_sink=self._enqueue_event,
                refresh_sink=self._refresh_endpoints,
                logging_selection_sink=self._toggle_panel_logging,
                waveform_output_directory_provider=self._waveform_output_directory,
                initial_instrument=instrument,
                parent=root,
            )
            self._panels.append(panel)
            grid.addWidget(panel, index // 2, index % 2)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid, stretch=1)

    def _enqueue_event(self, kind: str, instrument_index: int, payload: object) -> None:
        self._events.put((kind, instrument_index, payload))

    def _refresh_endpoints(self) -> None:
        serial_ports = SerialTransport.list_serial_ports()
        visa_resources = VisaTransport.list_resources()
        for panel in self._panels:
            panel.refresh_endpoints(serial_ports, visa_resources)

    def _start_all(self) -> None:
        candidates = [panel for panel in self._panels if panel.is_connected and not panel.is_running]
        if not candidates:
            QMessageBox.information(self, "Start all", "Connect at least one stopped instrument first.")
            return
        if not any(panel.is_running for panel in self._panels):
            self._clock.reset()
        start_gate = threading.Event()
        try:
            for panel in candidates:
                panel.start_acquisition(start_gate=start_gate, announce=False)
        finally:
            start_gate.set()

    def _stop_all(self) -> None:
        for panel in self._panels:
            panel.stop_acquisition()

    def _process_events(self) -> None:
        pending: list[tuple[str, int, object]] = []
        while True:
            try:
                pending.append(self._events.get_nowait())
            except queue.Empty:
                break
        pending.sort(
            key=lambda event: event[2].elapsed_seconds
            if event[0] == "reading" and isinstance(event[2], Reading)
            else float("inf")
        )
        for kind, instrument_index, payload in pending:
            if not 0 <= instrument_index < len(self._panels):
                continue
            panel = self._panels[instrument_index]
            if kind == "reading" and isinstance(payload, Reading):
                panel.consume_reading(payload)
                if self._logger and self._should_log_instrument(instrument_index):
                    self._logger.write_reading(payload)
            elif kind == "error":
                panel.handle_worker_error(payload)
            elif kind == "capture_complete":
                panel.handle_capture_complete(payload)
            elif kind == "capture_progress" and isinstance(payload, WaveformCaptureProgress):
                panel.handle_capture_progress(payload)
        _, elapsed, acquisition_run = self._clock.capture()
        run_label = f"Run {acquisition_run}" if acquisition_run else "Session"
        self._elapsed_label.setText(f"{run_label} +{elapsed:.3f} s")
        waveform_logging = any(panel.is_waveform_logging for panel in self._panels)
        self._log_checkbox.setEnabled(not waveform_logging)
        self._choose_log_button.setEnabled(not waveform_logging)

    def _should_log_instrument(self, instrument_index: int) -> bool:
        return self._log_checkbox.isChecked() or self._panels[instrument_index].logging_enabled

    def _waveform_output_directory(self, instrument_index: int) -> str | None:
        log_path = self._log_path_label.text()
        if not log_path or not self._should_log_instrument(instrument_index):
            return None
        csv_path = Path(log_path)
        return str(
            csv_path.parent
            / f"{csv_path.stem}_waveforms"
            / f"instrument_{instrument_index + 1}"
        )

    def _logging_requested(self) -> bool:
        return self._log_checkbox.isChecked() or any(
            panel.logging_enabled for panel in self._panels
        )

    def _toggle_shared_logging(self, enabled: bool) -> None:
        if enabled and not self._ensure_log_file():
            with QSignalBlocker(self._log_checkbox):
                self._log_checkbox.setChecked(False)
        self._update_logger()

    def _toggle_panel_logging(self, instrument_index: int, enabled: bool) -> None:
        if enabled and not self._ensure_log_file():
            panel = self._panels[instrument_index]
            with QSignalBlocker(panel._logging_checkbox):
                panel._logging_checkbox.setChecked(False)
        self._update_logger()

    def _ensure_log_file(self) -> bool:
        if self._log_path_label.text():
            return True
        return self._choose_log_file()

    def _update_logger(self) -> None:
        if self._logging_requested() and self._log_path_label.text():
            if not self._logger:
                self._logger = CsvLogger(self._log_path_label.text())
        elif self._logger:
            self._logger.close()
            self._logger = None

    def _choose_log_file(self, _checked: bool = False) -> bool:
        path, _ = QFileDialog.getSaveFileName(self, "Choose shared log file", "", "CSV files (*.csv)")
        if not path:
            return False
        if not path.lower().endswith(".csv"):
            path += ".csv"
        if self._logger:
            self._logger.close()
            self._logger = None
        self._log_path_label.setText(path)
        self._update_logger()
        return True

    def closeEvent(self, event) -> None:  # noqa: N802
        for panel in self._panels:
            panel.disconnect_device(announce=False)
        if self._logger:
            self._logger.close()
            self._logger = None
        super().closeEvent(event)
