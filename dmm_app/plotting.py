from __future__ import annotations

import json
import math
import sys
import time
from array import array
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from dmm_app.models import MeasurementFunction, Reading


TRACE_COLORS = (QColor("#55aaff"), QColor("#ffad42"))
BACKGROUND = QColor("#101418")
PLOT_BACKGROUND = QColor("#151b20")
GRID_COLOR = QColor("#34404a")
TEXT_COLOR = QColor("#d8e0e7")
TRIGGER_COLOR = QColor("#ffd84d")
LIVE_COLOR = QColor("#ff3b30")
MAX_SCALAR_POINTS = 100_000


def format_engineering(value: float, unit: str = "") -> str:
    if not math.isfinite(value):
        return "--"
    prefixes = (
        (1e9, "G"),
        (1e6, "M"),
        (1e3, "k"),
        (1.0, ""),
        (1e-3, "m"),
        (1e-6, "µ"),
        (1e-9, "n"),
        (1e-12, "p"),
    )
    magnitude = abs(value)
    for scale, prefix in prefixes:
        if magnitude >= scale or scale == 1e-12:
            return f"{value / scale:.4g} {prefix}{unit}".strip()
    return f"{value:.4g} {unit}".strip()


@dataclass
class ScalarTrace:
    label: str
    unit: str
    points: deque[tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=MAX_SCALAR_POINTS)
    )

    def append(self, elapsed_seconds: float, value: float) -> None:
        self.points.append((elapsed_seconds, value))


