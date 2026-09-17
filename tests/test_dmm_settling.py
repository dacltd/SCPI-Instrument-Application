import threading
import time
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from dmm_app.clock import AcquisitionClock
from dmm_app.gui import DMMAppWindow
from dmm_app.models import InstrumentType, MeasurementFunction
from dmm_app.poller import PollingWorker, PollRequest


def worker(scpi, readings, **kwargs):
    return PollingWorker(scpi, AcquisitionClock(), 0, 'USB', InstrumentType.MP730889,
                         'MULTICOMP,MP730889',
                         [PollRequest(0, MeasurementFunction.VOLTAGE, 'MEAS1?', 'V')],
                         .2, readings.append, lambda error: pytest.fail(error), **kwargs)


def test_settling_prevents_recording_reset_zero():
    configured = time.monotonic()
    scpi = Mock(query=Mock(side_effect=lambda _: '0' if time.monotonic()-configured < .04 else '3.74'))
    readings, complete = [], []
    capture = worker(scpi, readings, initial_delay_seconds=.08,
                     single_shot=True, on_complete=complete.append)
    capture.start()
    assert not capture.ready.wait(.02)
    scpi.query.assert_not_called()
    capture.join(1)
    assert [r.value for r in readings] == [3.74]
    assert capture.ready.is_set()
    assert complete == ['Snapshot complete.']


def test_real_zero_after_settling_is_kept():
    readings = []
    capture = worker(Mock(query=Mock(return_value='0')), readings,
                     initial_delay_seconds=.01, single_shot=True)
    capture.start()
    capture.join(1)
    assert [r.value for r in readings] == [0.0]
    assert readings[0].elapsed_seconds >= .01


def test_cancel_during_settling_does_not_query_or_record():
    scpi, readings, complete = Mock(), [], []
    capture = worker(scpi, readings, initial_delay_seconds=5,
                     single_shot=True, on_complete=complete.append)
    capture.start()
    capture.stop()
    capture.join(.5)
    assert not capture.is_alive()
    scpi.query.assert_not_called()
    assert readings == complete == []
    assert not capture.ready.is_set()


def test_stop_while_waiting_for_start_gate():
    capture = worker(Mock(), [], start_gate=threading.Event())
    capture.start()
    capture.stop()
    capture.join(.5)
    assert not capture.is_alive()


@pytest.fixture
def window():
    app = QApplication.instance() or QApplication([])
    w = DMMAppWindow(discover_endpoints=False)
    yield w
    w.close()
    app.processEvents()


def test_snapshot_is_asynchronous_and_disables_overlapping_operations(window):
    panel = window.add_instrument(InstrumentType.MP730889)
    panel._transport = Mock(is_open=True)
    panel._scpi = Mock(query=Mock(return_value='3.74'))
    panel._settling_input.setValue(.5)
    start = time.monotonic()
    panel.take_snapshot()
    assert time.monotonic()-start < .4
    assert panel.is_running
    assert not panel._snapshot_button.isEnabled()
    assert not panel._settling_input.isEnabled()
    panel.take_snapshot()  # same call is also guarded, not just the button
    assert panel._scpi.write.call_count == 2
    panel._worker.join(2)
    window._process_events()
    assert panel._measurement_rows[0].latest_label.text() == '3.74 V'
    assert panel._snapshot_button.isEnabled()
    assert panel._scpi.query.call_count == 1


def test_continuous_acquisition_uses_same_delay(window):
    panel = window.add_instrument(InstrumentType.MP730889)
    panel._transport = Mock(is_open=True)
    panel._scpi = Mock(query=Mock(return_value='3.74'))
    panel._device_idn = 'MULTICOMP,MP730889'
    panel._settling_input.setValue(.5)
    assert panel.start_acquisition(announce=False)
    assert not panel._worker.ready.wait(.05)
    panel._scpi.query.assert_not_called()
    assert panel._worker.ready.wait(2)
    panel.stop_acquisition(announce=False)
    window._process_events()
    assert panel._measurement_rows[0].latest_label.text() == '3.74 V'


