from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from dmm_app.clock import AcquisitionClock
from dmm_app.models import InstrumentType, MeasurementFunction, Reading
from dmm_app.scpi import SCPIClient
from dmm_app.transport import Transport


def parse_primary_value(raw_response: str) -> float | None:
    token = raw_response.replace(",", " ").split()[0].strip() if raw_response.strip() else ""
    if not token:
        return None
    try:
        return float(token)
    except ValueError:
        return None


@dataclass(frozen=True)
class PollRequest:
    slot_index: int
    function: MeasurementFunction
    query_command: str
    unit: str
    source: str = ""


class PollingWorker(threading.Thread):
    def __init__(
        self,
        scpi: SCPIClient,
        clock: AcquisitionClock,
        instrument_index: int,
        connection: str,
        instrument: InstrumentType,
        device_idn: str,
        measurements: list[PollRequest],
        interval_seconds: float,
        on_reading: Callable[[Reading], None],
        on_error: Callable[[str], None],
        start_gate: threading.Event | None = None,
    ):
        super().__init__(daemon=True)
        self._scpi = scpi
        self._clock = clock
        self._instrument_index = instrument_index
        self._connection = connection
        self._instrument = instrument
        self._device_idn = device_idn
        self._measurements = measurements
        self._interval_seconds = interval_seconds
        self._on_reading = on_reading
        self._on_error = on_error
        self._start_gate = start_gate
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if self._start_gate is not None:
            self._start_gate.wait()
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                for measurement in self._measurements:
                    if self._stop_event.is_set():
                        break
                    raw = self._scpi.query(measurement.query_command)
                    timestamp, elapsed_seconds, acquisition_run = self._clock.capture()
                    reading = Reading(
                        timestamp=timestamp,
                        elapsed_seconds=elapsed_seconds,
                        acquisition_run=acquisition_run,
                        instrument_index=self._instrument_index,
                        slot_index=measurement.slot_index,
                        instrument=self._instrument,
                        connection=self._connection,
                        device_idn=self._device_idn,
                        function=measurement.function,
                        source=measurement.source,
                        raw_response=raw,
                        value=parse_primary_value(raw),
                        unit=measurement.unit,
                    )
                    self._on_reading(reading)
            except Exception as exc:  # pragma: no cover - hardware error path
                self._on_error(str(exc))
                return

            elapsed = time.monotonic() - started
            remaining = self._interval_seconds - elapsed
            if remaining > 0:
                self._stop_event.wait(remaining)


class RawSerialWorker(threading.Thread):
    def __init__(
        self,
        transport: Transport,
        clock: AcquisitionClock,
        instrument_index: int,
        connection: str,
        terminator: bytes,
        on_reading: Callable[[Reading], None],
        on_error: Callable[[str], None],
        start_gate: threading.Event | None = None,
        encoding: str = "utf-8",
    ):
        super().__init__(daemon=True)
        self._transport = transport
        self._clock = clock
        self._instrument_index = instrument_index
        self._connection = connection
        self._terminator = terminator
        self._on_reading = on_reading
        self._on_error = on_error
        self._start_gate = start_gate
        self._encoding = encoding
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if self._start_gate is not None:
            self._start_gate.wait()
        while not self._stop_event.is_set():
            try:
                payload = self._transport.read_until(self._terminator)
                if not payload:
                    continue
                timestamp, elapsed_seconds, acquisition_run = self._clock.capture()
                raw = payload.decode(self._encoding, errors="replace").rstrip("\r\n")
                self._on_reading(
                    Reading(
                        timestamp=timestamp,
                        elapsed_seconds=elapsed_seconds,
                        acquisition_run=acquisition_run,
                        instrument_index=self._instrument_index,
                        slot_index=0,
                        instrument=InstrumentType.RAW_SERIAL,
                        connection=self._connection,
                        device_idn=self._connection,
                        function=MeasurementFunction.RAW_DATA,
                        source="",
                        raw_response=raw,
                        value=parse_primary_value(raw),
                        unit="",
                    )
                )
            except Exception as exc:  # pragma: no cover - hardware error path
                if not self._stop_event.is_set():
                    self._on_error(str(exc))
                return
