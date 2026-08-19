from __future__ import annotations

import csv
from pathlib import Path

from dmm_app.models import Reading


class CsvLogger:
    def __init__(self, path: str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = self._path.exists() and self._path.stat().st_size > 0
        self._file = self._path.open("a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        if not file_exists:
            self._writer.writerow(
                [
                    "timestamp",
                    "elapsed_seconds",
                    "acquisition_run",
                    "instrument_window",
                    "measurement_slot",
                    "device_name",
                    "connection",
                    "device_idn",
                    "function",
                    "source",
                    "value",
                    "unit",
                    "raw_response",
                ]
            )
            self._file.flush()

    @property
    def path(self) -> str:
        return str(self._path)

    def write_reading(self, reading: Reading) -> None:
        self._writer.writerow(
            [
                reading.timestamp.isoformat(timespec="microseconds"),
                f"{reading.elapsed_seconds:.9f}",
                reading.acquisition_run,
                reading.instrument_index + 1,
                "" if reading.slot_index < 0 else reading.slot_index + 1,
                reading.instrument.value,
                reading.connection,
                reading.device_idn,
                reading.function.value,
                reading.source,
                "" if reading.value is None else f"{reading.value:.12g}",
                reading.unit,
                reading.raw_response,
            ]
        )
        self._file.flush()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()
