from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from dmm_app.clock import AcquisitionClock
from dmm_app.models import InstrumentType, MeasurementFunction, Reading
from dmm_app.scpi import SCPIClient


DHO804_MEMORY_POINTS = {
    "100k": 100_000,
    "1M": 1_000_000,
    "5M": 5_000_000,
    "10M": 10_000_000,
    "25M": 25_000_000,
}

LOGGING_FIDELITY_SAMPLES_PER_CYCLE = {
    "coverage": 100.0,
    "balanced": 250.0,
}


@dataclass(frozen=True)
class OscilloscopeSetup:
    source: str = "CHANnel1"
    only_selected_channel: bool = True
    probe_ratio: float = 10.0
    coupling: str = "DC"
    bandwidth_limit: str = "OFF"
    vertical_scale_volts: float = 10.0
    time_scale_seconds: float = 1e-6
    memory_depth: str = "1M"
    acquisition_type: str = "NORMal"
    trigger_slope: str = "POSitive"
    trigger_level_volts: float = -24.0
    waveform_points: int = 1_000_000
    trigger_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class WaveformPreamble:
    format_code: int
    mode_code: int
    points: int
    count: int
    x_increment: float
    x_origin: float
    x_reference: float
    y_increment: float
    y_origin: float
    y_reference: float

    @classmethod
    def parse(cls, response: str) -> "WaveformPreamble":
        fields = [field.strip() for field in response.split(",")]
        if len(fields) != 10:
            raise ValueError(f"Expected 10 waveform preamble fields, received {len(fields)}.")
        return cls(
            format_code=int(fields[0]),
            mode_code=int(fields[1]),
            points=int(float(fields[2])),
            count=int(float(fields[3])),
            x_increment=float(fields[4]),
            x_origin=float(fields[5]),
            x_reference=float(fields[6]),
            y_increment=float(fields[7]),
            y_origin=float(fields[8]),
            y_reference=float(fields[9]),
        )


@dataclass(frozen=True)
class WaveformLoggingPlan:
    fidelity: str
    sample_rate_hz: float
    samples_per_cycle: float
    time_scale_seconds: float
    record_span_seconds: float
    cycles_per_capture: float
    waveform_points: int
    payload_bytes: int
    sample_rate_limited: bool


@dataclass(frozen=True)
class WaveformCaptureProgress:
    capture_sequence: int
    record_span_seconds: float
    start_to_start_seconds: float | None
    transfer_seconds: float
    file_write_seconds: float
    payload_bytes: int

    @property
    def coverage_percent(self) -> float | None:
        if not self.start_to_start_seconds or self.start_to_start_seconds <= 0:
            return None
        return min(100.0, 100.0 * self.record_span_seconds / self.start_to_start_seconds)

    @property
    def blind_gap_seconds(self) -> float | None:
        if self.start_to_start_seconds is None:
            return None
        return max(0.0, self.start_to_start_seconds - self.record_span_seconds)

    @property
    def captures_per_minute(self) -> float | None:
        if not self.start_to_start_seconds or self.start_to_start_seconds <= 0:
            return None
        return 60.0 / self.start_to_start_seconds

    @property
    def gibibytes_per_hour(self) -> float | None:
        if self.captures_per_minute is None:
            return None
        return self.payload_bytes * self.captures_per_minute * 60.0 / (1024**3)

    @property
    def transfer_mibibytes_per_second(self) -> float | None:
        if self.transfer_seconds <= 0:
            return None
        return self.payload_bytes / self.transfer_seconds / (1024**2)


