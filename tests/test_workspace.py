import copy
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
from PySide6.QtWidgets import QApplication

from dmm_app.gui import DMMAppWindow
from dmm_app.models import InstrumentType, MeasurementFunction, Reading
from dmm_app.poller import parse_primary_value


@pytest.fixture
def window():
    app = QApplication.instance() or QApplication([])
    window = DMMAppWindow(discover_endpoints=False)
    yield window
    window.close()
    app.processEvents()


def test_add_and_remove_duplicate_instruments_without_moving_sessions(window):
    assert window._tabs.count() == 1
    first = window.add_instrument(InstrumentType.KEITHLEY_2281S)
    second = window.add_instrument(InstrumentType.KEITHLEY_2281S)
    assert first is not second
    assert window._tabs.count() == 3
    assert first._detail_tabs.tabText(0) == "Live data"
    second._transport = Mock(is_open=True)
    worker = Mock(is_alive=Mock(return_value=True))
    second._worker = worker
    window._tabs.setCurrentWidget(window._overview)
    window._tabs.setCurrentWidget(first)
    worker.stop.assert_not_called()
    second._transport.close.assert_not_called()
    window._remove_instrument_tab(window._tabs.indexOf(first))
    assert window._tabs.count() == 2
    assert second.instrument_index == 1
    assert second._worker is worker
    second._worker = None


def test_connected_tab_cannot_be_removed(window):
    panel = window.add_instrument(InstrumentType.KEITHLEY_2281S)
    panel._transport = Mock(is_open=True)
    with patch("dmm_app.gui.QMessageBox.information") as notice:
        window._remove_instrument_tab(window._tabs.indexOf(panel))
    notice.assert_called_once()
    assert window._tabs.indexOf(panel) > 0
    panel._transport.close.assert_not_called()


def test_dynamic_workspace_and_legacy_configuration_round_trip(window):
    for _ in range(6):
        window.add_instrument(InstrumentType.KEITHLEY_2281S)
    document = window._configuration_document()
    assert document['version'] == 2
    assert len(document['panels']) == 6
    window._apply_configuration_document(document)
    assert len(window._panels) == 6
    assert window._tabs.count() == 7
    legacy = copy.deepcopy(document)
    legacy['version'] = 1
    legacy['panels'] = legacy['panels'][:4]
    for panel in legacy['panels']:
        panel['measurements'] = panel['measurements'][:2]
        panel.pop('overview', None)
    window._apply_configuration_document(legacy)
    assert len(window._panels) == 4
    assert window._tabs.count() == 5
    assert len(window._panels[0]._measurement_rows) == 2
    assert not any(p.is_connected or p.is_running for p in window._panels)


def test_overview_shares_readings_and_csv_without_extra_device_queries(window, tmp_path):
    panel = window.add_instrument(InstrumentType.KEITHLEY_2281S)
    instrument = Mock()
    panel._scpi = instrument
    destination = tmp_path / 'capture.csv'
    with patch('dmm_app.gui.QFileDialog.getSaveFileName', return_value=(str(destination), '')):
        panel._logging_checkbox.setChecked(True)
    reading = Reading(datetime.now(timezone.utc), 12.5, 1, 0, 0,
                      InstrumentType.KEITHLEY_2281S, 'USB::TEST', '2281S',
                      MeasurementFunction.BATTERY_VOLTAGE, '', '3.45', 3.45, 'V')
    window._tabs.setCurrentWidget(window._overview)
    window._enqueue_event('reading', 0, reading)
    window._process_events()
    card = window._overview_cards[0]
    assert '3.45' in card.table.item(0, 1).text()
    assert '3.45' in panel._measurement_rows[0].latest_label.text()
    assert card.plot.traces[0].points[-1] == (12.5, 3.45)
    assert panel._plot.traces[0].points[-1] == (12.5, 3.45)
    instrument.query.assert_not_called()
    window._close_panel_loggers(clear_paths=False)
    assert destination.read_text().count('3.45') == 2  # raw + numeric in one row
    card.axes[0].setCurrentIndex(card.axes[0].findData(3))
    soc = replace(reading, slot_index=3, function=MeasurementFunction.BATTERY_SOC,
                  value=42.0, raw_response='42', unit='%')
    window._enqueue_event('reading', 0, soc)
    window._process_events()
    assert card.plot.traces[0].unit == '%'
    assert card.plot.traces[0].points[-1][1] == 42


def test_keithley_complete_status_uses_queries_only(window):
    panel = window.add_instrument(InstrumentType.KEITHLEY_2281S)
    requests, writes = panel._build_poll_requests()
    assert writes == []
    assert len(requests) == 6
    assert [r.query_command for r in requests] == [
        ':BATTery:SIMulator:TVOLtage?', ':BATTery:SIMulator:CURRent?',
        ':BATTery:SIMulator:VOC?', ':BATTery:SIMulator:SOC?',
        ':BATTery:SIMulator:CAPacity?', ':BATTery:SIMulator:RESistance?',
    ]
    assert requests[1].value_scale == -1
    assert parse_primary_value('42%') == 42
    assert parse_primary_value('1.2Ah') == 1.2
    assert parse_primary_value('0.05OHM') == .05


def test_reusing_slot_clears_previous_overview_values(window):
    panel = window.add_instrument(InstrumentType.KEITHLEY_2281S)
    old_card = window._overview_cards[0]
    window._remove_instrument_tab(window._tabs.indexOf(panel))
    replacement = window.add_instrument(InstrumentType.PICOLOG_TC08)
    assert replacement is panel
    assert window._overview_cards[0] is not old_card
    assert window._overview_cards[0].table.item(0, 1).text() == '—'


def test_overview_trace_choices_survive_configuration_reload(window):
    window.add_instrument(InstrumentType.KEITHLEY_2281S)
    card = window._overview_cards[0]
    card.axes[0].setCurrentIndex(card.axes[0].findData(3))
    card.axes[1].setCurrentIndex(card.axes[1].findData(5))
    window._overview_range.setCurrentIndex(1)
    document = window._configuration_document()
    window._apply_configuration_document(document)
    assert [combo.currentData() for combo in window._overview_cards[0].axes] == [3, 5]
    assert window._overview_range.currentData() == 300.0
