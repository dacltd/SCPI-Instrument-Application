from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dmm_app.oscilloscope import DHO804_MEMORY_POINTS, OscilloscopeSetup, WaveformPreamble
from dmm_app.scpi import IEEEBinaryBlockError, SCPIClient


MAX_IEEE_ATTEMPTS = 3
MAX_DIAGNOSTIC_FRAMES = 1_000
MAX_DIAGNOSTIC_PAYLOAD_BYTES = 2 * 1024**3


@dataclass(frozen=True)
class ScopeToolsState:
    connected: bool
    acquisition_running: bool
    setup_applied: bool
    connection: str
    device_idn: str


@dataclass
class BurstFrameResult:
    frame: int
    hardware_timestamp: str
    points: int
    payload_bytes: int
    sha256: str
    result: str
    waveform_path: str
    metadata_path: str


@dataclass
class BurstDiagnosticSummary:
    started_at: str
    completed_at: str
    connection: str
    device_idn: str
    source: str
    requested_frames: int
    recorded_frames: int
    maximum_frames: int
    requested_frame_interval_seconds: float
    output_directory: str
    frames: list[BurstFrameResult]
    cancelled: bool = False


class ScopeCommandWorker(threading.Thread):
    def __init__(
        self,
        scpi: SCPIClient,
        command: str,
        query: bool,
        on_event: Callable[[str, object], None],
    ):
        super().__init__(daemon=True)
        self._scpi = scpi
        self._command = command
        self._query = query
        self._on_event = on_event
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            self._on_event("log", f"→ {self._command}")
            if self._query:
                response = self._scpi.query(self._command)
                self._on_event("log", f"← {response}")
            else:
                self._scpi.write(self._command)
                response = "Command accepted"
                self._on_event("log", "✓ write completed")
            if not self._stop_event.is_set():
                self._on_event("command_result", response)
        except Exception as exc:  # pragma: no cover - hardware error path
            if not self._stop_event.is_set():
                self._on_event("error", str(exc))


