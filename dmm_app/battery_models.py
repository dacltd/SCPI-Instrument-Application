"""Read Keithley 2281S user-model curves and save them on the host.

Reference manual 077114601, sections 7-30 and 7-33: only query model
slots 1..9; never recall a model or use SAVE:USB (the instrument's USB stick).
"""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import tempfile
import threading


class DownloadCancelled(Exception):
    """The user stopped the model download."""


def _numeric_list(response: str) -> list[Decimal]:
    text = response.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    try:
        values = [Decimal(token.strip()) for token in text.split(",")]
    except InvalidOperation as exc:
        raise ValueError("The instrument returned invalid battery-model data.") from exc
    if any(not value.is_finite() or value < 0 or value >= Decimal("1e30") for value in values):
        raise ValueError("The instrument returned invalid or over-range battery-model data.")
    return values


def read_model(scpi, slot: int, stop_event: threading.Event) -> list[tuple[int, Decimal, Decimal]]:
    if type(slot) is not int or not 1 <= slot <= 9:
        raise ValueError("Choose a user-model slot from 1 to 9.")

    def query(suffix):
        if stop_event.is_set():
            raise DownloadCancelled()
        return scpi.query(f":BATTery:MODel{slot}:{suffix}")

    for element in ("VOLTAGE", "RESISTANCE"):
        command = "VOC" if element == "VOLTAGE" else "RESistance"
        count = _numeric_list(query(f"{command}:STEPs?"))
        if count != [Decimal(101)]:
            raise ValueError(
                f"Model {slot} does not contain a complete 101-point {element.lower()} curve. "
                "Choose a populated user-model slot."
            )
    volts = _numeric_list(query("VOC?"))
    ohms = _numeric_list(query("RESistance?"))
    if len(volts) != 101 or len(ohms) != 101:
        raise ValueError("Incomplete battery-model response; expected 101 points in both curves.")
    if stop_event.is_set():
        raise DownloadCancelled()
    return list(zip(range(101), volts, ohms, strict=True))


def save_model(path: Path, points, stop_event: threading.Event) -> None:
    """Replace the destination only after the whole CSV is safely written."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as output:
            temporary = Path(output.name)
            writer = csv.writer(output)
            writer.writerow(("soc_percent", "open_circuit_voltage_V", "resistance_ohm"))
            writer.writerows(points)
            output.flush()
            os.fsync(output.fileno())
        if stop_event.is_set():
            raise DownloadCancelled()
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class BatteryModelDownloadWorker(threading.Thread):
    def __init__(self, *, scpi, slot: int, path: str, on_result):
        super().__init__(daemon=True)
        self._scpi = scpi
        self._slot = slot
        self._path = Path(path)
        self._on_result = on_result
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        try:
            points = read_model(self._scpi, self._slot, self._stop_event)
            save_model(self._path, points, self._stop_event)
        except DownloadCancelled:
            self._on_result(("cancelled", "Battery-model download cancelled."))
        except Exception as exc:
            self._on_result(("error", f"Battery-model download failed: {exc}"))
        else:
            self._on_result(("saved", f"Saved model {self._slot} (101 points) to {self._path}"))