@dataclass
class WaveformPlotData:
    words: array
    source: str
    x_increment: float
    x_origin: float
    x_reference: float
    y_increment: float
    y_origin: float
    y_reference: float
    time_scale_seconds: float
    vertical_scale_volts: float
    trigger_level_volts: float
    full_envelope: list[tuple[int, int, int]]

    @classmethod
    def load(cls, waveform_path: str, envelope_bins: int = 4096) -> "WaveformPlotData":
        path = Path(waveform_path)
        metadata_path = path.with_suffix(path.suffix + ".json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        raw = path.read_bytes()
        if not raw or len(raw) % 2:
            raise ValueError(f"Waveform payload must contain non-empty WORD data: {path}")

        words = array("H")
        words.frombytes(raw)
        if sys.byteorder != "little":
            words.byteswap()
        expected_points = int(metadata["word_points_received"])
        if len(words) != expected_points:
            raise ValueError(
                f"Waveform metadata reports {expected_points:,} points but the file "
                f"contains {len(words):,}."
            )

        preamble = metadata["preamble"]
        setup = metadata["setup"]
        full_envelope: list[tuple[int, int, int]] = []
        bin_count = min(envelope_bins, len(words))
        for bucket in range(bin_count):
            start = bucket * len(words) // bin_count
            stop = max(start + 1, (bucket + 1) * len(words) // bin_count)
            values = words[start:stop]
            full_envelope.append((start, min(values), max(values)))

        return cls(
            words=words,
            source=str(metadata["source"]),
            x_increment=float(preamble["x_increment"]),
            x_origin=float(preamble["x_origin"]),
            x_reference=float(preamble["x_reference"]),
            y_increment=float(preamble["y_increment"]),
            y_origin=float(preamble["y_origin"]),
            y_reference=float(preamble["y_reference"]),
            time_scale_seconds=float(setup["time_scale_seconds"]),
            vertical_scale_volts=float(setup["vertical_scale_volts"]),
            trigger_level_volts=float(setup["trigger_level_volts"]),
            full_envelope=full_envelope,
        )

    def time_at(self, index: int) -> float:
        return self.x_origin + (index - self.x_reference) * self.x_increment

    def voltage_for_word(self, word: int) -> float:
        return (word - self.y_origin - self.y_reference) * self.y_increment

    @property
    def record_start_seconds(self) -> float:
        return self.time_at(0)

    @property
    def record_stop_seconds(self) -> float:
        return self.time_at(len(self.words) - 1)

    def scope_view_indices(self) -> tuple[int, int]:
        left = -5.0 * self.time_scale_seconds
        right = 5.0 * self.time_scale_seconds
        start = math.floor((left - self.x_origin) / self.x_increment + self.x_reference)
        stop = math.ceil((right - self.x_origin) / self.x_increment + self.x_reference) + 1
        return max(0, start), min(len(self.words), stop)


class InstrumentPlotWidget(QWidget):
    """Lightweight plot supporting two scalar Y axes or one RAW scope waveform."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumHeight(90)
        self._traces: dict[int, ScalarTrace] = {}
        self._scalar_run: int | None = None
        self._scalar_window_seconds: float | None = 60.0
        self._waveform: WaveformPlotData | None = None
        self._waveform_full_record = False
        self._scope_profile = False
        self._stream_expected = False
        self._last_reading_monotonic: float | None = None
        self._stale_after_seconds = 2.5
        self._blink_on = True
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(450)
        self._live_timer.timeout.connect(self._advance_live_indicator)
        self._live_timer.start()
        self.setToolTip("A flashing red dot indicates that fresh readings are arriving.")

    @property
    def traces(self) -> dict[int, ScalarTrace]:
        return self._traces

    @property
    def waveform(self) -> WaveformPlotData | None:
        return self._waveform

    def configure_profile(self, scope_profile: bool) -> None:
        self._scope_profile = scope_profile
        self.clear()

    def clear(self) -> None:
        self._traces.clear()
        self._scalar_run = None
        self._waveform = None
        self.update()

    @property
    def data_is_live(self) -> bool:
        return bool(
            self._stream_expected
            and self._last_reading_monotonic is not None
            and time.monotonic() - self._last_reading_monotonic <= self._stale_after_seconds
        )

    def set_stream_expected(self, expected: bool, stale_after_seconds: float = 2.5) -> None:
        if not expected:
            self._last_reading_monotonic = None
            self._blink_on = False
        elif not self._stream_expected:
            self._last_reading_monotonic = None
            self._blink_on = True
        self._stream_expected = expected
        self._stale_after_seconds = max(0.1, stale_after_seconds)
        self.update()

    def note_live_reading(self) -> None:
        if self._stream_expected:
            self._last_reading_monotonic = time.monotonic()
            self._blink_on = True
            self.update()

    def _advance_live_indicator(self) -> None:
        self._blink_on = not self._blink_on if self.data_is_live else False
        self.update()

    def set_scalar_window(self, seconds: float | None) -> None:
        self._scalar_window_seconds = seconds
        self.update()

    def set_waveform_full_record(self, enabled: bool) -> None:
        self._waveform_full_record = enabled
        self.update()

    def add_reading(self, reading: Reading) -> None:
        if reading.function == MeasurementFunction.WAVEFORM_CAPTURE:
            self._waveform = WaveformPlotData.load(reading.raw_response)
            self.update()
            return
        if reading.value is None or not math.isfinite(reading.value):
            return
        self._waveform = None
        if self._scalar_run is not None and reading.acquisition_run != self._scalar_run:
            self._traces.clear()
        self._scalar_run = reading.acquisition_run
        axis = reading.slot_index
        if axis not in (0, 1):
            return
        source = f" {reading.source}" if reading.source else ""
        label = f"{reading.function.value}{source}"
        trace = self._traces.get(axis)
        if trace is None or trace.label != label or trace.unit != reading.unit:
            trace = ScalarTrace(label=label, unit=reading.unit)
            self._traces[axis] = trace
        trace.append(reading.elapsed_seconds, reading.value)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), BACKGROUND)
        plot = QRectF(64, 28, max(20, self.width() - 128), max(20, self.height() - 72))
        painter.fillRect(plot, PLOT_BACKGROUND)
        if self._waveform is not None and self._scope_profile:
            self._paint_waveform(painter, plot)
        else:
            self._paint_scalar(painter, plot)
        if self.data_is_live and self._blink_on:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(LIVE_COLOR)
            painter.drawEllipse(QPointF(self.width() - 14, 14), 5.5, 5.5)
        painter.end()

    def _paint_grid(self, painter: QPainter, plot: QRectF, x_divisions: int = 10) -> None:
        painter.setPen(QPen(GRID_COLOR, 1))
        for division in range(x_divisions + 1):
            x = plot.left() + plot.width() * division / x_divisions
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        for division in range(9):
            y = plot.top() + plot.height() * division / 8
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        painter.setPen(QPen(TEXT_COLOR, 1))
        painter.drawRect(plot)

    def _paint_scalar(self, painter: QPainter, plot: QRectF) -> None:
        self._paint_grid(painter, plot)
        traces = [(axis, self._traces[axis]) for axis in sorted(self._traces)[:2]]
        if not traces or not any(trace.points for _axis, trace in traces):
            self._paint_center_message(painter, plot, "No numeric readings yet")
            return

        x_max = max(point[0] for _axis, trace in traces for point in trace.points)
        if self._scalar_window_seconds is None:
            x_min = min(point[0] for _axis, trace in traces for point in trace.points)
        else:
            x_min = max(0.0, x_max - self._scalar_window_seconds)
        if x_max <= x_min:
            x_max = x_min + 1.0

        for division in range(11):
            x_value = x_min + (x_max - x_min) * division / 10
            if abs(x_value) < (x_max - x_min) * 1e-9:
                x_value = 0.0
            text = format_engineering(x_value, "s")
            x = plot.left() + plot.width() * division / 10
            painter.setPen(TEXT_COLOR)
            painter.drawText(QRectF(x - 38, plot.bottom() + 5, 76, 18), Qt.AlignHCenter, text)

        for axis, trace in traces:
            visible = [point for point in trace.points if point[0] >= x_min]
            if not visible:
                continue
            y_min, y_max = _padded_range([point[1] for point in visible])
            self._paint_y_axis(painter, plot, axis, trace, y_min, y_max)
            self._paint_scalar_trace(
                painter, plot, visible, x_min, x_max, y_min, y_max, TRACE_COLORS[axis]
            )

    def _paint_y_axis(
        self,
        painter: QPainter,
        plot: QRectF,
        axis: int,
        trace: ScalarTrace,
        y_min: float,
        y_max: float,
    ) -> None:
        color = TRACE_COLORS[axis]
        painter.setPen(color)
        legend = f"{trace.label} ({trace.unit or 'value'})"
        if axis == 0:
            painter.drawText(QRectF(plot.left(), 4, plot.width() / 2, 20), Qt.AlignLeft, legend)
        else:
            painter.drawText(
                QRectF(plot.center().x(), 4, plot.width() / 2, 20), Qt.AlignRight, legend
            )
        for division in range(9):
            value = y_max - (y_max - y_min) * division / 8
            y = plot.top() + plot.height() * division / 8
            label = format_engineering(value, trace.unit)
            if axis == 0:
                rect = QRectF(0, y - 9, plot.left() - 5, 18)
                alignment = Qt.AlignRight | Qt.AlignVCenter
            else:
                rect = QRectF(plot.right() + 5, y - 9, self.width() - plot.right() - 5, 18)
                alignment = Qt.AlignLeft | Qt.AlignVCenter
            painter.drawText(rect, alignment, label)

    @staticmethod
    def _paint_scalar_trace(
        painter: QPainter,
        plot: QRectF,
        points: list[tuple[float, float]],
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
        color: QColor,
    ) -> None:
        path = QPainterPath()
        for index, (x_value, y_value) in enumerate(points):
            x = plot.left() + (x_value - x_min) / (x_max - x_min) * plot.width()
            y = plot.bottom() - (y_value - y_min) / (y_max - y_min) * plot.height()
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.setPen(QPen(color, 1.6))
        painter.drawPath(path)

    def _paint_waveform(self, painter: QPainter, plot: QRectF) -> None:
        waveform = self._waveform
        if waveform is None:
            return
        self._paint_grid(painter, plot)
        if self._waveform_full_record:
            x_min = waveform.record_start_seconds
            x_max = waveform.record_stop_seconds
            samples = waveform.full_envelope
            heading = f"{waveform.source} — full RAW record"
        else:
            x_min = -5.0 * waveform.time_scale_seconds
            x_max = 5.0 * waveform.time_scale_seconds
            start, stop = waveform.scope_view_indices()
            samples = _envelope_for_range(waveform.words, start, stop, max(1, int(plot.width())))
            heading = (
                f"{waveform.source} — {format_engineering(waveform.time_scale_seconds, 's')}/div, "
                f"{format_engineering(waveform.vertical_scale_volts, 'V')}/div"
            )
        y_center = -waveform.y_origin * waveform.y_increment
        y_min = y_center - 4.0 * waveform.vertical_scale_volts
        y_max = y_center + 4.0 * waveform.vertical_scale_volts

        painter.setPen(TRACE_COLORS[0])
        painter.drawText(QRectF(plot.left(), 4, plot.width(), 20), Qt.AlignLeft, heading)
        for division in range(11):
            value = x_min + (x_max - x_min) * division / 10
            if abs(value) < (x_max - x_min) * 1e-9:
                value = 0.0
            x = plot.left() + plot.width() * division / 10
            painter.setPen(TEXT_COLOR)
            painter.drawText(
                QRectF(x - 40, plot.bottom() + 5, 80, 18),
                Qt.AlignHCenter,
                format_engineering(value, "s"),
            )
        for division in range(9):
            value = y_max - (y_max - y_min) * division / 8
            y = plot.top() + plot.height() * division / 8
            painter.drawText(
                QRectF(0, y - 9, plot.left() - 5, 18),
                Qt.AlignRight | Qt.AlignVCenter,
                format_engineering(value, "V"),
            )

        trigger_x = plot.left() + (0.0 - x_min) / (x_max - x_min) * plot.width()
        painter.setPen(QPen(TRIGGER_COLOR, 1, Qt.PenStyle.DashLine))
        if plot.left() <= trigger_x <= plot.right():
            painter.drawLine(QPointF(trigger_x, plot.top()), QPointF(trigger_x, plot.bottom()))
        trigger_y = plot.bottom() - (
            (waveform.trigger_level_volts - y_min) / (y_max - y_min) * plot.height()
        )
        if plot.top() <= trigger_y <= plot.bottom():
            painter.drawLine(QPointF(plot.left(), trigger_y), QPointF(plot.right(), trigger_y))

        painter.save()
        painter.setClipRect(plot)
        painter.setPen(QPen(TRACE_COLORS[0], 1.2))
        for index, low_word, high_word in samples:
            time_value = waveform.time_at(index)
            if time_value < x_min or time_value > x_max:
                continue
            x = plot.left() + (time_value - x_min) / (x_max - x_min) * plot.width()
            low = waveform.voltage_for_word(low_word)
            high = waveform.voltage_for_word(high_word)
            y_low = plot.bottom() - (low - y_min) / (y_max - y_min) * plot.height()
            y_high = plot.bottom() - (high - y_min) / (y_max - y_min) * plot.height()
            painter.drawLine(QPointF(x, y_low), QPointF(x, y_high))
        painter.restore()

    @staticmethod
    def _paint_center_message(painter: QPainter, plot: QRectF, message: str) -> None:
        painter.setPen(TEXT_COLOR)
        painter.drawText(plot, Qt.AlignCenter, message)


def _padded_range(values: list[float]) -> tuple[float, float]:
    low = min(values)
    high = max(values)
    if high == low:
        minimum_span = max(abs(high) * 0.02, 1e-9)
        return low - minimum_span, high + minimum_span
    padding = (high - low) * 0.08
    return low - padding, high + padding


def _envelope_for_range(
    words: array,
    start: int,
    stop: int,
    bins: int,
) -> list[tuple[int, int, int]]:
    count = max(0, stop - start)
    if count == 0:
        return []
    bin_count = min(bins, count)
    envelope: list[tuple[int, int, int]] = []
    for bucket in range(bin_count):
        bucket_start = start + bucket * count // bin_count
        bucket_stop = max(bucket_start + 1, start + (bucket + 1) * count // bin_count)
        values = words[bucket_start:bucket_stop]
        envelope.append((bucket_start, min(values), max(values)))
    return envelope
