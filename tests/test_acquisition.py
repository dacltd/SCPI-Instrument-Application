from __future__ import annotations

import csv
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from dmm_app.clock import AcquisitionClock
from dmm_app.commands import INSTRUMENT_PROFILES, idn_matches_profile
from dmm_app.gui import DMMAppWindow
from dmm_app.logging_util import CsvLogger
from dmm_app.models import InstrumentType, MeasurementFunction, Reading, VisaSettings
from dmm_app.poller import PollRequest, PollingWorker, RawSerialWorker
from dmm_app.oscilloscope import (
    OscilloscopeSetup,
    RepeatedWaveformCaptureWorker,
    WaveformCaptureWorker,
    WaveformPreamble,
    build_dho804_setup_commands,
    optimise_waveform_logging,
)
from dmm_app.scpi import decode_ieee_binary_blocks
from dmm_app.transport import VisaTransport


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0

    def capture(self):
        self.elapsed += 0.001
        return datetime(2026, 8, 19, tzinfo=timezone.utc), self.elapsed, 1


class FakeScpi:
    def __init__(self):
        self.queries: list[str] = []

    def query(self, command: str) -> str:
        self.queries.append(command)
        return "3.300E+00"


class FakeRawTransport:
    def __init__(self):
        self.calls: list[bytes] = []

    def read_until(self, terminator: bytes) -> bytes:
        self.calls.append(terminator)
        return b"42.5 device-ready\r\n"


class FakeVisaResource:
    def __init__(self):
        self.timeout = None
        self.is_closed = False
        self.writes = []

    def write_raw(self, payload):
        self.writes.append(payload)

    def read_raw(self):
        return b"RIGOL,DHO804\n"

    def close(self):
        self.is_closed = True


class FakeVisaManager:
    def __init__(self):
        self.resource = FakeVisaResource()
        self.closed = False

    def list_resources(self):
        return ("USB0::scope::INSTR",)

    def open_resource(self, _name):
        return self.resource

    def close(self):
        self.closed = True


class FakePyVisa:
    def __init__(self):
        self.manager = FakeVisaManager()

    def ResourceManager(self, *_args):
        return self.manager