def optimise_waveform_logging(
    switching_frequency_hz: float,
    waveform_points: int,
    fidelity: str,
    only_selected_channel: bool,
) -> WaveformLoggingPlan:
    """Choose a timebase that maximises span while preserving requested fidelity."""
    if switching_frequency_hz <= 0:
        raise ValueError("Switching frequency must be positive.")
    if waveform_points <= 0:
        raise ValueError("Waveform points must be positive.")
    if fidelity not in (*LOGGING_FIDELITY_SAMPLES_PER_CYCLE, "edge"):
        raise ValueError(f"Unsupported logging fidelity: {fidelity}.")

    # DHO804 maximum sample rates from the DHO800 data sheet. When other channels
    # are not explicitly disabled, use the conservative three/four-channel limit.
    maximum_sample_rate_hz = 1.25e9 if only_selected_channel else 312.5e6
    required_sample_rate_hz = (
        maximum_sample_rate_hz
        if fidelity == "edge"
        else switching_frequency_hz * LOGGING_FIDELITY_SAMPLES_PER_CYCLE[fidelity]
    )
    sample_rate_hz = min(required_sample_rate_hz, maximum_sample_rate_hz)
    sample_rate_limited = required_sample_rate_hz > maximum_sample_rate_hz
    samples_per_cycle = sample_rate_hz / switching_frequency_hz
    record_span_seconds = waveform_points / sample_rate_hz
    return WaveformLoggingPlan(
        fidelity=fidelity,
        sample_rate_hz=sample_rate_hz,
        samples_per_cycle=samples_per_cycle,
        time_scale_seconds=record_span_seconds / 10.0,
        record_span_seconds=record_span_seconds,
        cycles_per_capture=record_span_seconds * switching_frequency_hz,
        waveform_points=waveform_points,
        payload_bytes=waveform_points * 2,
        sample_rate_limited=sample_rate_limited,
    )


@dataclass(frozen=True)
class _CapturedWaveform:
    timestamp: datetime
    elapsed_seconds: float
    acquisition_run: int
    trigger_status: str
    preamble: WaveformPreamble
    payload: bytes
    arm_monotonic: float
    stop_observed_monotonic: float
    preamble_query_seconds: float
    transfer_seconds: float


@dataclass(frozen=True)
class _QueuedWaveformSave:
    capture: _CapturedWaveform
    output_path: Path
    capture_sequence: int
    start_to_start_seconds: float | None


def build_dho804_setup_commands(setup: OscilloscopeSetup) -> list[str]:
    channel_number = int(setup.source.removeprefix("CHANnel"))
    if channel_number not in (1, 2, 3, 4):
        raise ValueError("DHO804 source must be CHANnel1 through CHANnel4.")
    if setup.probe_ratio <= 0:
        raise ValueError("Probe ratio must be positive.")
    if setup.vertical_scale_volts <= 0:
        raise ValueError("Vertical scale must be positive.")
    if setup.time_scale_seconds <= 0:
        raise ValueError("Time scale must be positive.")
    if setup.waveform_points < 1:
        raise ValueError("Waveform points must be positive.")
    maximum_points = DHO804_MEMORY_POINTS.get(setup.memory_depth)
    if maximum_points is None:
        raise ValueError(f"Unsupported DHO804 memory depth: {setup.memory_depth}.")
    if setup.waveform_points > maximum_points:
        raise ValueError(
            f"Waveform points ({setup.waveform_points:,}) exceed selected memory depth "
            f"({maximum_points:,})."
        )

    commands: list[str] = []
    if setup.only_selected_channel:
        commands.extend(
            f":CHANnel{number}:DISPlay {'ON' if number == channel_number else 'OFF'}"
            for number in range(1, 5)
        )
    else:
        commands.append(f":CHANnel{channel_number}:DISPlay ON")
    commands.extend(
        [
            f":CHANnel{channel_number}:PROBe {setup.probe_ratio:g}",
            f":CHANnel{channel_number}:COUPling {setup.coupling}",
            f":CHANnel{channel_number}:BWLimit {setup.bandwidth_limit}",
            f":CHANnel{channel_number}:SCALe {setup.vertical_scale_volts:.12g}",
            ":TIMebase:MODE MAIN",
            f":TIMebase:MAIN:SCALe {setup.time_scale_seconds:.12g}",
            f":ACQuire:TYPE {setup.acquisition_type}",
            f":ACQuire:MDEPth {setup.memory_depth}",
            ":TRIGger:MODE EDGE",
            f":TRIGger:EDGE:SOURce {setup.source}",
            f":TRIGger:EDGE:SLOPe {setup.trigger_slope}",
            f":TRIGger:EDGE:LEVel {setup.trigger_level_volts:.12g}",
            ":TRIGger:COUPling DC",
            ":TRIGger:SWEep SINGle",
            f":WAVeform:SOURce {setup.source}",
            ":WAVeform:MODE RAW",
            ":WAVeform:FORMat WORD",
            ":WAVeform:STARt 1",
            f":WAVeform:STOP {setup.waveform_points}",
        ]
    )
    return commands