class BurstDiagnosticWorker(threading.Thread):
    """Record DHO frames internally, then verify and save every selected RAW frame."""

    def __init__(
        self,
        scpi: SCPIClient,
        setup: OscilloscopeSetup,
        connection: str,
        device_idn: str,
        requested_frames: int,
        frame_interval_seconds: float,
        record_timeout_seconds: float,
        output_parent: str,
        on_event: Callable[[str, object], None],
    ):
        super().__init__(daemon=True)
        self._scpi = scpi
        self._setup = setup
        self._connection = connection
        self._device_idn = device_idn
        self._requested_frames = requested_frames
        self._frame_interval_seconds = frame_interval_seconds
        self._record_timeout_seconds = record_timeout_seconds
        self._output_parent = Path(output_parent)
        self._on_event = on_event
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def _log(self, message: str) -> None:
        self._on_event("log", message)

    def _write(self, command: str) -> None:
        self._log(f"→ {command}")
        self._scpi.write(command)
        self._log("✓ write completed")

    def _query(self, command: str) -> str:
        self._log(f"→ {command}")
        response = self._scpi.query(command).strip()
        self._log(f"← {response}")
        return response

    def _query_int(self, command: str) -> int:
        return int(float(self._query(command)))

    def run(self) -> None:
        recording_enabled = False
        error: Exception | None = None
        summary: BurstDiagnosticSummary | None = None
        try:
            started = datetime.now().astimezone()
            output_directory = self._output_parent / f"dho804_burst_{started:%Y%m%dT%H%M%S_%f}"
            output_directory.mkdir(parents=True, exist_ok=False)

            self._on_event("status", "Preparing hardware waveform recording…")
            self._write(":STOP")
            self._write(":RECord:WRECord:ENABle ON")
            recording_enabled = True
            maximum_frames = self._query_int(":RECord:WRECord:FMAX?")
            self._on_event("maximum_frames", maximum_frames)
            if self._requested_frames > maximum_frames:
                raise ValueError(
                    f"Requested {self._requested_frames:,} frames, but the current "
                    f"configuration supports at most {maximum_frames:,}."
                )

            self._write(f":RECord:WRECord:FRAMes {self._requested_frames}")
            self._write(
                f":RECord:WRECord:FINTerval {self._frame_interval_seconds:.12g}"
            )
            self._write(":RECord:WRECord:PROMpt OFF")
            self._write(":RECord:WRECord:OPERate RUN")
            self._on_event(
                "status",
                f"Recording up to {self._requested_frames:,} frame(s) in scope memory…",
            )

            deadline = time.monotonic() + self._record_timeout_seconds
            while not self._stop_event.is_set():
                operation = self._query(":RECord:WRECord:OPERate?").upper()
                if operation == "STOP":
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Waveform recording did not reach STOP within "
                        f"{self._record_timeout_seconds:g} seconds."
                    )
                self._stop_event.wait(0.1)
            if self._stop_event.is_set():
                self._write(":RECord:WRECord:OPERate STOP")

            recorded_frames = self._query_int(":RECord:WREPlay:FMAX?")
            frames_to_read = min(self._requested_frames, recorded_frames)
            if frames_to_read < 1 and not self._stop_event.is_set():
                raise ValueError("The scope reports that no waveform-record frames are available.")

            self._write(":RECord:WREPlay:OPERate STOP")
            results: list[BurstFrameResult] = []
            observed_hashes: set[str] = set()
            for frame in range(1, frames_to_read + 1):
                if self._stop_event.is_set():
                    break
                self._on_event("status", f"Reading recorded frame {frame:,}/{frames_to_read:,}…")
                result = self._read_and_save_frame(frame, output_directory)
                if result.sha256 in observed_hashes:
                    result.result = "Unproven: duplicate payload"
                else:
                    observed_hashes.add(result.sha256)
                results.append(result)
                self._on_event("frame", result)

            completed = datetime.now().astimezone()
            summary = BurstDiagnosticSummary(
                started_at=started.isoformat(timespec="microseconds"),
                completed_at=completed.isoformat(timespec="microseconds"),
                connection=self._connection,
                device_idn=self._device_idn,
                source=self._setup.source,
                requested_frames=self._requested_frames,
                recorded_frames=recorded_frames,
                maximum_frames=maximum_frames,
                requested_frame_interval_seconds=self._frame_interval_seconds,
                output_directory=str(output_directory),
                frames=results,
                cancelled=self._stop_event.is_set(),
            )
            summary_path = output_directory / "diagnostic.json"
            summary_path.write_text(
                json.dumps(asdict(summary), indent=2),
                encoding="utf-8",
            )
            self._log(f"Saved diagnostic summary: {summary_path}")
        except Exception as exc:  # pragma: no cover - hardware and filesystem error path
            error = exc
        finally:
            if recording_enabled:
                for command in (
                    ":RECord:WRECord:OPERate STOP",
                    ":RECord:WREPlay:OPERate STOP",
                    ":RECord:WRECord:ENABle OFF",
                    ":STOP",
                ):
                    try:
                        self._write(command)
                    except Exception as cleanup_error:  # pragma: no cover - hardware path
                        self._log(f"Cleanup warning for {command}: {cleanup_error}")

        if error is not None:
            if not self._stop_event.is_set():
                self._on_event("error", str(error))
            else:
                self._on_event("done", "Burst diagnostic cancelled.")
        elif summary is not None:
            self._on_event("summary", summary)
            state = "cancelled" if summary.cancelled else "completed"
            self._on_event(
                "done",
                f"Burst diagnostic {state}: {len(summary.frames):,} frame(s) saved under "
                f"{summary.output_directory}",
            )

    def _select_frame(self, frame: int) -> tuple[str, WaveformPreamble]:
        self._write(f":RECord:WREPlay:FCURrent {frame}")
        selected = self._query_int(":RECord:WREPlay:FCURrent?")
        if selected != frame:
            raise ValueError(f"Requested recorded frame {frame}, but the scope selected {selected}.")
        hardware_timestamp = self._query(":RECord:WREPlay:FCURrent:TIME?")
        for command in (
            f":WAVeform:SOURce {self._setup.source}",
            ":WAVeform:MODE RAW",
            ":WAVeform:FORMat WORD",
            ":WAVeform:STARt 1",
        ):
            self._write(command)
        preamble = WaveformPreamble.parse(self._query(":WAVeform:PREamble?"))
        if preamble.format_code != 1 or preamble.mode_code != 2:
            raise ValueError(
                "Selected record frame did not expose WORD/RAW waveform data "
                f"(format={preamble.format_code}, mode={preamble.mode_code})."
            )
        self._write(f":WAVeform:STOP {preamble.points}")
        return hardware_timestamp, preamble

    def _read_and_save_frame(self, frame: int, output_directory: Path) -> BurstFrameResult:
        hardware_timestamp, preamble = self._select_frame(frame)
        payload: bytes | None = None
        for attempt in range(1, MAX_IEEE_ATTEMPTS + 1):
            try:
                self._log("→ :WAVeform:DATA?")
                payload = self._scpi.query_binary_block(":WAVeform:DATA?")
                self._log(f"← IEEE block: {len(payload):,} payload bytes")
                break
            except IEEEBinaryBlockError as exc:
                if attempt >= MAX_IEEE_ATTEMPTS:
                    raise
                self._log(
                    f"IEEE transfer failed for frame {frame} ({attempt}/{MAX_IEEE_ATTEMPTS}); "
                    f"clearing VISA and reselecting the frame: {exc}"
                )
                self._scpi.recover_binary_transfer()
                hardware_timestamp, preamble = self._select_frame(frame)
        if payload is None:
            raise ValueError(f"No RAW payload was returned for recorded frame {frame}.")
        if len(payload) % 2:
            raise IEEEBinaryBlockError(
                f"Recorded frame {frame} returned an odd WORD byte count ({len(payload):,})."
            )
        points = len(payload) // 2
        if points != preamble.points:
            raise IEEEBinaryBlockError(
                f"Recorded frame {frame} contains {points:,} points; "
                f"the preamble announced {preamble.points:,}."
            )

        digest = hashlib.sha256(payload).hexdigest()
        waveform_path = output_directory / f"frame_{frame:06d}.bin"
        metadata_path = waveform_path.with_suffix(".bin.json")
        waveform_path.write_bytes(payload)
        metadata = {
            "diagnostic": "DHO804 hardware waveform recording",
            "frame": frame,
            "hardware_timestamp": hardware_timestamp,
            "connection": self._connection,
            "device_idn": self._device_idn,
            "source": self._setup.source,
            "requested_frame_interval_seconds": self._frame_interval_seconds,
            "setup": asdict(self._setup),
            "preamble": asdict(preamble),
            "word_points_received": points,
            "raw_bytes": len(payload),
            "sha256": digest,
            "word_byte_order": "little-endian (validated on DHO804 firmware 00.01.03)",
        }
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return BurstFrameResult(
            frame=frame,
            hardware_timestamp=hardware_timestamp,
            points=points,
            payload_bytes=len(payload),
            sha256=digest,
            result="Pass",
            waveform_path=str(waveform_path),
            metadata_path=str(metadata_path),
        )


