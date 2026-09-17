"""GSMIV schema-1 live power telemetry; host receive timestamps, not ADC timestamps."""
from __future__ import annotations

from dataclasses import replace
from dmm_app.models import InstrumentType, MeasurementFunction, Reading
from dmm_app.poller import RawSerialWorker

PREFIX = "[POWER_BENCH],"
FIELDS = "schema sequence tick_ms span_ms read_mask status0 status1 fault0 control1 adc_control adc_disable ibat_raw vbat_raw vbus_raw vsys_raw ts_raw ce_requested ce_matches external_requested call_active wifi_active vext_valid vext_min vext_median vext_max vext_mv calibration_source keep_awake".split()
DERIVED = "ibat_validity ibat_ma vbat_mv vbus_mv vsys_mv en_chg vsys_reg iindpm vindpm chg_stat vbus_stat vext_saturated adc_enabled ibat_channel_enabled".split()


def parse_sample(line: str) -> dict | None:
    if not line.startswith(PREFIX):
        return None
    values = line.strip().split(",")[1:]
    if len(values) != len(FIELDS) or any(not v.isascii() or not v.isdecimal() for v in values):
        raise ValueError("malformed power row")
    row = dict(zip(FIELDS, map(int, values)))
    if row["schema"] != 1:
        raise ValueError("unsupported power schema")
    limits = {key: 0xFFFFFFFF for key in FIELDS}
    for key in "status0 status1 fault0 control1 adc_control adc_disable calibration_source".split():
        limits[key] = 255
    for key in "ibat_raw vbat_raw vbus_raw vsys_raw ts_raw".split():
        limits[key] = 65535
    for key in "ce_requested ce_matches external_requested call_active wifi_active vext_valid keep_awake".split():
        limits[key] = 1
    for key in "vext_min vext_median vext_max".split():
        limits[key] = 4095
    limits["read_mask"] = 0x7FF
    if any(row[key] > limit for key, limit in limits.items()):
        raise ValueError("power field out of range")
    if row["vext_valid"] and not row["vext_min"] <= row["vext_median"] <= row["vext_max"]:
        raise ValueError("invalid VEXT burst ordering")
    row.update({key: "" for key in DERIVED})
    read_ok = lambda bit: bool(row["read_mask"] & (1 << bit))
    row["ibat_validity"] = "read_error"
    if read_ok(6):
        field = row["ibat_raw"] >> 2
        ma = (field - 16384 if field & 8192 else field) * 4
        row["ibat_validity"] = "aborted" if field == 8192 else "out_of_range" if not -7500 <= ma <= 4000 else "valid"
        if row["ibat_validity"] == "valid":
            row["ibat_ma"] = ma
    for name, bit, mask, shift, scale, maximum in (
        ("vbat", 7, 0x1FFE, 1, 1.99, 0xAF0),
        ("vbus", 8, 0x7FFC, 2, 3.97, 0x11B6),
        ("vsys", 9, 0x1FFE, 1, 1.99, 0xAF0),
    ):
        code = (row[name + "_raw"] & mask) >> shift
        if read_ok(bit) and code <= maximum:
            row[name + "_mv"] = round(code * scale, 2)
    if read_ok(0):
        for key, mask in (("vsys_reg", 16), ("iindpm", 8), ("vindpm", 4)):
            row[key] = int(bool(row["status0"] & mask))
    if read_ok(1):
        row["chg_stat"] = (row["status1"] >> 3) & 3
        row["vbus_stat"] = row["status1"] & 7
    if read_ok(3):
        row["en_chg"] = int(bool(row["control1"] & 32))
    if read_ok(4):
        row["adc_enabled"] = int(bool(row["adc_control"] & 128))
    if read_ok(5):
        row["ibat_channel_enabled"] = int(not row["adc_disable"] & 64)
    if row["vext_valid"]:
        row["vext_saturated"] = int(row["vext_max"] == 4095)
    else:
        row["vext_mv"] = ""
    # A successful bus read may return a retained ADC register. Known disabled
    # or unreadable ADC configuration must not become a live plotted value.
    for name, disable_mask in (("ibat_ma", 64), ("vbat_mv", 16), ("vbus_mv", 32), ("vsys_mv", 8)):
        unavailable = not read_ok(4) or not read_ok(5)
        disabled = not row["adc_control"] & 128 or row["adc_disable"] & disable_mask
        if unavailable or disabled:
            row[name] = ""
            if name == "ibat_ma" and row["ibat_validity"] == "valid":
                row["ibat_validity"] = "adc_unverified" if unavailable else "adc_disabled"
    if row["vext_saturated"] == 1:
        row["vext_mv"] = ""
    return row


class GsmivPowerWorker(RawSerialWorker):
    def __init__(self, *, measurements, **kwargs):
        super().__init__(**kwargs)
        self._measurements = measurements

    def run(self):
        if self._start_gate is not None:
            self._start_gate.wait()
        buffer = b""
        discarding = False
        segment = 0
        previous = None
        identity = "GSMIV power telemetry schema 1"
        while not self._stop_event.is_set():
            try:
                buffer += self._transport.read_until(self._terminator)
                while self._terminator in buffer:
                    payload, buffer = buffer.split(self._terminator, 1)
                    if discarding:
                        discarding = False
                        continue
                    timestamp, elapsed, run = self._clock.capture()
                    raw = payload.decode(self._encoding, errors="replace").rstrip("\r\n")
                    if raw.startswith("[POWER_BENCH_META]"):
                        identity = raw
                    try:
                        sample = parse_sample(raw)
                        error = ""
                    except ValueError as exc:
                        sample, error = None, str(exc)
                    if sample is not None:
                        if previous and (sample["sequence"] <= previous[0] or sample["tick_ms"] < previous[1]):
                            segment += 1
                        previous = sample["sequence"], sample["tick_ms"]
                    base = Reading(timestamp, elapsed, run, self._instrument_index, -1,
                                   InstrumentType.GSMIV_POWER, self._connection, identity,
                                   MeasurementFunction.RAW_DATA, f"segment={segment}" + (f" invalid={error}" if error else ""),
                                   raw, None, "")
                    self._on_reading(base)  # Retain every line, including invalid data and metadata.
                    if sample is None:
                        continue
                    for measurement in self._measurements:
                        value = sample[measurement.query_command]
                        self._on_reading(replace(base, slot_index=measurement.slot_index,
                            function=measurement.function, source=measurement.query_command,
                            value=None if value == "" else value / 1000.0, unit=measurement.unit))
                if len(buffer) > 16384:
                    buffer = b""
                    discarding = True
            except Exception as exc:
                if not self._stop_event.is_set():
                    self._on_error(str(exc))
                return
