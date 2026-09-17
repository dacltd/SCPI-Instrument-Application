import csv
from decimal import Decimal
import threading
from unittest.mock import Mock, patch

import pytest
from PySide6.QtWidgets import QApplication

from dmm_app.battery_models import BatteryModelDownloadWorker, read_model, save_model
from dmm_app.gui import DMMAppWindow
from dmm_app.models import InstrumentType


class ModelInstrument:
    def __init__(self):
        self.queries = []

    def query(self, command):
        self.queries.append(command)
        if command.endswith("STEPs?"):
            return "+1.010000E+02"
        if command.endswith("VOC?"):
            return '"' + ','.join(str(Decimal("3.0") + Decimal(i) / 100) for i in range(101)) + '"'
        return ','.join("0.05" for _ in range(101))


def test_model_download_queries_selected_slot_and_writes_complete_csv(tmp_path):
    instrument = ModelInstrument()
    destination = tmp_path / "Chosen folder"
    destination.mkdir()
    path = destination / "my battery.csv"
    results = []
    worker = BatteryModelDownloadWorker(scpi=instrument, slot=4, path=str(path), on_result=results.append)
    worker.run()
    assert results[0][0] == "saved"
    assert instrument.queries == [
        ":BATTery:MODel4:VOC:STEPs?", ":BATTery:MODel4:RESistance:STEPs?",
        ":BATTery:MODel4:VOC?", ":BATTery:MODel4:RESistance?",
    ]
    with path.open() as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 101
    assert rows[0] == {"soc_percent": "0", "open_circuit_voltage_V": "3.0", "resistance_ohm": "0.05"}
    assert rows[-1]["soc_percent"] == "100"
    assert Decimal(rows[-1]["open_circuit_voltage_V"]) == 4
    assert list(destination.iterdir()) == [path]


@pytest.mark.parametrize("bad_response", ["", "0", "100", "NaN", "9.9E37", "garbage"])
def test_invalid_slot_data_preserves_existing_file(tmp_path, bad_response):
    path = tmp_path / "existing.csv"
    path.write_text("existing model")
    results = []
    worker = BatteryModelDownloadWorker(scpi=Mock(query=Mock(return_value=bad_response)), slot=1,
                                       path=str(path), on_result=results.append)
    worker.run()
    assert results[0][0] == "error"
    assert path.read_text() == "existing model"
    assert list(tmp_path.iterdir()) == [path]


def test_truncated_curve_is_rejected():
    instrument = Mock(query=Mock(side_effect=["101", "101", "3,4", "0.1,0.2"]))
    with pytest.raises(ValueError, match="Incomplete"):
        read_model(instrument, 1, threading.Event())


def test_cancelled_download_does_not_query_or_create_file(tmp_path):
    instrument = ModelInstrument()
    results = []
    path = tmp_path / "cancelled.csv"
    worker = BatteryModelDownloadWorker(scpi=instrument, slot=2, path=str(path), on_result=results.append)
    worker.stop()
    worker.run()
    assert results[0][0] == "cancelled"
    assert instrument.queries == []
    assert not path.exists()


def test_timeout_preserves_existing_file(tmp_path):
    instrument = Mock(query=Mock(side_effect=TimeoutError("USB timeout")))
    results = []
    path = tmp_path / "existing.csv"
    path.write_text("keep")
    BatteryModelDownloadWorker(scpi=instrument, slot=2, path=str(path), on_result=results.append).run()
    assert results[0][0] == "error"
    assert "USB timeout" in results[0][1]
    assert path.read_text() == "keep"


def test_failed_atomic_save_cleans_temporary_file_and_preserves_destination(tmp_path):
    path = tmp_path / "existing.csv"
    path.write_text("keep")
    with patch("dmm_app.battery_models.os.replace", side_effect=PermissionError("denied")):
        with pytest.raises(PermissionError):
            save_model(path, [(0, Decimal(3), Decimal("0.1"))], threading.Event())
    assert path.read_text() == "keep"
    assert list(tmp_path.iterdir()) == [path]


def test_gui_download_uses_save_dialog_and_disables_conflicting_actions(tmp_path):
    app = QApplication.instance() or QApplication([])
    with (
        patch("dmm_app.gui.SerialTransport.list_serial_ports", return_value=[]),
        patch("dmm_app.gui.VisaTransport.list_resources", return_value=[]),
    ):
        window = DMMAppWindow()
    panel = window._panels[0]
    try:
        assert panel._battery_model_controls.isHidden()
        panel._instrument_combo.setCurrentText(InstrumentType.KEITHLEY_2281S.value)
        assert not panel._battery_model_controls.isHidden()
        assert not panel._battery_model_download.isEnabled()
        panel._transport = Mock(is_open=True)
        instrument = ModelInstrument()
        panel._scpi = instrument
        panel._refresh_controls()
        assert panel._battery_model_download.isEnabled()
        with patch("dmm_app.gui.QFileDialog.getSaveFileName", return_value=("", "")):
            panel.download_battery_model()
        assert instrument.queries == []
        release = threading.Event()
        entered = threading.Event()
        def slow_query(command):
            entered.set()
            assert release.wait(3)
            return instrument.query(command)
        panel._scpi = Mock(query=slow_query)
        path = tmp_path / "chosen.csv"
        with patch("dmm_app.gui.QFileDialog.getSaveFileName", return_value=(str(path), "")):
            panel.download_battery_model()
        assert entered.wait(1)
        assert not panel._battery_model_download.isEnabled()
        assert not panel._start_button.isEnabled()
        assert not panel._snapshot_button.isEnabled()
        assert panel._stop_button.isEnabled()
        release.set()
        panel._worker.join(3)
        window._process_events()
        assert path.exists()
        assert panel._battery_model_download.isEnabled()
        assert "Saved model 1" in panel._output.toPlainText()
        app.processEvents()
    finally:
        window.close()


def test_cancel_during_query_stops_before_saving(tmp_path):
    instrument = ModelInstrument()
    path = tmp_path / "existing.csv"
    path.write_text("keep")
    results = []
    worker = BatteryModelDownloadWorker(scpi=instrument, slot=2, path=str(path), on_result=results.append)
    original_query = instrument.query
    def cancel_after_response(command):
        response = original_query(command)
        worker.stop()
        return response
    instrument.query = cancel_after_response
    worker.run()
    assert results[0][0] == "cancelled"
    assert len(instrument.queries) == 1
    assert path.read_text() == "keep"


@pytest.mark.parametrize("slot", [0, 10, True, "1;*RST"])
def test_invalid_slot_never_reaches_instrument(slot):
    instrument = ModelInstrument()
    with pytest.raises(ValueError, match="slot"):
        read_model(instrument, slot, threading.Event())
    assert not instrument.queries