class AcquisitionTests(unittest.TestCase):
    def test_ieee_binary_blocks_are_validated_and_joined(self):
        self.assertEqual(
            decode_ieee_binary_blocks(b"#14ABCD\n#13XYZ\n"),
            b"ABCDXYZ",
        )
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            decode_ieee_binary_blocks(b"#210short")

    def test_dho_profile_parameterizes_measurement_by_channel(self):
        profile = INSTRUMENT_PROFILES[InstrumentType.RIGOL_DHO804]
        command = profile.commands[MeasurementFunction.VOLTAGE_AVERAGE]
        self.assertEqual(command.query_for_source("CHANnel3"), ":MEASure:ITEM? VAVG,CHANnel3")
        self.assertTrue(idn_matches_profile(profile, "RIGOL TECHNOLOGIES,DHO804,ABC,1.0"))

    def test_dho_smps_setup_builds_verified_scope_commands(self):
        setup = OscilloscopeSetup()
        commands = build_dho804_setup_commands(setup)
        self.assertIn(":CHANnel1:DISPlay ON", commands)
        self.assertIn(":CHANnel2:DISPlay OFF", commands)
        self.assertIn(":CHANnel1:PROBe 10", commands)
        self.assertIn(":CHANnel1:COUPling DC", commands)
        self.assertIn(":CHANnel1:BWLimit OFF", commands)
        self.assertIn(":ACQuire:MDEPth 1M", commands)
        self.assertIn(":TRIGger:EDGE:SLOPe POSitive", commands)
        self.assertIn(":WAVeform:MODE RAW", commands)
        self.assertIn(":WAVeform:FORMat WORD", commands)
        self.assertEqual(commands[-1], ":WAVeform:STOP 1000000")

    def test_waveform_preamble_parses_all_scaling_fields(self):
        preamble = WaveformPreamble.parse(
            "1,2,1000000,1,8e-10,-0.0004,0,0.001,10,32768"
        )
        self.assertEqual(preamble.points, 1_000_000)
        self.assertEqual(preamble.format_code, 1)
        self.assertAlmostEqual(preamble.x_increment, 8e-10)
        self.assertEqual(preamble.y_reference, 32768)

    def test_waveform_logging_optimizer_trades_coverage_for_fidelity(self):
        coverage = optimise_waveform_logging(
            switching_frequency_hz=500_000,
            waveform_points=1_000_000,
            fidelity="coverage",
            only_selected_channel=True,
        )
        balanced = optimise_waveform_logging(
            switching_frequency_hz=500_000,
            waveform_points=1_000_000,
            fidelity="balanced",
            only_selected_channel=True,
        )
        edge = optimise_waveform_logging(
            switching_frequency_hz=500_000,
            waveform_points=1_000_000,
            fidelity="edge",
            only_selected_channel=True,
        )

        self.assertEqual(coverage.sample_rate_hz, 50_000_000)
        self.assertEqual(coverage.samples_per_cycle, 100)
        self.assertAlmostEqual(coverage.record_span_seconds, 0.02)
        self.assertEqual(coverage.cycles_per_capture, 10_000)
        self.assertEqual(coverage.payload_bytes, 2_000_000)
        self.assertEqual(balanced.sample_rate_hz, 125_000_000)
        self.assertAlmostEqual(balanced.time_scale_seconds, 0.0008)
        self.assertEqual(edge.sample_rate_hz, 1_250_000_000)
        self.assertAlmostEqual(edge.record_span_seconds, 0.0008)

    def test_waveform_logging_optimizer_uses_conservative_multichannel_rate(self):
        edge = optimise_waveform_logging(
            switching_frequency_hz=500_000,
            waveform_points=1_000_000,
            fidelity="edge",
            only_selected_channel=False,
        )
        self.assertEqual(edge.sample_rate_hz, 312_500_000)
        self.assertEqual(edge.samples_per_cycle, 625)

    def test_single_waveform_capture_writes_raw_data_and_metadata(self):
        class CaptureScpi:
            def __init__(self):
                self.writes = []
                self.queries = []

            def write(self, command):
                self.writes.append(command)

            def query(self, command):
                self.queries.append(command)
                if command == ":TRIGger:STATus?":
                    return "STOP"
                if command == ":WAVeform:PREamble?":
                    return "1,2,2,1,1e-9,0,0,0.01,0,32768"
                raise AssertionError(command)

            def query_binary_block(self, command):
                self.queries.append(command)
                return b"\x01\x02\x03\x04"

        scpi = CaptureScpi()
        readings = []
        completions = []
        errors = []
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "capture.bin")
            worker = WaveformCaptureWorker(
                scpi=scpi,
                clock=FakeClock(),
                instrument_index=1,
                connection="USB0::scope::INSTR",
                device_idn="RIGOL,DHO804",
                setup=OscilloscopeSetup(memory_depth="100k", waveform_points=2),
                output_path=path,
                on_reading=readings.append,
                on_complete=completions.append,
                on_error=errors.append,
            )
            worker.start()
            worker.join(timeout=1)
            with open(path, "rb") as stream:
                self.assertEqual(stream.read(), b"\x01\x02\x03\x04")
            with open(path + ".json", encoding="utf-8") as stream:
                metadata = json.load(stream)
        self.assertEqual(errors, [])
        self.assertEqual(scpi.writes, [":SINGle"])
        self.assertEqual(readings[0].function, MeasurementFunction.WAVEFORM_CAPTURE)
        self.assertEqual(readings[0].value, 2)
        self.assertEqual(metadata["word_points_received"], 2)
        self.assertIn("not specified", metadata["word_byte_order"])
        self.assertEqual(len(completions), 1)

    def test_repeated_waveform_capture_saves_each_record_and_emits_manifest_reading(self):
        class CaptureScpi:
            def __init__(self):
                self.writes = []
                self.queries = []

            def write(self, command):
                self.writes.append(command)

            def query(self, command):
                self.queries.append(command)
                if command == ":TRIGger:STATus?":
                    return "STOP"
                if command == ":WAVeform:PREamble?":
                    return "1,2,2,1,1e-9,0,0,0.01,0,32768"
                raise AssertionError(command)

            def query_binary_block(self, command):
                self.assert_command(command)
                return b"\x01\x02\x03\x04"

            @staticmethod
            def assert_command(command):
                if command != ":WAVeform:DATA?":
                    raise AssertionError(command)

        readings = []
        progress_updates = []
        completions = []
        errors = []
        worker_holder = []
        logger = None

        def on_reading(reading):
            readings.append(reading)
            logger.write_reading(reading)
            if len(readings) == 2:
                worker_holder[0].stop()

        with tempfile.TemporaryDirectory() as directory:
            scpi = CaptureScpi()
            manifest_path = os.path.join(directory, "manifest.csv")
            logger = CsvLogger(manifest_path)
            worker = RepeatedWaveformCaptureWorker(
                scpi=scpi,
                clock=FakeClock(),
                instrument_index=0,
                connection="USB0::scope::INSTR",
                device_idn="RIGOL,DHO804",
                setup=OscilloscopeSetup(memory_depth="100k", waveform_points=2),
                output_directory=directory,
                interval_seconds=0.001,
                on_reading=on_reading,
                on_complete=completions.append,
                on_error=errors.append,
                on_progress=progress_updates.append,
            )
            worker_holder.append(worker)
            worker.start()
            worker.join(timeout=1)
            logger.close()
            waveform_files = sorted(Path(directory).glob("*.bin"))
            metadata_files = sorted(Path(directory).glob("*.bin.json"))
            with metadata_files[-1].open(encoding="utf-8") as stream:
                metadata = json.load(stream)
            with open(manifest_path, newline="", encoding="utf-8") as stream:
                manifest_rows = list(csv.DictReader(stream))

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(readings), 2)
        self.assertEqual(len(waveform_files), 2)
        self.assertEqual(len(metadata_files), 2)
        self.assertEqual(scpi.writes, [":SINGle", ":SINGle"])
        self.assertEqual(scpi.queries.count(":WAVeform:PREamble?"), 1)
        self.assertEqual(metadata["capture_sequence"], 2)
        self.assertIn("timing", metadata)
        self.assertIsNotNone(metadata["timing"]["start_to_start_seconds"])
        self.assertEqual(len(progress_updates), 2)
        self.assertIsNotNone(progress_updates[1].coverage_percent)
        self.assertEqual(readings[1].raw_response, str(waveform_files[1]))
        self.assertEqual([row["function"] for row in manifest_rows], ["Waveform capture"] * 2)
        self.assertEqual(manifest_rows[1]["raw_response"], str(waveform_files[1]))
        self.assertIn("2 capture(s)", completions[0])

    def test_polling_worker_waits_for_shared_start_gate(self):
        gate = threading.Event()
        readings: list[Reading] = []
        scpi = FakeScpi()
        worker_holder = []

        def on_reading(reading):
            readings.append(reading)
            worker_holder[0].stop()

        worker = PollingWorker(
            scpi=scpi,
            clock=FakeClock(),
            instrument_index=2,
            connection="USB0::scope::INSTR",
            instrument=InstrumentType.RIGOL_DHO804,
            device_idn="RIGOL,DHO804",
            measurements=[
                PollRequest(
                    slot_index=0,
                    function=MeasurementFunction.VOLTAGE_AVERAGE,
                    query_command=":MEASure:ITEM? VAVG,CHANnel1",
                    unit="V",
                    source="CHANnel1",
                )
            ],
            interval_seconds=0.2,
            on_reading=on_reading,
            on_error=self.fail,
            start_gate=gate,
        )
        worker_holder.append(worker)
        worker.start()
        time.sleep(0.02)
        self.assertEqual(scpi.queries, [])
        gate.set()
        worker.join(timeout=1)
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0].instrument_index, 2)
        self.assertEqual(readings[0].source, "CHANnel1")
        self.assertAlmostEqual(readings[0].value, 3.3)

    def test_raw_serial_worker_preserves_line_and_parses_leading_number(self):
        readings: list[Reading] = []
        holder = []

        def on_reading(reading):
            readings.append(reading)
            holder[0].stop()

        transport = FakeRawTransport()
        worker = RawSerialWorker(
            transport=transport,
            clock=FakeClock(),
            instrument_index=3,
            connection="/dev/test",
            terminator=b"\r\n",
            on_reading=on_reading,
            on_error=self.fail,
        )
        holder.append(worker)
        worker.start()
        worker.join(timeout=1)
        self.assertEqual(readings[0].raw_response, "42.5 device-ready")
        self.assertEqual(readings[0].value, 42.5)
        self.assertEqual(transport.calls, [b"\r\n"])

    def test_visa_transport_supports_discovery_and_binary_io_contract(self):
        fake_pyvisa = FakePyVisa()
        with patch("dmm_app.transport.pyvisa", fake_pyvisa):
            self.assertEqual(VisaTransport.list_resources(), ["USB0::scope::INSTR"])
            transport = VisaTransport(VisaSettings("USB0::scope::INSTR", timeout_seconds=1.5))
            transport.open()
            transport.write(b"*IDN?\n")
            self.assertEqual(transport.read_until(b"\n"), b"RIGOL,DHO804\n")
            self.assertEqual(fake_pyvisa.manager.resource.timeout, 1500)
            self.assertTrue(transport.is_open)
            transport.close()
            self.assertFalse(transport.is_open)

    def test_shared_csv_contains_correlation_fields(self):
        reading = Reading(
            timestamp=datetime(2026, 8, 19, 12, 30, 1, 123456, tzinfo=timezone.utc),
            elapsed_seconds=0.125,
            acquisition_run=4,
            instrument_index=2,
            slot_index=1,
            instrument=InstrumentType.RIGOL_DHO804,
            connection="USB0::scope::INSTR",
            device_idn="RIGOL,DHO804",
            function=MeasurementFunction.VOLTAGE_RMS,
            source="CHANnel2",
            raw_response="1.25E+00",
            value=1.25,
            unit="V",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "shared.csv")
            logger = CsvLogger(path)
            logger.write_reading(reading)
            logger.close()
            with open(path, newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["instrument_window"], "3")
        self.assertEqual(rows[0]["acquisition_run"], "4")
        self.assertEqual(rows[0]["source"], "CHANnel2")
        self.assertEqual(rows[0]["elapsed_seconds"], "0.125000000")
        self.assertTrue(rows[0]["timestamp"].endswith("+00:00"))

    def test_clock_derives_aware_wall_time_from_monotonic_elapsed(self):
        wall = datetime(2026, 8, 19, tzinfo=timezone.utc)
        clock = AcquisitionClock(wall_start=wall, monotonic_start_ns=time.monotonic_ns())
        timestamp, elapsed, acquisition_run = clock.capture()
        self.assertIsNotNone(timestamp.tzinfo)
        self.assertGreaterEqual(elapsed, 0)
        self.assertGreaterEqual(timestamp, wall)
        self.assertEqual(acquisition_run, 0)


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_has_four_independent_panels(self):
        window = DMMAppWindow()
        self.assertEqual(len(window._panels), 4)
        self.assertEqual(
            [panel.instrument_index for panel in window._panels],
            [0, 1, 2, 3],
        )
        self.assertEqual(
            [panel._selected_instrument() for panel in window._panels],
            [InstrumentType.NONE] * 4,
        )
        self.assertEqual(
            [panel.title() for panel in window._panels],
            ["No Instrument Selected"] * 4,
        )
        window.close()
        self.app.processEvents()

    def test_each_panel_has_independent_logging_opt_in(self):
        window = DMMAppWindow()
        self.assertEqual(
            [panel._logging_checkbox.text() for panel in window._panels],
            ["Log this instrument"] * 4,
        )
        self.assertEqual(
            [panel.logging_enabled for panel in window._panels],
            [False] * 4,
        )
        window.close()
        self.app.processEvents()

    def test_csv_logging_filters_by_panel_and_shared_override(self):
        window = DMMAppWindow()

        def reading(instrument_index: int, value: float) -> Reading:
            return Reading(
                timestamp=datetime(2026, 8, 19, 12, 30, instrument_index, tzinfo=timezone.utc),
                elapsed_seconds=float(instrument_index),
                acquisition_run=1,
                instrument_index=instrument_index,
                slot_index=-1,
                instrument=InstrumentType.RIGOL_DHO804,
                connection=f"USB0::scope{instrument_index}::INSTR",
                device_idn="RIGOL,DHO804",
                function=MeasurementFunction.VOLTAGE_AVERAGE,
                source="CHANnel1",
                raw_response=str(value),
                value=value,
                unit="V",
            )

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "selected.csv")
            window._log_path_label.setText(path)
            window._panels[0]._logging_checkbox.setChecked(True)
            self.assertEqual(
                window._waveform_output_directory(0),
                os.path.join(directory, "selected_waveforms", "instrument_1"),
            )
            self.assertIsNone(window._waveform_output_directory(1))
            window._enqueue_event("reading", 0, reading(0, 1.0))
            window._enqueue_event("reading", 1, reading(1, 2.0))
            window._process_events()

            window._log_checkbox.setChecked(True)
            window._enqueue_event("reading", 0, reading(0, 3.0))
            window._enqueue_event("reading", 1, reading(1, 4.0))
            window._process_events()
            window.close()

            with open(path, newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))

        self.assertEqual([row["value"] for row in rows], ["1", "3", "4"])
        self.assertEqual([row["instrument_window"] for row in rows], ["1", "1", "2"])
        self.app.processEvents()

    def test_logging_selection_returns_to_off_when_file_choice_is_cancelled(self):
        window = DMMAppWindow()
        with patch("dmm_app.gui.QFileDialog.getSaveFileName", return_value=("", "")):
            window._panels[0]._logging_checkbox.setChecked(True)
            self.assertFalse(window._panels[0].logging_enabled)
            window._log_checkbox.setChecked(True)
            self.assertFalse(window._log_checkbox.isChecked())
        self.assertIsNone(window._logger)
        window.close()
        self.app.processEvents()

    def test_panel_title_follows_selected_instrument(self):
        window = DMMAppWindow()
        panel = window._panels[0]
        panel._instrument_combo.setCurrentText(InstrumentType.RIGOL_DHO804.value)
        self.assertEqual(panel.title(), InstrumentType.RIGOL_DHO804.value)
        panel._instrument_combo.setCurrentText(InstrumentType.NONE.value)
        self.assertEqual(panel.title(), "No Instrument Selected")
        window.close()
        self.app.processEvents()

    def test_dho_profile_reveals_scope_setup_and_calculates_timebase(self):
        window = DMMAppWindow()
        panel = window._panels[0]
        self.assertTrue(panel._scope_section.isHidden())
        panel._instrument_combo.setCurrentText(InstrumentType.RIGOL_DHO804.value)
        self.assertFalse(panel._scope_section.isHidden())
        self.assertFalse(panel._scope_section.is_expanded)
        panel._scope_frequency_input.setText("500")
        panel._scope_cycles_combo.setCurrentText("5")
        panel._calculate_scope_timebase()
        self.assertEqual(panel._scope_time_scale_input.text(), "1e-06")
        self.assertEqual(panel._scope_memory_combo.currentText(), "1M")
        self.assertEqual(panel._scope_points_input.text(), "1000000")
        self.assertFalse(panel._acquisition_mode_combo.isHidden())
        panel._acquisition_mode_combo.setCurrentIndex(1)
        self.assertTrue(panel._waveform_logging_selected())
        self.assertEqual(panel._interval_label.text(), "Min interval (ms)")
        self.assertFalse(panel._measurement_rows[0].function_combo.isEnabled())
        window.close()
        self.app.processEvents()

    def test_dho_logging_optimizer_applies_balanced_zero_delay_plan(self):
        window = DMMAppWindow()
        panel = window._panels[0]
        panel._instrument_combo.setCurrentText(InstrumentType.RIGOL_DHO804.value)
        panel._scope_frequency_input.setText("500")
        panel._scope_points_input.setText("1000000")
        panel._scope_logging_fidelity_combo.setCurrentIndex(
            panel._scope_logging_fidelity_combo.findData("balanced")
        )

        panel._optimise_scope_logging()

        self.assertEqual(panel._scope_time_scale_input.text(), "0.0008")
        self.assertEqual(panel._interval_input.text(), "0")
        self.assertTrue(panel._waveform_logging_selected())
        self.assertEqual(panel._validated_interval_seconds(), 0.0)
        self.assertIn("125 MSa/s", panel._scope_logging_estimate_label.text())
        self.assertIn("4,000 cycles", panel._scope_logging_estimate_label.text())
        window.close()
        self.app.processEvents()

    def test_each_panel_profile_and_measurement_sections_collapse_independently(self):
        window = DMMAppWindow()
        for panel in window._panels:
            self.assertTrue(panel._profile_section.is_expanded)
            self.assertTrue(panel._measurement_section.is_expanded)
            panel._profile_section.toggle_button.click()
            self.assertFalse(panel._profile_section.is_expanded)
            self.assertTrue(panel._measurement_section.is_expanded)
            panel._measurement_section.toggle_button.click()
            self.assertFalse(panel._measurement_section.is_expanded)
            self.assertEqual(panel._profile_section.toggle_button.text(), "+")
            self.assertEqual(panel._measurement_section.toggle_button.text(), "+")
        window.close()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