def test_setting_persists_and_legacy_default_is_five_seconds(window):
    panel = window.add_instrument(InstrumentType.MP730889)
    panel._settling_input.setValue(8)
    document = window._configuration_document()
    window._apply_configuration_document(document)
    assert panel._settling_seconds() == 8
    document['panels'][0]['acquisition'].pop('settling_seconds')
    window._apply_configuration_document(document)
    assert panel._settling_seconds() == 5
    panel._instrument_combo.setCurrentText(InstrumentType.OWON_SPE6103.value)
    assert panel._settling_seconds() == 0


@pytest.mark.parametrize('invalid', [0, -1, True, '5', float('nan'), 61])
def test_invalid_config_settling_rejected(window, invalid):
    document = window._configuration_document()
    document['panels'][0]['acquisition']['settling_seconds'] = invalid
    with pytest.raises(ValueError, match='settling_seconds'):
        window._apply_configuration_document(document)


def test_repeat_snapshot_and_start_reuse_setup_without_another_delay(window):
    panel = window.add_instrument(InstrumentType.MP730889)
    panel._transport = Mock(is_open=True)
    panel._scpi = Mock(query=Mock(return_value='3.74'))
    panel._device_idn = 'MULTICOMP,MP730889'
    panel._settling_input.setValue(.5)
    panel.take_snapshot()
    panel._worker.join(2)
    window._process_events()
    assert panel._scpi.write.call_count == 2
    panel.take_snapshot()
    panel._worker.join(.3)
    assert not panel._worker.is_alive()
    window._process_events()
    assert panel._scpi.query.call_count == 2
    assert panel._scpi.write.call_count == 2
    assert panel.start_acquisition(announce=False)
    assert panel._worker.ready.wait(.3)
    panel.stop_acquisition(announce=False)
    assert panel._scpi.write.call_count == 2


def test_configuration_cache_invalidates_on_mode_change_error_and_new_connection(window):
    panel = window.add_instrument(InstrumentType.MP730889)
    panel._scpi = Mock()
    _, commands = panel._build_poll_requests()
    assert panel._prepare_scpi_setup(commands) > 4
    panel._prepare_scpi_setup(commands)
    assert panel._scpi.write.call_count == 2
    panel._measurement_rows[0].function_combo.setCurrentText(MeasurementFunction.CURRENT.value)
    _, commands = panel._build_poll_requests()
    panel._prepare_scpi_setup(commands)
    assert panel._scpi.write.call_args.args[0] == 'CONFigure:CURRent:DC'
    assert panel._scpi.write.call_count == 4
    panel.handle_worker_error('read timeout')
    panel._prepare_scpi_setup(commands)
    assert panel._scpi.write.call_count == 6
    panel._scpi = Mock()
    panel._prepare_scpi_setup(commands)
    assert panel._scpi.write.call_count == 2
    panel._close_transport()
    assert panel._dmm_setup_client is None


def test_cancelled_settling_preserves_remaining_wait_and_reapply_is_explicit(window):
    from unittest.mock import patch
    panel = window.add_instrument(InstrumentType.MP730889)
    panel._scpi = Mock()
    _, commands = panel._build_poll_requests()
    with patch('dmm_app.gui.time.monotonic', return_value=100):
        assert panel._prepare_scpi_setup(commands) == 5
    with patch('dmm_app.gui.time.monotonic', return_value=102):
        assert panel._prepare_scpi_setup(commands) == 3
        assert panel._scpi.write.call_count == 2
        panel._reapply_dmm_setup()
        assert panel._prepare_scpi_setup(commands) == 5
        assert panel._scpi.write.call_count == 4


def test_failed_setup_is_not_cached(window):
    panel = window.add_instrument(InstrumentType.MP730889)
    panel._scpi = Mock(write=Mock(side_effect=[None, OSError('serial lost')]))
    _, commands = panel._build_poll_requests()
    with pytest.raises(OSError):
        panel._prepare_scpi_setup(commands)
    assert panel._dmm_setup_client is None
    assert panel._dmm_configured_at is None


def test_disconnect_retains_connection_while_query_is_stopping(window):
    panel = window.add_instrument(InstrumentType.MP730889)
    panel._transport = Mock(is_open=True)
    panel._worker = Mock(spec=PollingWorker)
    panel._worker.is_alive.return_value = True
    panel.disconnect_device(announce=False)
    panel._transport.close.assert_not_called()
    assert panel._worker is not None
    panel._worker = None
