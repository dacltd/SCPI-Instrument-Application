from __future__ import annotations

import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from dmm_app.commands import INSTRUMENT_PROFILES
from dmm_app.gsmiv_power import FIELDS, GsmivPowerWorker, parse_sample
from dmm_app.gui import DMMAppWindow
from dmm_app.logging_util import CsvLogger
from dmm_app.models import ConnectionKind, InstrumentType, MeasurementFunction
from dmm_app.poller import PollRequest, PollingWorker, parse_primary_value
from tests.test_acquisition import FakeClock

FIXTURE = Path(__file__).parent / "fixtures/gsmiv_power_schema1.txt"


def telemetry(**changes):
    # Fixture is emitted by the firmware repository's production-linked C++ test.
    line = next(line for line in FIXTURE.read_text().splitlines() if line.startswith("[POWER_BENCH],"))
    row = dict(zip(FIELDS, line.split(",")[1:]))
    row.update({key: str(value) for key, value in changes.items()})
    return "[POWER_BENCH]," + ",".join(row[key] for key in FIELDS)


class PowerCaptureTests(unittest.TestCase):
    def test_firmware_fixture_and_read_failures(self):
        rows = [row for line in FIXTURE.read_text().splitlines() if (row := parse_sample(line)) is not None]
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["ibat_ma"], 500)
        self.assertEqual(rows[0]["vbat_mv"], 3980)
        self.assertEqual(rows[0]["vext_mv"], 3000)  # No sub-4.5V clamp.
        self.assertEqual(rows[1]["ibat_validity"], "read_error")
        self.assertEqual(rows[1]["ibat_ma"], "")
        self.assertEqual(rows[2]["vbat_mv"], "")

    def test_invalid_and_disabled_samples_are_never_numeric_measurements(self):
        for raw, validity in ((32768, "aborted"), (4004, "out_of_range"), (58032, "out_of_range")):
            sample = parse_sample(telemetry(ibat_raw=raw))
            self.assertEqual(sample["ibat_validity"], validity)
            self.assertEqual(sample["ibat_ma"], "")
        self.assertEqual(parse_sample(telemetry(ibat_raw=65036))["ibat_ma"], -500)
        self.assertEqual(parse_sample(telemetry(ibat_raw=0))["ibat_ma"], 0)
        for changes in ({"adc_control": 0}, {"adc_disable": 64}, {"read_mask": 2047 & ~(1 << 4)}):
            self.assertEqual(parse_sample(telemetry(**changes))["ibat_ma"], "")
        self.assertEqual(parse_sample(telemetry(vext_valid=0))["vext_mv"], "")
        self.assertEqual(parse_sample(telemetry(vext_max=4095))["vext_mv"], "")
        for bad in (telemetry(schema=2), telemetry(vext_min=121), telemetry(read_mask=2048),
                    telemetry(ibat_raw=65536), telemetry() + ",noise", "[POWER_BENCH],1,garbled"):
            with self.assertRaises(ValueError):
                parse_sample(bad)

    def test_usb_profile_uses_simulator_queries_and_normalizes_current(self):
        profile = INSTRUMENT_PROFILES[InstrumentType.KEITHLEY_2281S]
        self.assertEqual(profile.connection_kind, ConnectionKind.VISA)
        self.assertTrue(all(not c.prepare_commands for c in profile.commands.values()))
        current = profile.commands[MeasurementFunction.BATTERY_CURRENT]
        self.assertEqual(current.query_command, ":BATTery:SIMulator:CURRent?")
        self.assertEqual(profile.commands[MeasurementFunction.BATTERY_VOLTAGE].query_command,
                         ":BATTery:SIMulator:TVOLtage?")
        class Instrument:
            queries = []
            def query(self, command):
                self.queries.append(command)
                return "-2.5E-02A"
        scpi = Instrument()
        readings = []
        def received(reading):
            readings.append(reading)
            worker.stop()
        worker = PollingWorker(scpi, FakeClock(), 0, "USB::SIM::INSTR", profile.instrument,
                               "KEITHLEY,2281S-20-6", [PollRequest(0, current.function,
                               current.query_command, current.unit, value_scale=current.value_scale)],
                               0.2, received, self.fail)
        worker.run()
        self.assertEqual(readings[0].value, 0.025)
        self.assertEqual(readings[0].raw_response, "-2.5E-02A")
        self.assertEqual(scpi.queries, [current.query_command])
        for value in ("nan", "inf", "9.9E37", "bad", "1.2oops"):
            self.assertIsNone(parse_primary_value(value))

    def test_split_serial_lines_reset_segmentation_and_shared_csv(self):
        first = telemetry()
        second = telemetry(sequence=1, tick_ms=1250, ibat_raw=32768)
        reboot = telemetry(sequence=0, tick_ms=100)
        payload = (first + "\r\n" + second + "\r\n" + reboot + "\r\n").encode()
        chunks = iter((payload[:23], b"", payload[23:70], payload[70:]))
        class Transport:
            def read_until(self, _terminator):
                try:
                    return next(chunks)
                except StopIteration:
                    worker.stop()
                    return b""
        readings = []
        worker = GsmivPowerWorker(transport=Transport(), clock=FakeClock(), instrument_index=1,
            connection="COM7", terminator=b"\n", on_reading=readings.append,
            on_error=self.fail, measurements=[PollRequest(0, MeasurementFunction.BATTERY_CURRENT,
                "ibat_ma", "A")])
        worker.run()
        self.assertEqual(len(readings), 6)  # Original frame + selected numeric channel.
        self.assertEqual(readings[0].elapsed_seconds, readings[1].elapsed_seconds)
        self.assertEqual(readings[1].value, 0.5)
        self.assertIsNone(readings[3].value)
        self.assertEqual(readings[4].source, "segment=1")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shared.csv"
            logger = CsvLogger(str(path))
            for reading in readings:
                logger.write_reading(reading)
            logger.close()
            with path.open() as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(rows[0]["raw_response"], first)
            self.assertEqual(rows[1]["value"], "0.5")
            self.assertEqual(rows[3]["value"], "")

    def test_malformed_row_preserved_and_next_good_row_decoded(self):
        chunks = iter((b"[POWER_BENCH],broken\n", telemetry().encode() + b"\n"))
        class Transport:
            def read_until(self, _terminator):
                try: return next(chunks)
                except StopIteration:
                    worker.stop()
                    return b""
        readings = []
        worker = GsmivPowerWorker(transport=Transport(), clock=FakeClock(), instrument_index=0,
            connection="COM7", terminator=b"\n", on_reading=readings.append, on_error=self.fail,
            measurements=[PollRequest(0, MeasurementFunction.BATTERY_VOLTAGE, "vbat_mv", "V")])
        worker.run()
        self.assertIn("invalid=", readings[0].source)
        self.assertEqual(readings[-1].value, 3.98)


class PowerGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_profiles_default_to_voltage_current_and_round_trip_configuration(self):
        with patch("dmm_app.gui.SerialTransport.list_serial_ports", return_value=[]), \
             patch("dmm_app.gui.VisaTransport.list_resources", return_value=[]):
            window = DMMAppWindow()
        try:
            for index, instrument in enumerate((InstrumentType.KEITHLEY_2281S, InstrumentType.GSMIV_POWER)):
                panel = window._panels[index]
                panel._instrument_combo.setCurrentText(instrument.value)
                self.assertEqual(len(panel._measurement_rows), 2)
                requests, setup = panel._build_poll_requests()
                self.assertEqual(setup, [])
                self.assertEqual(requests[1].function, MeasurementFunction.BATTERY_CURRENT)
                if index == 0:
                    self.assertEqual(requests[1].value_scale, -1)
                else:
                    self.assertTrue(panel._measurement_rows[0].function_combo.isEnabled())
                    panel._measurement_rows[0].function_combo.setCurrentText(MeasurementFunction.EXTERNAL_VOLTAGE.value)
                    self.assertEqual(panel._build_poll_requests()[0][0].query_command, "vext_mv")
            document = window._configuration_document()
            # Exercise the same validation used when loading a saved configuration.
            window._apply_configuration_document(document)
            self.assertEqual(window._panels[0]._selected_instrument(), InstrumentType.KEITHLEY_2281S)
            self.assertEqual(window._panels[1]._selected_instrument(), InstrumentType.GSMIV_POWER)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
