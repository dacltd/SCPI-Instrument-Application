"""Read-only dashboard views fed by the existing instrument acquisition stream."""
from dataclasses import replace
import time

from PySide6.QtWidgets import (
    QComboBox, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QHeaderView,
)

from dmm_app.models import MeasurementFunction
from dmm_app.plotting import InstrumentPlotWidget


class OverviewCard(QGroupBox):
    def __init__(self, panel, open_instrument, parent=None):
        super().__init__(parent)
        self.panel = panel
        self._signature = None
        self._profile = None
        self._last_received = None
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self.status = QLabel()
        self.status.setWordWrap(True)
        header.addWidget(self.status, 1)
        open_button = QPushButton("Open instrument")
        open_button.clicked.connect(open_instrument)
        header.addWidget(open_button)
        layout.addLayout(header)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Measurement", "Latest"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setMinimumSectionSize(20)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setMaximumHeight(160)
        layout.addWidget(self.table)
        traces = QHBoxLayout()
        self.axes = []
        for title in ("Left", "Right"):
            traces.addWidget(QLabel(title))
            combo = QComboBox()
            combo.currentIndexChanged.connect(self._clear_plot)
            traces.addWidget(combo, 1)
            self.axes.append(combo)
        layout.addLayout(traces)
        self.plot = InstrumentPlotWidget()
        self.plot.setMinimumHeight(190)
        layout.addWidget(self.plot)
        self.sync()

    def _clear_plot(self):
        self.plot.clear()

    def sync(self):
        panel = self.panel
        self.setTitle(f"{panel.instrument_index + 1} · {panel._selected_instrument().value}")
        signature = tuple((row.function_combo.currentText(), row.source_combo.currentText())
                          for row in panel._measurement_rows)
        profile_state = (panel._selected_instrument(), panel._acquisition_mode_combo.currentData())
        if signature != self._signature or self._profile != profile_state:
            self._profile = profile_state
            self._signature = signature
            self._last_received = None
            self.plot.clear()
            self.table.setRowCount(len(signature))
            self.table.setFixedHeight(min(160, max(52, len(signature) * 22 + 26)))
            for index, (name, source) in enumerate(signature):
                label = f"{name} {source}".strip(" —")
                self.table.setItem(index, 0, QTableWidgetItem(label))
                self.table.setItem(index, 1, QTableWidgetItem("—"))
            for axis, combo in enumerate(self.axes):
                combo.blockSignals(True)
                combo.clear()
                combo.addItem("None", -1)
                for index, (name, source) in enumerate(signature):
                    combo.addItem(f"{name} {source}".strip(" —"), index)
                combo.setCurrentIndex(axis + 1 if len(signature) > axis else 0)
                combo.blockSignals(False)
        state = "Disconnected"
        if panel.is_connected:
            state = "Acquiring" if panel.is_running else "Connected · stopped"
        if panel.is_running and panel._worker.__class__.__name__ == "BatteryModelDownloadWorker":
            state = "Downloading model"
        logging = "Logging selected" if panel.logging_enabled else "Logging off"
        age = (f"Last received {time.monotonic() - self._last_received:.1f}s ago"
               if self._last_received is not None else "No readings received")
        self.status.setText(f"{state} · {logging}\n{age}")
        self.plot.set_stream_expected(panel.is_running)

    def consume(self, reading):
        self._last_received = time.monotonic()
        if reading.function == MeasurementFunction.WAVEFORM_CAPTURE:
            if self.table.rowCount():
                self.table.setItem(0, 0, QTableWidgetItem("Latest RAW waveform"))
                item = QTableWidgetItem(f"{int(reading.value or 0):,} points · view in instrument tab")
                item.setToolTip(reading.raw_response)
                self.table.setItem(0, 1, item)
            return
        if 0 <= reading.slot_index < self.table.rowCount():
            self.table.setItem(reading.slot_index, 0, QTableWidgetItem(
                f"{reading.function.value} {reading.source}".strip()
            ))
            if reading.value is None:
                value = "Unavailable" if reading.function != MeasurementFunction.RAW_DATA else reading.raw_response[:120]
            else:
                value = f"{reading.value:.7g} {reading.unit}".strip()
            item = QTableWidgetItem(value)
            item.setToolTip(reading.raw_response)
            self.table.setItem(reading.slot_index, 1, item)
        for axis, combo in enumerate(self.axes):
            if reading.slot_index >= 0 and combo.currentData() == reading.slot_index:
                self.plot.add_reading(replace(reading, slot_index=axis))
                self.plot.note_live_reading()