def _capture_raw_waveform(
    scpi: SCPIClient,
    clock: AcquisitionClock,
    setup: OscilloscopeSetup,
    stop_event: threading.Event,
    cached_preamble: WaveformPreamble | None = None,
) -> _CapturedWaveform | None:
    arm_monotonic = time.monotonic()
    scpi.write(":SINGle")
    deadline = time.monotonic() + setup.trigger_timeout_seconds
    trigger_status = ""
    while not stop_event.is_set():
        trigger_status = scpi.query(":TRIGger:STATus?").strip().upper()
        if trigger_status == "STOP":
            break
        if time.monotonic() >= deadline:
            scpi.write(":STOP")
            raise TimeoutError(
                f"Single trigger did not reach STOP within "
                f"{setup.trigger_timeout_seconds:g} seconds "
                f"(last status: {trigger_status or 'unknown'})."
            )
        stop_event.wait(0.05)
    if stop_event.is_set():
        scpi.write(":STOP")
        return None

    stop_observed_monotonic = time.monotonic()
    timestamp, elapsed_seconds, acquisition_run = clock.capture()
    if cached_preamble is None:
        preamble_started = time.monotonic()
        preamble = WaveformPreamble.parse(scpi.query(":WAVeform:PREamble?"))
        preamble_query_seconds = time.monotonic() - preamble_started
    else:
        preamble = cached_preamble
        preamble_query_seconds = 0.0
    if preamble.format_code != 1 or preamble.mode_code != 2:
        raise ValueError(
            "Scope did not report the requested WORD/RAW waveform format "
            f"(format={preamble.format_code}, mode={preamble.mode_code})."
        )
    transfer_started = time.monotonic()
    payload = scpi.query_binary_block(":WAVeform:DATA?")
    transfer_seconds = time.monotonic() - transfer_started
    if len(payload) % 2:
        raise ValueError(f"WORD waveform payload has an odd byte count ({len(payload):,}).")
    return _CapturedWaveform(
        timestamp=timestamp,
        elapsed_seconds=elapsed_seconds,
        acquisition_run=acquisition_run,
        trigger_status=trigger_status,
        preamble=preamble,
        payload=payload,
        arm_monotonic=arm_monotonic,
        stop_observed_monotonic=stop_observed_monotonic,
        preamble_query_seconds=preamble_query_seconds,
        transfer_seconds=transfer_seconds,
    )