class DHO804ToolsDialog(QDialog):
    def __init__(
        self,
        scpi_provider: Callable[[], SCPIClient | None],
        state_provider: Callable[[], ScopeToolsState],
        setup_provider: Callable[[], OscilloscopeSetup | None],
        on_setup_invalidated: Callable[[str], None],
        on_busy_changed: Callable[[], None],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("DHO804 Tools")
        self.resize(820, 590)
        self.setModal(False)
        self._scpi_provider = scpi_provider
        self._state_provider = state_provider
        self._setup_provider = setup_provider
        self._on_setup_invalidated = on_setup_invalidated
        self._on_busy_changed = on_busy_changed
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._worker: ScopeCommandWorker | BurstDiagnosticWorker | None = None
        self._busy = False
        self._operation = ""
        self._operation_invalidates_setup = False
        self._results: BurstDiagnosticSummary | None = None
        self._status_override = ""
        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._process_events)
        self._timer.start()
        self._refresh_availability()

    @property
    def is_busy(self) -> bool:
        return self._busy

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        self._identity_label = QLabel("Disconnected")
        self._identity_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self._identity_label, stretch=1)
        self._status_label = QLabel("Idle")
        header.addWidget(self._status_label)
        layout.addLayout(header)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_recording_tab(), "Waveform recording")
        self._tabs.addTab(self._build_console_tab(), "SCPI console")
        self._tabs.addTab(self._build_log_tab(), "Command log")
        layout.addWidget(self._tabs, stretch=1)

        footer = QHBoxLayout()
        self._stop_button = QPushButton("Stop operation")
        self._stop_button.clicked.connect(self._stop_operation)
        footer.addWidget(self._stop_button)
        footer.addStretch(1)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        footer.addWidget(close_button)
        layout.addLayout(footer)

    def _build_recording_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        form = QFormLayout()

        maximum_row = QHBoxLayout()
        self._maximum_frames_label = QLabel("--")
        maximum_row.addWidget(self._maximum_frames_label, stretch=1)
        self._query_maximum_button = QPushButton("Query maximum")
        self._query_maximum_button.clicked.connect(self._query_maximum_frames)
        maximum_row.addWidget(self._query_maximum_button)
        form.addRow("Maximum frames", maximum_row)

        self._frame_count_input = QLineEdit("10")
        form.addRow("Test frames", self._frame_count_input)
        self._frame_interval_input = QLineEdit("1e-8")
        self._frame_interval_input.setToolTip("Requested recording interval in seconds (10 ns to 1 s)")
        form.addRow("Frame interval (s)", self._frame_interval_input)
        self._record_timeout_input = QLineEdit("60")
        form.addRow("Record timeout (s)", self._record_timeout_input)

        output_row = QHBoxLayout()
        self._output_directory_input = QLineEdit("")
        self._output_directory_input.setPlaceholderText("Choose a parent directory for diagnostic files")
        output_row.addWidget(self._output_directory_input, stretch=1)
        choose_output = QPushButton("Choose…")
        choose_output.clicked.connect(self._choose_output_directory)
        output_row.addWidget(choose_output)
        form.addRow("Output directory", output_row)
        layout.addLayout(form)

        actions = QHBoxLayout()
        self._run_diagnostic_button = QPushButton("Run capability test")
        self._run_diagnostic_button.clicked.connect(self._run_diagnostic)
        actions.addWidget(self._run_diagnostic_button)
        self._export_button = QPushButton("Export results…")
        self._export_button.clicked.connect(self._export_results)
        actions.addWidget(self._export_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._results_table = QTableWidget(0, 6)
        self._results_table.setHorizontalHeaderLabels(
            ("Frame", "Hardware timestamp", "Points", "Bytes", "SHA-256", "Result")
        )
        header = self._results_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._results_table, stretch=1)
        return tab

    def _build_console_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        warning = QLabel(
            "Expert tool: commands can alter the scope. Console access is disabled during "
            "acquisition. Binary waveform queries must use the recording diagnostic."
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        command_row = QHBoxLayout()
        self._console_command_input = QLineEdit("*IDN?")
        self._console_command_input.returnPressed.connect(self._console_query)
        command_row.addWidget(self._console_command_input, stretch=1)
        self._console_query_button = QPushButton("Query")
        self._console_query_button.clicked.connect(self._console_query)
        command_row.addWidget(self._console_query_button)
        self._console_write_button = QPushButton("Write")
        self._console_write_button.clicked.connect(self._console_write)
        command_row.addWidget(self._console_write_button)
        layout.addLayout(command_row)
        self._console_response = QPlainTextEdit()
        self._console_response.setReadOnly(True)
        layout.addWidget(self._console_response, stretch=1)
        return tab

    def _build_log_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self._command_log = QPlainTextEdit()
        self._command_log.setReadOnly(True)
        self._command_log.document().setMaximumBlockCount(5000)
        layout.addWidget(self._command_log)
        clear_button = QPushButton("Clear command log")
        clear_button.clicked.connect(self._command_log.clear)
        layout.addWidget(clear_button, alignment=Qt.AlignRight)
        return tab

    def _state(self) -> ScopeToolsState:
        try:
            return self._state_provider()
        except RuntimeError:
            return ScopeToolsState(False, False, False, "", "UNKNOWN")

    def _refresh_availability(self) -> None:
        state = self._state()
        if state.connected:
            self._identity_label.setText(f"{state.device_idn} — {state.connection}")
        else:
            self._identity_label.setText("DHO804 disconnected")
        available = state.connected and not state.acquisition_running and not self._busy
        self._query_maximum_button.setEnabled(available)
        self._run_diagnostic_button.setEnabled(available and state.setup_applied)
        self._console_query_button.setEnabled(available)
        self._console_write_button.setEnabled(available)
        self._stop_button.setEnabled(self._busy)
        self._export_button.setEnabled(self._results is not None and not self._busy)
        if self._busy:
            return
        if not state.connected:
            self._status_label.setText("Disconnected")
        elif state.acquisition_running:
            self._status_label.setText("Acquisition active — tools locked")
        elif self._status_override:
            self._status_label.setText(self._status_override)
        elif not state.setup_applied:
            self._status_label.setText("Idle — apply scope setup before recording test")
        else:
            self._status_label.setText("Connected / Idle")

    def _enqueue_event(self, kind: str, payload: object) -> None:
        self._events.put((kind, payload))

    def _operation_available(self, require_setup: bool = False) -> bool:
        state = self._state()
        available = (
            state.connected
            and not state.acquisition_running
            and not self._busy
            and (state.setup_applied or not require_setup)
        )
        if not available:
            self._refresh_availability()
        return available

    def _start_worker(
        self,
        worker: ScopeCommandWorker | BurstDiagnosticWorker,
        operation: str,
        invalidates_setup: bool,
    ) -> None:
        if self._busy:
            return
        self._worker = worker
        self._operation = operation
        self._operation_invalidates_setup = invalidates_setup
        self._status_override = ""
        self._busy = True
        self._status_label.setText(operation)
        self._on_busy_changed()
        self._refresh_availability()
        worker.start()

    def _finish_operation(self) -> None:
        invalidates = self._operation_invalidates_setup
        operation = self._operation
        self._worker = None
        self._busy = False
        self._operation = ""
        self._operation_invalidates_setup = False
        if invalidates:
            self._on_setup_invalidated(f"{operation} changed oscilloscope state")
        self._on_busy_changed()
        self._refresh_availability()

    def _process_events(self) -> None:
        finished = False
        while True:
            try:
                kind, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._command_log.appendPlainText(str(payload))
            elif kind == "status":
                self._status_label.setText(str(payload))
            elif kind == "maximum_frames":
                self._maximum_frames_label.setText(f"{int(payload):,}")
            elif kind == "frame" and isinstance(payload, BurstFrameResult):
                self._append_frame_result(payload)
            elif kind == "summary" and isinstance(payload, BurstDiagnosticSummary):
                self._results = payload
            elif kind == "command_result":
                response = str(payload)
                if self._operation == "Querying maximum frame count":
                    try:
                        self._maximum_frames_label.setText(f"{int(float(response)):,}")
                    except ValueError:
                        self._maximum_frames_label.setText(response)
                    self._status_override = "Maximum frame count received"
                else:
                    self._console_response.appendPlainText(response)
                    self._status_override = "SCPI operation completed"
                finished = True
            elif kind == "done":
                self._status_override = str(payload)
                finished = True
            elif kind == "error":
                message = str(payload)
                self._status_override = f"Error: {message}"
                self._command_log.appendPlainText(f"ERROR: {message}")
                QMessageBox.critical(self, "DHO804 tools", message)
                finished = True
        if finished and self._busy:
            self._finish_operation()
        self._refresh_availability()

    def _append_frame_result(self, result: BurstFrameResult) -> None:
        row = self._results_table.rowCount()
        self._results_table.insertRow(row)
        values = (
            str(result.frame),
            result.hardware_timestamp,
            f"{result.points:,}",
            f"{result.payload_bytes:,}",
            result.sha256[:12],
            result.result,
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setToolTip(result.waveform_path)
            self._results_table.setItem(row, column, item)

    def _query_maximum_frames(self) -> None:
        if not self._operation_available():
            return
        scpi = self._scpi_provider()
        if scpi is None:
            return
        self._start_worker(
            ScopeCommandWorker(
                scpi,
                ":RECord:WRECord:FMAX?",
                query=True,
                on_event=self._enqueue_event,
            ),
            "Querying maximum frame count",
            invalidates_setup=False,
        )

    def _choose_output_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "Choose burst diagnostic output directory",
            self._output_directory_input.text().strip(),
        )
        if directory:
            self._output_directory_input.setText(directory)

    def _run_diagnostic(self) -> None:
        if not self._operation_available(require_setup=True):
            return
        scpi = self._scpi_provider()
        setup = self._setup_provider()
        if scpi is None or setup is None:
            QMessageBox.warning(
                self,
                "Burst diagnostic",
                "Connect the DHO804, verify safety, and apply the scope setup first.",
            )
            return
        try:
            frames = int(self._frame_count_input.text().strip())
            interval = float(self._frame_interval_input.text().strip())
            timeout = float(self._record_timeout_input.text().strip())
            output_parent = self._output_directory_input.text().strip()
            if frames < 1:
                raise ValueError("Test frame count must be at least 1.")
            if frames > MAX_DIAGNOSTIC_FRAMES:
                raise ValueError(
                    f"The capability test is limited to {MAX_DIAGNOSTIC_FRAMES:,} frames. "
                    "Production high-frame-count recording remains disabled until validation."
                )
            if not 1e-8 <= interval <= 1.0:
                raise ValueError("Frame interval must be from 1e-8 to 1 second.")
            if timeout <= 0:
                raise ValueError("Record timeout must be positive.")
            if not output_parent:
                raise ValueError("Choose an output directory for the diagnostic files.")
            output_path = Path(output_parent)
            if not output_path.is_dir():
                raise ValueError("The selected output directory does not exist.")
            if setup.acquisition_type.upper().startswith("ULTR"):
                raise ValueError(
                    "RIGOL does not support waveform recording in UltraAcquire mode. "
                    "Select Normal or Peak detect and Apply setup again."
                )
            points_per_frame = DHO804_MEMORY_POINTS.get(
                setup.memory_depth,
                setup.waveform_points,
            )
            estimated_payload_bytes = frames * points_per_frame * 2
            if estimated_payload_bytes > MAX_DIAGNOSTIC_PAYLOAD_BYTES:
                raise ValueError(
                    "The requested diagnostic could exceed 2 GiB of RAW payloads. "
                    "Reduce the frame count or memory depth."
                )
        except ValueError as exc:
            QMessageBox.warning(self, "Burst diagnostic", str(exc))
            return

        state = self._state()
        self._results = None
        self._results_table.setRowCount(0)
        self._start_worker(
            BurstDiagnosticWorker(
                scpi=scpi,
                setup=setup,
                connection=state.connection,
                device_idn=state.device_idn,
                requested_frames=frames,
                frame_interval_seconds=interval,
                record_timeout_seconds=timeout,
                output_parent=output_parent,
                on_event=self._enqueue_event,
            ),
            "Running burst capability test",
            invalidates_setup=True,
        )

    def _console_command(self) -> str | None:
        command = self._console_command_input.text().strip()
        if not command:
            return None
        compact = command.upper().replace(" ", "")
        if "WAVEFORM:DATA?" in compact or "WAV:DATA?" in compact:
            QMessageBox.warning(
                self,
                "Binary query blocked",
                "Use the waveform-recording diagnostic for binary waveform transfers.",
            )
            return None
        return command

    def _console_query(self) -> None:
        if not self._operation_available():
            return
        command = self._console_command()
        scpi = self._scpi_provider()
        if command is None or scpi is None:
            return
        if "?" not in command:
            QMessageBox.warning(
                self,
                "Query expected",
                "This command has no query marker (?). Use Write for non-query commands.",
            )
            return
        self._start_worker(
            ScopeCommandWorker(scpi, command, query=True, on_event=self._enqueue_event),
            f"Querying {command}",
            invalidates_setup=False,
        )

    def _console_write(self) -> None:
        if not self._operation_available():
            return
        command = self._console_command()
        scpi = self._scpi_provider()
        if command is None or scpi is None:
            return
        if "?" in command:
            QMessageBox.warning(
                self,
                "Unread query blocked",
                "Use Query for commands containing ?. Sending a query as a write would leave "
                "its response queued and corrupt the next SCPI transaction.",
            )
            return
        upper = command.upper()
        if "*RST" in upper or "SYST" in upper and "PRE" in upper:
            answer = QMessageBox.question(
                self,
                "Confirm scope reset",
                f"Send potentially destructive command?\n\n{command}",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._start_worker(
            ScopeCommandWorker(scpi, command, query=False, on_event=self._enqueue_event),
            f"Writing {command}",
            invalidates_setup=True,
        )

    def _stop_operation(self) -> None:
        if self._worker is not None:
            self._status_label.setText("Stop requested…")
            self._worker.stop()

    def _export_results(self) -> None:
        if self._results is None:
            return
        suggested = str(Path(self._results.output_directory) / "diagnostic_export.json")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export burst diagnostic results",
            suggested,
            "JSON files (*.json);;All files (*)",
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        Path(path).write_text(json.dumps(asdict(self._results), indent=2), encoding="utf-8")

    def shutdown(self) -> None:
        self._timer.stop()
        if self._worker is not None and self._worker.is_alive():
            self._worker.stop()
            self._worker.join(timeout=1.5)

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self.hide()
