from __future__ import annotations

import csv
import json
import os
import struct
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from dmm_app.clock import AcquisitionClock
from dmm_app.commands import INSTRUMENT_PROFILES, idn_matches_profile
from dmm_app.gui import DMMAppWindow
from dmm_app.logging_util import CsvLogger
from dmm_app.models import InstrumentType, MeasurementFunction, Reading, VisaSettings
from dmm_app.poller import PollRequest, PollingWorker, RawSerialWorker
from dmm_app.plotting import WaveformPlotData
from dmm_app.oscilloscope import (
    OscilloscopeSetup,
    RepeatedWaveformCaptureWorker,
    WaveformCaptureWorker,
    WaveformPreamble,
    build_dho804_setup_commands,
    optimise_waveform_logging,
)
from dmm_app.scpi import IEEEBinaryBlockError, SCPIClient, decode_ieee_binary_blocks
from dmm_app.scope_tools import BurstDiagnosticSummary, BurstDiagnosticWorker
from dmm_app.transport import SerialTransport, VisaTransport


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

    def read_raw(self, size=None):
        del size
        return b"RIGOL,DHO804\n"

    def close(self):
        self.is_closed = True


class FakeVisaManager:
    def __init__(self, resource=None):
        self.resource = resource or FakeVisaResource()
        self.closed = False

    def list_resources(self):
        return ("USB0::scope::INSTR",)

    def open_resource(self, _name):
        return self.resource

    def close(self):
        self.closed = True