def _save_raw_waveform(
    capture: _CapturedWaveform,
    output_path: Path,
    setup: OscilloscopeSetup,
    instrument_index: int,
    connection: str,
    device_idn: str,
    capture_sequence: int | None = None,
    start_to_start_seconds: float | None = None,
) -> tuple[Reading, WaveformCaptureProgress]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_started = time.monotonic()
    output_path.write_bytes(capture.payload)
    file_write_seconds = time.monotonic() - write_started

    received_points = len(capture.payload) // 2
    record_span_seconds = received_points * capture.preamble.x_increment

    metadata_path = output_path.with_suffix(output_path.suffix + ".json")
    metadata = {
        "captured_at": capture.timestamp.isoformat(timespec="microseconds"),
        "elapsed_seconds": capture.elapsed_seconds,
        "acquisition_run": capture.acquisition_run,
        "instrument_window": instrument_index + 1,
        "instrument": InstrumentType.RIGOL_DHO804.value,
        "connection": connection,
        "device_idn": device_idn,
        "source": setup.source,
        "trigger_status": capture.trigger_status,
        "setup": asdict(setup),
        "preamble": asdict(capture.preamble),
        "raw_word_file": str(output_path),
        "raw_bytes": len(capture.payload),
        "word_points_received": received_points,
        "word_byte_order": "not specified by DHO800/DHO900 programming guide",
        "timing": {
            "trigger_wait_seconds": (
                capture.stop_observed_monotonic - capture.arm_monotonic
            ),
            "preamble_query_seconds": capture.preamble_query_seconds,
            "raw_transfer_seconds": capture.transfer_seconds,
            "binary_file_write_seconds": file_write_seconds,
            "record_span_seconds": record_span_seconds,
            "start_to_start_seconds": start_to_start_seconds,
            "estimated_blind_gap_seconds": (
                None
                if start_to_start_seconds is None
                else max(0.0, start_to_start_seconds - record_span_seconds)
            ),
            "estimated_coverage_percent": (
                None
                if not start_to_start_seconds
                else min(100.0, 100.0 * record_span_seconds / start_to_start_seconds)
            ),
            "captures_per_minute": (
                None if not start_to_start_seconds else 60.0 / start_to_start_seconds
            ),
            "estimated_data_gibibytes_per_hour": (
                None
                if not start_to_start_seconds
                else (
                    len(capture.payload)
                    * (60.0 / start_to_start_seconds)
                    * 60.0
                    / (1024**3)
                )
            ),
        },
    }
    if capture_sequence is not None:
        metadata["capture_sequence"] = capture_sequence
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    reading = Reading(
        timestamp=capture.timestamp,
        elapsed_seconds=capture.elapsed_seconds,
        acquisition_run=capture.acquisition_run,
        instrument_index=instrument_index,
        slot_index=-1,
        instrument=InstrumentType.RIGOL_DHO804,
        connection=connection,
        device_idn=device_idn,
        function=MeasurementFunction.WAVEFORM_CAPTURE,
        source=setup.source,
        raw_response=str(output_path),
        value=float(received_points),
        unit="points",
    )
    progress = WaveformCaptureProgress(
        capture_sequence=capture_sequence or 1,
        record_span_seconds=record_span_seconds,
        start_to_start_seconds=start_to_start_seconds,
        transfer_seconds=capture.transfer_seconds,
        file_write_seconds=file_write_seconds,
        payload_bytes=len(capture.payload),
    )
    return reading, progress


class WaveformCaptureWorker(threading.Thread):
    def __init__(
        self,
        scpi: SCPIClient,
        clock: AcquisitionClock,
        instrument_index: int,
        connection: str,
        device_idn: str,
        setup: OscilloscopeSetup,
        output_path: str,
        on_reading: Callable[[Reading], None],
        on_complete: Callable[[str], None],
        on_error: Callable[[str], None],
    ):
        super().__init__(daemon=True)
        self._scpi = scpi
        self._clock = clock
        self._instrument_index = instrument_index
        self._connection = connection
        self._device_idn = device_idn
        self._setup = setup
        self._output_path = Path(output_path)
        self._on_reading = on_reading
        self._on_complete = on_complete
        self._on_error = on_error
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            capture = _capture_raw_waveform(
                self._scpi,
                self._clock,
                self._setup,
                self._stop_event,
            )
            if capture is None:
                return
            reading, _progress = _save_raw_waveform(
                capture=capture,
                output_path=self._output_path,
                setup=self._setup,
                instrument_index=self._instrument_index,
                connection=self._connection,
                device_idn=self._device_idn,
            )
            self._on_reading(reading)
            metadata_path = self._output_path.with_suffix(self._output_path.suffix + ".json")
            self._on_complete(
                f"Saved {len(capture.payload):,} waveform bytes and metadata to "
                f"{self._output_path} and {metadata_path}."
            )
        except Exception as exc:  # pragma: no cover - hardware and filesystem error path
            if not self._stop_event.is_set():
                self._on_error(str(exc))


class RepeatedWaveformCaptureWorker(threading.Thread):
    def __init__(
        self,
        scpi: SCPIClient,
        clock: AcquisitionClock,
        instrument_index: int,
        connection: str,
        device_idn: str,
        setup: OscilloscopeSetup,
        output_directory: str,
        interval_seconds: float,
        on_reading: Callable[[Reading], None],
        on_complete: Callable[[str], None],
        on_error: Callable[[str], None],
        on_progress: Callable[[WaveformCaptureProgress], None] | None = None,
        start_gate: threading.Event | None = None,
    ):
        super().__init__(daemon=True)
        self._scpi = scpi
        self._clock = clock
        self._instrument_index = instrument_index
        self._connection = connection
        self._device_idn = device_idn
        self._setup = setup
        self._output_directory = Path(output_directory)
        self._interval_seconds = interval_seconds
        self._on_reading = on_reading
        self._on_complete = on_complete
        self._on_error = on_error
        self._on_progress = on_progress
        self._start_gate = start_gate
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        capture_count = 0
        saved_count = 0
        save_queue: queue.Queue[_QueuedWaveformSave | None] = queue.Queue(maxsize=2)
        writer_errors: list[Exception] = []

        def save_waveforms() -> None:
            nonlocal saved_count
            while True:
                item = save_queue.get()
                if item is None:
                    return
                if writer_errors:
                    continue
                try:
                    reading, progress = _save_raw_waveform(
                        capture=item.capture,
                        output_path=item.output_path,
                        setup=self._setup,
                        instrument_index=self._instrument_index,
                        connection=self._connection,
                        device_idn=self._device_idn,
                        capture_sequence=item.capture_sequence,
                        start_to_start_seconds=item.start_to_start_seconds,
                    )
                    saved_count += 1
                    self._on_reading(reading)
                    if self._on_progress is not None:
                        self._on_progress(progress)
                except Exception as exc:  # pragma: no cover - filesystem error path
                    writer_errors.append(exc)
                    self._stop_event.set()

        writer = threading.Thread(target=save_waveforms, daemon=True)
        writer.start()
        try:
            if self._start_gate is not None:
                self._start_gate.wait()
            cached_preamble: WaveformPreamble | None = None
            previous_arm_monotonic: float | None = None
            while not self._stop_event.is_set():
                started = time.monotonic()
                capture = _capture_raw_waveform(
                    self._scpi,
                    self._clock,
                    self._setup,
                    self._stop_event,
                    cached_preamble=cached_preamble,
                )
                if capture is None:
                    break
                cached_preamble = capture.preamble
                capture_count += 1
                source = self._setup.source.lower().replace("channel", "ch")
                filename = (
                    f"dho804_{source}_run{capture.acquisition_run:04d}_"
                    f"{capture.timestamp:%Y%m%dT%H%M%S_%f}_{capture_count:06d}.bin"
                )
                start_to_start_seconds = (
                    None
                    if previous_arm_monotonic is None
                    else capture.arm_monotonic - previous_arm_monotonic
                )
                previous_arm_monotonic = capture.arm_monotonic
                save_queue.put(
                    _QueuedWaveformSave(
                        capture=capture,
                        output_path=self._output_directory / filename,
                        capture_sequence=capture_count,
                        start_to_start_seconds=start_to_start_seconds,
                    )
                )
                remaining = self._interval_seconds - (time.monotonic() - started)
                if remaining > 0:
                    self._stop_event.wait(remaining)
                if writer_errors:
                    raise writer_errors[0]
            save_queue.put(None)
            writer.join()
            if writer_errors:
                raise writer_errors[0]
            self._on_complete(
                f"Waveform logging stopped after {saved_count:,} capture(s). "
                f"Files: {self._output_directory}"
            )
        except Exception as exc:  # pragma: no cover - hardware and filesystem error path
            if writer.is_alive():
                save_queue.put(None)
                writer.join()
            if not self._stop_event.is_set():
                self._on_error(str(exc))
            elif writer_errors:
                self._on_error(str(writer_errors[0]))