class FakePyVisa:
    def __init__(self, resource=None):
        self.manager = FakeVisaManager(resource)

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
        with self.assertRaisesRegex(ValueError, "empty"):
            decode_ieee_binary_blocks(b"#10\n")

    def test_visa_binary_query_reads_the_complete_announced_block(self):
        payload = bytes(range(256)) * 7_813
        payload = payload[:2_000_000]
        response = b"#9002000000" + payload + b"\n\x00"

        class ChunkedVisaResource(FakeVisaResource):
            def __init__(self):
                super().__init__()
                self._chunks = [response[:20_480], response[20_480:]]
                self.read_sizes = []

            def read_raw(self, size=None):
                self.read_sizes.append(size)
                return self._chunks.pop(0)

        resource = ChunkedVisaResource()
        with patch("dmm_app.transport.pyvisa", FakePyVisa(resource)):
            transport = VisaTransport(VisaSettings("USB0::scope::INSTR", timeout_seconds=1.5))
            transport.open()
            client = SCPIClient(transport)
            received = client.query_binary_block(":WAVeform:DATA?")
            transport.close()

        self.assertEqual(received, payload)
        self.assertEqual(resource.writes, [b":WAVeform:DATA?\n"])
        self.assertEqual(resource.read_sizes, [None, 1_979_533])

    def test_visa_binary_query_classifies_interrupted_block_and_can_clear_stream(self):
        class InterruptedVisaResource(FakeVisaResource):
            def __init__(self):
                super().__init__()
                self._chunks = [b"#21012345", b""]
                self.clear_count = 0

            def read_raw(self, size=None):
                del size
                return self._chunks.pop(0)

            def clear(self):
                self.clear_count += 1

        resource = InterruptedVisaResource()
        with patch("dmm_app.transport.pyvisa", FakePyVisa(resource)):
            transport = VisaTransport(VisaSettings("USB0::scope::INSTR", timeout_seconds=1.5))
            transport.open()
            client = SCPIClient(transport)
            with self.assertRaisesRegex(IEEEBinaryBlockError, "ended before"):
                client.query_binary_block(":WAVeform:DATA?")
            client.recover_binary_transfer()
            transport.close()

        self.assertEqual(resource.clear_count, 1)

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
        self.assertIn("little-endian", metadata["word_byte_order"])
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
                interval_seconds=0.05,
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

    def test_repeated_waveform_capture_recovers_from_an_ieee_transfer_error(self):
        class RecoveringScpi:
            def __init__(self):
                self.binary_queries = 0
                self.recoveries = 0
                self.writes = []

            def write(self, command):
                self.writes.append(command)

            def query(self, command):
                if command == ":TRIGger:STATus?":
                    return "STOP"
                if command == ":WAVeform:PREamble?":
                    return "1,2,2,1,1e-9,0,0,0.01,0,32768"
                raise AssertionError(command)

            def query_binary_block(self, command):
                self.assert_command(command)
                self.binary_queries += 1
                if self.binary_queries == 1:
                    raise IEEEBinaryBlockError("incomplete test block")
                return b"\x01\x02\x03\x04"

            def recover_binary_transfer(self):
                self.recoveries += 1

            @staticmethod
            def assert_command(command):
                if command != ":WAVeform:DATA?":
                    raise AssertionError(command)

        readings = []
        warnings = []
        errors = []
        worker_holder = []

        def on_reading(reading):
            readings.append(reading)
            worker_holder[0].stop()

        with tempfile.TemporaryDirectory() as directory:
            scpi = RecoveringScpi()
            worker = RepeatedWaveformCaptureWorker(
                scpi=scpi,
                clock=FakeClock(),
                instrument_index=0,
                connection="USB0::scope::INSTR",
                device_idn="RIGOL,DHO804",
                setup=OscilloscopeSetup(memory_depth="100k", waveform_points=2),
                output_directory=directory,
                interval_seconds=0.05,
                on_reading=on_reading,
                on_complete=lambda _message: None,
                on_error=errors.append,
                on_warning=warnings.append,
            )
            worker_holder.append(worker)
            worker.start()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(readings), 1)
        self.assertEqual(scpi.recoveries, 1)
        self.assertEqual(len(warnings), 1)
        self.assertIn("retrying (1/3)", warnings[0])
        self.assertIn(":WAVeform:MODE RAW", scpi.writes)
        self.assertEqual(scpi.writes.count(":SINGle"), 2)

    def test_repeated_waveform_capture_stops_after_three_ieee_failures(self):
        class BrokenScpi:
            def __init__(self):
                self.recoveries = 0

            def write(self, _command):
                pass

            def query(self, command):
                if command == ":TRIGger:STATus?":
                    return "STOP"
                if command == ":WAVeform:PREamble?":
                    return "1,2,2,1,1e-9,0,0,0.01,0,32768"
                raise AssertionError(command)

            def query_binary_block(self, _command):
                raise IEEEBinaryBlockError("bad test header")

            def recover_binary_transfer(self):
                self.recoveries += 1

        warnings = []
        errors = []
        with tempfile.TemporaryDirectory() as directory:
            scpi = BrokenScpi()
            worker = RepeatedWaveformCaptureWorker(
                scpi=scpi,
                clock=FakeClock(),
                instrument_index=0,
                connection="USB0::scope::INSTR",
                device_idn="RIGOL,DHO804",
                setup=OscilloscopeSetup(memory_depth="100k", waveform_points=2),
                output_directory=directory,
                interval_seconds=0,
                on_reading=lambda _reading: None,
                on_complete=lambda _message: None,
                on_error=errors.append,
                on_warning=warnings.append,
            )
            worker.start()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(scpi.recoveries, 2)
        self.assertEqual(len(warnings), 2)
        self.assertEqual(len(errors), 1)
        self.assertIn("3 consecutive times", errors[0])

    def test_burst_diagnostic_selects_and_saves_each_recorded_frame(self):
        class RecordScpi:
            def __init__(self):
                self.current_frame = 0
                self.writes = []

            def write(self, command):
                self.writes.append(command)
                if command.startswith(":RECord:WREPlay:FCURrent "):
                    self.current_frame = int(command.rsplit(" ", 1)[1])

            def query(self, command):
                if command == ":RECord:WRECord:FMAX?":
                    return "25"
                if command == ":RECord:WRECord:OPERate?":
                    return "STOP"
                if command == ":RECord:WREPlay:FMAX?":
                    return "2"
                if command == ":RECord:WREPlay:FCURrent?":
                    return str(self.current_frame)
                if command == ":RECord:WREPlay:FCURrent:TIME?":
                    return f"FRAME_TIME_{self.current_frame}"
                if command == ":WAVeform:PREamble?":
                    return "1,2,4,1,1e-9,0,0,0.01,0,32768"
                raise AssertionError(command)

            def query_binary_block(self, command):
                if command != ":WAVeform:DATA?":
                    raise AssertionError(command)
                return bytes([self.current_frame, 0]) * 4

            def recover_binary_transfer(self):
                raise AssertionError("Recovery should not be needed")

        events = []
        with tempfile.TemporaryDirectory() as directory:
            scpi = RecordScpi()
            worker = BurstDiagnosticWorker(
                scpi=scpi,
                setup=OscilloscopeSetup(memory_depth="100k", waveform_points=4),
                connection="USB0::scope::INSTR",
                device_idn="RIGOL,DHO804",
                requested_frames=2,
                frame_interval_seconds=1e-8,
                record_timeout_seconds=1,
                output_parent=directory,
                on_event=lambda kind, payload: events.append((kind, payload)),
            )
            worker.start()
            worker.join(timeout=2)
            summaries = [payload for kind, payload in events if kind == "summary"]
            self.assertEqual(len(summaries), 1)
            summary = summaries[0]
            self.assertIsInstance(summary, BurstDiagnosticSummary)
            self.assertEqual(summary.maximum_frames, 25)
            self.assertEqual(summary.recorded_frames, 2)
            self.assertEqual(len(summary.frames), 2)
            self.assertNotEqual(summary.frames[0].sha256, summary.frames[1].sha256)
            self.assertEqual(summary.frames[0].hardware_timestamp, "FRAME_TIME_1")
            self.assertEqual(Path(summary.frames[0].waveform_path).read_bytes(), b"\x01\x00" * 4)
            self.assertTrue(Path(summary.output_directory, "diagnostic.json").exists())

        self.assertFalse(worker.is_alive())
        self.assertIn(":RECord:WRECord:FRAMes 2", scpi.writes)
        self.assertIn(":RECord:WRECord:FINTerval 1e-08", scpi.writes)
        self.assertEqual(
            [command for command in scpi.writes if command.startswith(":RECord:WREPlay:FCURrent ")],
            [":RECord:WREPlay:FCURrent 1", ":RECord:WREPlay:FCURrent 2"],
        )

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

    def test_waveform_plot_data_decodes_little_endian_words_and_preamble(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "waveform.bin"
            words = (32_768, 34_268, 31_768, 32_768)
            path.write_bytes(struct.pack("<4H", *words))
            metadata = {
                "source": "CHANnel1",
                "word_points_received": 4,
                "setup": {
                    "time_scale_seconds": 1e-6,
                    "vertical_scale_volts": 0.5,
                    "trigger_level_volts": 0.25,
                },
                "preamble": {
                    "x_increment": 1e-9,
                    "x_origin": -2e-9,
                    "x_reference": 0,
                    "y_increment": 0.001,
                    "y_origin": 0,
                    "y_reference": 32_768,
                },
            }
            path.with_suffix(".bin.json").write_text(json.dumps(metadata), encoding="utf-8")
            waveform = WaveformPlotData.load(str(path))

        self.assertEqual(list(waveform.words), list(words))
        self.assertAlmostEqual(waveform.time_at(2), 0.0)
        self.assertAlmostEqual(waveform.voltage_for_word(34_268), 1.5)
        self.assertAlmostEqual(waveform.voltage_for_word(31_768), -1.0)
        self.assertEqual(waveform.scope_view_indices(), (0, 4))
        self.assertEqual(len(waveform.full_envelope), 4)

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
        self.assertEqual(window._save_config_button.text(), "Save config…")
        self.assertEqual(window._load_config_button.text(), "Load config…")
        window.close()
        self.app.processEvents()

    def test_configuration_file_round_trip_restores_panels_without_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            configuration_path = os.path.join(directory, "bench-setup.json")
            shared_log_path = os.path.join(directory, "bench-readings.csv")

            source = DMMAppWindow()
            owon = source._panels[0]
            owon._instrument_combo.setCurrentText(InstrumentType.OWON_SPE6103.value)
            owon._endpoint_combo.setCurrentText("/dev/cu.owon-test")
            owon._baud_combo.setCurrentText("115200")
            owon._interval_input.setText("250")
            owon._add_measurement()
            owon._profile_section.set_expanded(False)

            scope = source._panels[1]
            scope._instrument_combo.setCurrentText(InstrumentType.RIGOL_DHO804.value)
            scope._endpoint_combo.setCurrentText("USB0::saved-scope::INSTR")
            scope._acquisition_mode_combo.setCurrentIndex(
                scope._acquisition_mode_combo.findData("waveforms")
            )
            scope._interval_input.setText("0")
            scope._scope_only_channel_checkbox.setChecked(False)
            scope._scope_channel_combo.setCurrentText("CHANnel3")
            scope._scope_probe_combo.setCurrentText("20")
            scope._scope_coupling_combo.setCurrentText("AC")
            scope._scope_bandwidth_combo.setCurrentIndex(
                scope._scope_bandwidth_combo.findData("20M")
            )
            scope._scope_vertical_scale_input.setText("2.5")
            scope._scope_frequency_input.setText("1400")
            scope._scope_cycles_combo.setCurrentText("3")
            scope._scope_time_scale_input.setText("2e-7")
            scope._scope_memory_combo.setCurrentText("5M")
            scope._scope_points_input.setText("123456")
            scope._scope_acquisition_combo.setCurrentIndex(
                scope._scope_acquisition_combo.findData("PEAK")
            )
            scope._scope_trigger_slope_combo.setCurrentIndex(
                scope._scope_trigger_slope_combo.findData("NEGative")
            )
            scope._scope_trigger_level_input.setText("1.25")
            scope._scope_trigger_timeout_input.setText("12")
            scope._scope_logging_fidelity_combo.setCurrentIndex(
                scope._scope_logging_fidelity_combo.findData("edge")
            )
            scope._scope_safety_checkbox.setChecked(True)
            scope._scope_section.set_expanded(True)
            scope._toggle_output_view()
            scope._plot_range_combo.setCurrentIndex(
                scope._plot_range_combo.findData("full")
            )

            tc08 = source._panels[2]
            tc08._instrument_combo.setCurrentText(InstrumentType.PICOLOG_TC08.value)
            tc08._tc08_mains_combo.setCurrentIndex(tc08._tc08_mains_combo.findData(60))
            tc08._tc08_units_combo.setCurrentIndex(tc08._tc08_units_combo.findData("F"))
            tc08._tc08_channel_type_combos[0].setCurrentIndex(
                tc08._tc08_channel_type_combos[0].findData("")
            )
            tc08._tc08_channel_type_combos[7].setCurrentIndex(
                tc08._tc08_channel_type_combos[7].findData("T")
            )
            tc08._measurement_rows[0].source_combo.setCurrentText("Channel 8")
            tc08._tc08_section.set_expanded(False)

            with patch(
                "dmm_app.gui.QFileDialog.getSaveFileName",
                return_value=(shared_log_path, "CSV files (*.csv)"),
            ):
                source._log_checkbox.setChecked(True)
                owon._logging_checkbox.setChecked(True)
                scope._logging_checkbox.setChecked(True)
            source._write_configuration_file(configuration_path)
            source.close()
            self.app.processEvents()

            restored = DMMAppWindow()
            restored._read_configuration_file(configuration_path)

            restored_owon = restored._panels[0]
            self.assertEqual(restored_owon._selected_instrument(), InstrumentType.OWON_SPE6103)
            self.assertEqual(restored_owon._endpoint_combo.currentText(), "/dev/cu.owon-test")
            self.assertEqual(restored_owon._baud_combo.currentText(), "115200")
            self.assertEqual(restored_owon._interval_input.text(), "250")
            self.assertEqual(
                [row.function_combo.currentText() for row in restored_owon._measurement_rows],
                [MeasurementFunction.VOLTAGE.value, MeasurementFunction.CURRENT.value],
            )
            self.assertFalse(restored_owon._profile_section.is_expanded)

            restored_scope = restored._panels[1]
            self.assertEqual(restored_scope._selected_instrument(), InstrumentType.RIGOL_DHO804)
            self.assertEqual(
                restored_scope._endpoint_combo.currentText(), "USB0::saved-scope::INSTR"
            )
            self.assertTrue(restored_scope._waveform_logging_selected())
            self.assertEqual(restored_scope._interval_input.text(), "0")
            self.assertFalse(restored_scope._scope_only_channel_checkbox.isChecked())
            self.assertEqual(restored_scope._scope_channel_combo.currentText(), "CHANnel3")
            self.assertEqual(restored_scope._scope_probe_combo.currentText(), "20")
            self.assertEqual(restored_scope._scope_coupling_combo.currentText(), "AC")
            self.assertEqual(restored_scope._scope_bandwidth_combo.currentData(), "20M")
            self.assertEqual(restored_scope._scope_vertical_scale_input.text(), "2.5")
            self.assertEqual(restored_scope._scope_frequency_input.text(), "1400")
            self.assertEqual(restored_scope._scope_memory_combo.currentText(), "5M")
            self.assertEqual(restored_scope._scope_points_input.text(), "123456")
            self.assertEqual(restored_scope._scope_acquisition_combo.currentData(), "PEAK")
            self.assertEqual(restored_scope._scope_trigger_slope_combo.currentData(), "NEGative")
            self.assertEqual(restored_scope._scope_logging_fidelity_combo.currentData(), "edge")
            self.assertIs(restored_scope._output_stack.currentWidget(), restored_scope._plot)
            self.assertEqual(restored_scope._plot_range_combo.currentData(), "full")
            self.assertFalse(restored_scope._scope_safety_checkbox.isChecked())
            self.assertIsNone(restored_scope._scope_setup_applied)

            restored_tc08 = restored._panels[2]
            self.assertEqual(restored_tc08._selected_instrument(), InstrumentType.PICOLOG_TC08)
            self.assertEqual(restored_tc08._endpoint_combo.currentText(), "First available USB TC-08")
            self.assertEqual(restored_tc08._tc08_mains_combo.currentData(), 60)
            self.assertEqual(restored_tc08._tc08_units_combo.currentData(), "F")
            self.assertEqual(
                [combo.currentData() for combo in restored_tc08._tc08_channel_type_combos],
                ["", "", "", "", "", "", "", "T"],
            )
            self.assertEqual(
                restored_tc08._measurement_rows[0].source_combo.currentText(), "Channel 8"
            )
            self.assertFalse(restored_tc08._tc08_section.is_expanded)

            self.assertTrue(restored._log_checkbox.isChecked())
            self.assertEqual(restored._shared_log_path, shared_log_path)
            self.assertEqual(
                [panel.logging_enabled for panel in restored._panels],
                [True, True, False, False],
            )
            self.assertFalse(any(panel.is_connected for panel in restored._panels))
            self.assertFalse(any(panel.is_running for panel in restored._panels))

            restored.close()
            self.app.processEvents()

    def test_load_configuration_rejects_invalid_version_without_changing_panels(self):
        window = DMMAppWindow()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "future.json")
            Path(path).write_text(
                json.dumps(
                    {
                        "format": "scpi-lab-instrument-config",
                        "version": 999,
                        "application": {},
                        "panels": [],
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch(
                    "dmm_app.gui.QFileDialog.getOpenFileName",
                    return_value=(path, "JSON configuration (*.json)"),
                ),
                patch("dmm_app.gui.QMessageBox.critical") as critical,
                patch("dmm_app.gui.QMessageBox.information") as information,
            ):
                window._load_configuration()

        self.assertEqual(critical.call_count, 1)
        self.assertIn("Unsupported configuration version", critical.call_args.args[2])
        self.assertEqual(information.call_count, 0)
        self.assertEqual(
            [panel._selected_instrument() for panel in window._panels],
            [InstrumentType.NONE] * 4,
        )
        window.close()
        self.app.processEvents()

    def test_load_configuration_is_blocked_while_an_instrument_is_connected(self):
        class ConnectedTransport:
            is_open = True

            def close(self):
                self.is_open = False

        window = DMMAppWindow()
        window._panels[0]._transport = ConnectedTransport()
        with (
            patch("dmm_app.gui.QMessageBox.warning") as warning,
            patch("dmm_app.gui.QFileDialog.getOpenFileName") as choose_file,
        ):
            window._load_configuration()

        self.assertEqual(warning.call_count, 1)
        self.assertIn("disconnect", warning.call_args.args[2].lower())
        self.assertEqual(choose_file.call_count, 0)
        window.close()
        self.app.processEvents()

    def test_profile_change_uses_cached_endpoints_without_visa_rediscovery(self):
        with (
            patch.object(
                SerialTransport,
                "list_serial_ports",
                return_value=["/dev/cu.test"],
            ) as serial_discovery,
            patch.object(
                VisaTransport,
                "list_resources",
                return_value=["USB0::scope::INSTR"],
            ) as visa_discovery,
        ):
            window = DMMAppWindow()
            self.assertEqual(serial_discovery.call_count, 1)
            self.assertEqual(visa_discovery.call_count, 1)

            serial_panel = window._panels[1]
            serial_panel._instrument_combo.setCurrentText(InstrumentType.MP730889.value)
            self.assertEqual(serial_panel._endpoint_combo.currentText(), "/dev/cu.test")

            visa_panel = window._panels[2]
            visa_panel._instrument_combo.setCurrentText(InstrumentType.RIGOL_DHO804.value)
            self.assertEqual(visa_panel._endpoint_combo.currentText(), "USB0::scope::INSTR")

            self.assertEqual(serial_discovery.call_count, 1)
            self.assertEqual(visa_discovery.call_count, 1)

            class RunningWorker:
                @staticmethod
                def is_alive():
                    return True

            active_worker = RunningWorker()
            visa_panel._worker = active_worker
            other_panel = window._panels[3]
            other_panel._instrument_combo.setCurrentText(InstrumentType.OWON_SPE6103.value)
            self.assertIs(visa_panel._worker, active_worker)
            self.assertEqual(visa_discovery.call_count, 1)

            window._refresh_endpoints()
            self.assertEqual(serial_discovery.call_count, 2)
            self.assertEqual(visa_discovery.call_count, 1)
            self.assertEqual(visa_panel._endpoint_combo.currentText(), "USB0::scope::INSTR")
            visa_panel._worker = None
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

    def test_panel_toggles_between_text_and_graph_views(self):
        window = DMMAppWindow()
        panel = window._panels[0]
        panel._instrument_combo.setCurrentText(InstrumentType.MP730889.value)
        self.assertIs(panel._output_stack.currentWidget(), panel._output)
        self.assertEqual(panel._view_toggle_button.text(), "Graph view")

        panel._view_toggle_button.click()

        self.assertIs(panel._output_stack.currentWidget(), panel._plot)
        self.assertEqual(panel._view_toggle_button.text(), "Text view")
        self.assertFalse(panel._profile_section.is_expanded)
        self.assertFalse(panel._measurement_section.is_expanded)
        panel._view_toggle_button.click()
        self.assertIs(panel._output_stack.currentWidget(), panel._output)
        self.assertTrue(panel._profile_section.is_expanded)
        self.assertTrue(panel._measurement_section.is_expanded)
        window.close()
        self.app.processEvents()

    def test_graph_live_indicator_requires_fresh_streamed_data(self):
        window = DMMAppWindow()
        plot = window._panels[0]._plot
        with patch("dmm_app.plotting.time.monotonic", return_value=100.0):
            plot.set_stream_expected(True, stale_after_seconds=2.5)
            self.assertFalse(plot.data_is_live)
            plot.note_live_reading()
            self.assertTrue(plot.data_is_live)
        with patch("dmm_app.plotting.time.monotonic", return_value=103.0):
            self.assertFalse(plot.data_is_live)
        plot.set_stream_expected(False)
        self.assertFalse(plot.data_is_live)
        window.close()
        self.app.processEvents()

    def test_dho_tools_window_reuses_panel_scpi_and_queries_maximum_frames(self):
        class OpenTransport:
            is_open = True

        class ToolsScpi:
            def __init__(self):
                self.queries = []

            def query(self, command):
                self.queries.append(command)
                if command == ":RECord:WRECord:FMAX?":
                    return "321"
                raise AssertionError(command)

            def write(self, _command):
                pass

        window = DMMAppWindow()
        panel = window._panels[0]
        panel._instrument_combo.setCurrentText(InstrumentType.RIGOL_DHO804.value)
        scpi = ToolsScpi()
        panel._transport = OpenTransport()
        panel._scpi = scpi
        panel._device_idn = "RIGOL,DHO804"
        panel._scope_setup_applied = OscilloscopeSetup()
        panel._refresh_controls()

        panel._scope_tools_button.click()
        dialog = panel._scope_tools_dialog
        self.assertIsNotNone(dialog)
        self.assertFalse(dialog.isModal())
        self.assertEqual(dialog._tabs.count(), 3)
        self.assertIs(dialog._scpi_provider(), scpi)

        dialog._query_maximum_button.click()
        deadline = time.monotonic() + 1
        while dialog.is_busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        dialog._process_events()

        self.assertFalse(dialog.is_busy)
        self.assertEqual(dialog._maximum_frames_label.text(), "321")
        self.assertEqual(scpi.queries, [":RECord:WRECord:FMAX?"])

        class RunningWorker:
            @staticmethod
            def is_alive():
                return True

        panel._worker = RunningWorker()
        dialog._query_maximum_frames()
        self.assertFalse(dialog.is_busy)
        self.assertEqual(scpi.queries, [":RECord:WRECord:FMAX?"])
        panel._worker = None
        panel._transport = None
        panel._scpi = None
        window.close()
        self.app.processEvents()

    def test_owon_graph_uses_only_first_two_readings_as_independent_axes(self):
        window = DMMAppWindow()
        panel = window._panels[0]
        panel._instrument_combo.setCurrentText(InstrumentType.OWON_SPE6103.value)

        def reading(slot: int, function: MeasurementFunction, value: float, unit: str):
            return Reading(
                timestamp=datetime(2026, 8, 19, tzinfo=timezone.utc),
                elapsed_seconds=10.0 + slot,
                acquisition_run=2,
                instrument_index=0,
                slot_index=slot,
                instrument=InstrumentType.OWON_SPE6103,
                connection="/dev/test",
                device_idn="OWON,SPE6103",
                function=function,
                source="",
                raw_response=str(value),
                value=value,
                unit=unit,
            )

        panel.consume_reading(reading(0, MeasurementFunction.VOLTAGE, 48.0, "V"))
        panel.consume_reading(reading(1, MeasurementFunction.CURRENT, 0.25, "A"))
        panel.consume_reading(reading(2, MeasurementFunction.CURRENT, 0.5, "A"))

        self.assertEqual(set(panel._plot.traces), {0, 1})
        self.assertEqual(panel._plot.traces[0].unit, "V")
        self.assertEqual(list(panel._plot.traces[0].points), [(10.0, 48.0)])
        self.assertEqual(panel._plot.traces[1].unit, "A")
        self.assertEqual(list(panel._plot.traces[1].points), [(11.0, 0.25)])
        window.close()
        self.app.processEvents()

    def test_shared_csv_logs_only_selected_panels_without_more_file_prompts(self):
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
            path = os.path.join(directory, "shared.csv")
            with patch(
                "dmm_app.gui.QFileDialog.getSaveFileName",
                return_value=(path, "CSV files (*.csv)"),
            ) as choose_file:
                window._log_checkbox.setChecked(True)
                window._panels[0]._logging_checkbox.setChecked(True)
                window._panels[2]._logging_checkbox.setChecked(True)

            self.assertEqual(choose_file.call_count, 1)
            self.assertEqual(window._shared_log_path, path)
            self.assertEqual(
                window._waveform_output_directory(0),
                os.path.join(directory, "shared_waveforms", "instrument_1"),
            )
            self.assertIsNone(window._waveform_output_directory(1))
            self.assertEqual(
                window._waveform_output_directory(2),
                os.path.join(directory, "shared_waveforms", "instrument_3"),
            )
            window._enqueue_event("reading", 0, reading(0, 1.0))
            window._enqueue_event("reading", 1, reading(1, 2.0))
            window._enqueue_event("reading", 2, reading(2, 3.0))
            window._process_events()
            window.close()

            with open(path, newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))

        self.assertEqual([row["value"] for row in rows], ["1", "3"])
        self.assertEqual([row["instrument_window"] for row in rows], ["1", "3"])
        self.app.processEvents()

    def test_individual_logging_prompts_for_and_uses_one_file_per_panel(self):
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
            first_path = os.path.join(directory, "instrument-one.csv")
            third_path = os.path.join(directory, "instrument-three.csv")
            with patch(
                "dmm_app.gui.QFileDialog.getSaveFileName",
                side_effect=[
                    (first_path, "CSV files (*.csv)"),
                    (third_path, "CSV files (*.csv)"),
                ],
            ) as choose_file:
                window._panels[0]._logging_checkbox.setChecked(True)
                window._panels[2]._logging_checkbox.setChecked(True)

            self.assertEqual(choose_file.call_count, 2)
            self.assertEqual(window._panel_log_paths, {0: first_path, 2: third_path})
            self.assertEqual(
                window._waveform_output_directory(0),
                os.path.join(directory, "instrument-one_waveforms", "instrument_1"),
            )
            self.assertIsNone(window._waveform_output_directory(1))
            self.assertEqual(
                window._waveform_output_directory(2),
                os.path.join(directory, "instrument-three_waveforms", "instrument_3"),
            )
            window._enqueue_event("reading", 0, reading(0, 11.0))
            window._enqueue_event("reading", 1, reading(1, 22.0))
            window._enqueue_event("reading", 2, reading(2, 33.0))
            window._process_events()
            window.close()

            with open(first_path, newline="", encoding="utf-8") as stream:
                first_rows = list(csv.DictReader(stream))
            with open(third_path, newline="", encoding="utf-8") as stream:
                third_rows = list(csv.DictReader(stream))

        self.assertEqual([row["value"] for row in first_rows], ["11"])
        self.assertEqual([row["value"] for row in third_rows], ["33"])
        self.app.processEvents()

    def test_disabling_shared_csv_confirms_and_clears_panel_selections(self):
        window = DMMAppWindow()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "shared.csv")
            with patch(
                "dmm_app.gui.QFileDialog.getSaveFileName",
                return_value=(path, "CSV files (*.csv)"),
            ):
                window._log_checkbox.setChecked(True)
                window._panels[0]._logging_checkbox.setChecked(True)
                window._panels[2]._logging_checkbox.setChecked(True)

            with patch(
                "dmm_app.gui.QMessageBox.question", return_value=QMessageBox.Cancel
            ):
                window._log_checkbox.setChecked(False)
            self.assertTrue(window._log_checkbox.isChecked())
            self.assertTrue(window._panels[0].logging_enabled)
            self.assertTrue(window._panels[2].logging_enabled)

            with patch(
                "dmm_app.gui.QMessageBox.question", return_value=QMessageBox.Yes
            ) as confirmation:
                window._log_checkbox.setChecked(False)

            self.assertEqual(confirmation.call_count, 1)
            self.assertFalse(window._log_checkbox.isChecked())
            self.assertEqual(
                [panel.logging_enabled for panel in window._panels],
                [False] * 4,
            )
            self.assertEqual(window._shared_log_path, path)
            self.assertIsNone(window._shared_logger)
            self.assertEqual(window._panel_log_paths, {})

            individual_path = os.path.join(directory, "instrument-one.csv")
            with patch(
                "dmm_app.gui.QFileDialog.getSaveFileName",
                return_value=(individual_path, "CSV files (*.csv)"),
            ) as choose_individual:
                window._panels[0]._logging_checkbox.setChecked(True)
            self.assertEqual(choose_individual.call_count, 1)
            self.assertEqual(window._panel_log_paths, {0: individual_path})
            window.close()
        self.app.processEvents()

    def test_logging_selection_returns_to_off_when_file_choice_is_cancelled(self):
        window = DMMAppWindow()
        with patch("dmm_app.gui.QFileDialog.getSaveFileName", return_value=("", "")):
            window._panels[0]._logging_checkbox.setChecked(True)
            self.assertFalse(window._panels[0].logging_enabled)
            window._log_checkbox.setChecked(True)
            self.assertFalse(window._log_checkbox.isChecked())
        self.assertIsNone(window._shared_logger)
        self.assertEqual(window._panel_loggers, {})
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

    def test_tc08_profile_exposes_channel_types_units_and_measurement_rows(self):
        window = DMMAppWindow()
        panel = window._panels[0]
        panel._instrument_combo.setCurrentText(InstrumentType.PICOLOG_TC08.value)

        self.assertFalse(panel._tc08_section.isHidden())
        self.assertTrue(panel._scope_section.isHidden())
        self.assertEqual(panel._endpoint_label.text(), "Device")
        self.assertEqual(panel._endpoint_combo.currentText(), "First available USB TC-08")
        self.assertEqual(panel._measurement_rows[0].source_combo.currentText(), "Channel 1")
        self.assertEqual(panel._tc08_channel_type_combos[0].currentData(), "K")
        self.assertEqual(panel._tc08_units_combo.currentData(), "C")

        panel._tc08_channel_type_combos[1].setCurrentIndex(
            panel._tc08_channel_type_combos[1].findData("J")
        )
        panel._add_button.click()
        self.assertEqual(panel._measurement_rows[1].source_combo.currentText(), "Channel 2")
        configuration = panel.configuration_dict()
        self.assertEqual(configuration["tc08"]["channel_types"][:2], ["K", "J"])

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
