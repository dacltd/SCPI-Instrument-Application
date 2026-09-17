"""Tests for the PicoLog USB TC-08 adapter and worker."""

import threading
from unittest.mock import Mock, patch

import pytest

import dmm_app.pico_tc08 as pico_tc08
from dmm_app.clock import AcquisitionClock
from dmm_app.models import MeasurementFunction
from dmm_app.pico_tc08 import (
    PicoTC08Device,
    PicoTC08Error,
    PicoTC08Library,
    PicoTC08Measurement,
    PicoTC08Settings,
    PicoTC08Worker,
)


class FakeTC08Library:
    def __init__(self, *, open_handle: int = 7):
        self.open_handle = open_handle
        self.closed_handles: list[int] = []
        self.mains_calls: list[tuple[int, int]] = []
        self.channel_calls: list[tuple[int, int, str]] = []
        self.last_error_code = 0

    def open_unit(self) -> int:
        return self.open_handle

    def close_unit(self, handle: int) -> int:
        self.closed_handles.append(handle)
        return 1

    def set_mains(self, handle: int, mains_hz: int) -> int:
        self.mains_calls.append((handle, mains_hz))
        return 1

    def set_channel(self, handle: int, channel: int, channel_type: str) -> int:
        self.channel_calls.append((handle, channel, channel_type))
        return 1

    def minimum_interval_ms(self, _handle: int) -> int:
        return 800

    def get_single(self, _handle: int, units_code: int):
        assert units_code == 0
        return 1, tuple(float(value) for value in range(9)), 1 << 2

    def last_error(self, _handle: int) -> int:
        return self.last_error_code

    def formatted_info(self, _handle: int) -> str:
        return "PicoLog USB TC-08\nSerial: TC08-TEST"


def test_windows_driver_candidates_prefer_64_bit_sdk_and_ignore_x86_discovery() -> None:
    environment = {
        "PROGRAMW6432": r"C:\Program Files",
        "PROGRAMFILES": r"C:\Program Files",
        "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
    }
    x86_driver = r"C:\Program Files (x86)\Pico Technology\SDK\lib\usbtc08.dll"

    candidates, detected_x86 = PicoTC08Library._windows_driver_candidates(
        environment, x86_driver
    )

    assert candidates[0] == r"C:\Program Files\Pico Technology\SDK\lib\usbtc08.dll"
    assert x86_driver not in candidates
    assert detected_x86 == x86_driver


def test_windows_loader_uses_explicit_64_bit_sdk_directory() -> None:
    environment = {
        "ProgramW6432": r"C:\Program Files",
        "ProgramFiles": r"C:\Program Files",
        "ProgramFiles(x86)": r"C:\Program Files (x86)",
    }
    x64_driver = r"C:\Program Files\Pico Technology\SDK\lib\usbtc08.dll"
    x86_driver = r"C:\Program Files (x86)\Pico Technology\SDK\lib\usbtc08.dll"
    loaded_dll = object()
    directory_handle = Mock()
    library = PicoTC08Library.__new__(PicoTC08Library)
    library._dll_directory_handles = []

    with (
        patch.object(pico_tc08.os, "name", "nt"),
        patch.dict(pico_tc08.os.environ, environment, clear=True),
        patch.object(pico_tc08, "find_library", return_value=x86_driver),
        patch.object(pico_tc08.os.path, "isfile", side_effect=lambda path: path == x64_driver),
        patch.object(
            pico_tc08.os,
            "add_dll_directory",
            return_value=directory_handle,
            create=True,
        ) as add_directory,
        patch.object(pico_tc08.ctypes, "WinDLL", return_value=loaded_dll, create=True) as load,
    ):
        result = library._load_driver()

    assert result is loaded_dll
    add_directory.assert_called_once_with(r"C:\Program Files\Pico Technology\SDK\lib")
    load.assert_called_once_with(x64_driver)
    assert library._dll_directory_handles == [directory_handle]


def test_windows_loader_reports_detected_32_bit_sdk() -> None:
    environment = {
        "ProgramW6432": r"C:\Program Files",
        "ProgramFiles": r"C:\Program Files",
        "ProgramFiles(x86)": r"C:\Program Files (x86)",
    }
    x86_driver = r"C:\Program Files (x86)\Pico Technology\SDK\lib\usbtc08.dll"
    library = PicoTC08Library.__new__(PicoTC08Library)
    library._dll_directory_handles = []

    with (
        patch.object(pico_tc08.os, "name", "nt"),
        patch.dict(pico_tc08.os.environ, environment, clear=True),
        patch.object(pico_tc08, "find_library", return_value=x86_driver),
        patch.object(pico_tc08.os.path, "isfile", side_effect=lambda path: path == x86_driver),
        patch.object(
            pico_tc08.ctypes,
            "WinDLL",
            side_effect=OSError("module not found"),
            create=True,
        ),
    ):
        with pytest.raises(PicoTC08Error, match="32-bit SDK installation was detected"):
            library._load_driver()


def test_device_configures_all_channels_and_reads_temperatures() -> None:
    library = FakeTC08Library()
    device = PicoTC08Device(library=library)
    settings = PicoTC08Settings(
        mains_hz=50,
        units="C",
        channel_types=("K", "J", "", "", "", "", "", ""),
    )

    device.open()
    device.configure(settings)
    temperatures, overflow = device.read_temperatures()

    assert device.is_open
    assert device.minimum_interval_ms == 800
    assert device.device_idn == "PicoLog USB TC-08 | Serial: TC08-TEST"
    assert library.mains_calls == [(7, 50)]
    assert library.channel_calls == [
        (7, channel, channel_type)
        for channel, channel_type in enumerate(settings.channel_types, start=1)
    ]
    assert temperatures[1] == 1.0
    assert overflow == 1 << 2

    device.close()
    assert not device.is_open
    assert library.closed_handles == [7]


def test_device_reports_driver_error_when_no_unit_is_found() -> None:
    library = FakeTC08Library(open_handle=0)
    library.last_error_code = 10
    device = PicoTC08Device(library=library)

    with pytest.raises(PicoTC08Error, match="device not found"):
        device.open()


def test_settings_require_at_least_one_valid_thermocouple_channel() -> None:
    with pytest.raises(ValueError, match="at least one"):
        PicoTC08Settings(mains_hz=50, units="C", channel_types=("",) * 8)
    with pytest.raises(ValueError, match="Unsupported"):
        PicoTC08Settings(
            mains_hz=50,
            units="C",
            channel_types=("Q", "", "", "", "", "", "", ""),
        )


def test_worker_emits_selected_channels_and_marks_over_range() -> None:
    device = PicoTC08Device(library=FakeTC08Library())
    device.open()
    settings = PicoTC08Settings(
        mains_hz=50,
        units="C",
        channel_types=("K", "J", "", "", "", "", "", ""),
    )
    device.configure(settings)
    readings = []
    completed = threading.Event()

    worker: PicoTC08Worker

    def collect(reading) -> None:
        readings.append(reading)
        if len(readings) == 2:
            worker.stop()
            completed.set()

    worker = PicoTC08Worker(
        device=device,
        clock=AcquisitionClock(),
        instrument_index=2,
        connection="PicoSDK USB",
        device_idn=device.device_idn,
        measurements=[
            PicoTC08Measurement(slot_index=0, channel=0, source="Cold junction"),
            PicoTC08Measurement(slot_index=1, channel=2, source="Channel 2"),
        ],
        unit=settings.unit_label,
        interval_seconds=0.2,
        on_reading=collect,
        on_error=lambda error: pytest.fail(error),
    )
    worker.start()

    assert completed.wait(2.0)
    worker.join(timeout=1.0)
    assert not worker.is_alive()
    assert [reading.function for reading in readings] == [
        MeasurementFunction.TEMPERATURE,
        MeasurementFunction.TEMPERATURE,
    ]
    assert readings[0].value == 0.0
    assert readings[0].source == "Cold junction"
    assert readings[1].value is None
    assert readings[1].raw_response == "OVER-RANGE"
    assert readings[1].source == "Channel 2"


def test_macos_driver_candidates_find_sdk_without_shell_library_path() -> None:
    candidates = PicoTC08Library._macos_driver_candidates({}, None)
    assert candidates[0] == (
        "/Library/Frameworks/PicoSDK.framework/Libraries/libusbtc08/libusbtc08.dylib"
    )
    assert "/Applications/PicoLog.app/Contents/Resources/libusbtc08.dylib" in candidates
    assert candidates.index("/Applications/PicoLog.app/Contents/Resources/libusbtc08.dylib") > 0


def test_macos_driver_override_precedes_standard_sdk() -> None:
    candidates = PicoTC08Library._macos_driver_candidates(
        {"PICO_SDK_PATH": "/custom/PicoSDK.framework"}, "/custom/libusbtc08.dylib"
    )
    assert candidates[0] == "/custom/PicoSDK.framework/Libraries/libusbtc08/libusbtc08.dylib"
    assert "/custom/libusbtc08.dylib" in candidates


def test_macos_loader_loads_sdk_and_falls_back_after_incompatible_driver() -> None:
    library = PicoTC08Library.__new__(PicoTC08Library)
    library._dll_directory_handles = []
    loaded = object()
    with (
        patch.object(pico_tc08.os, "name", "posix"),
        patch.object(pico_tc08.sys, "platform", "darwin"),
        patch.object(pico_tc08, "find_library", return_value=None),
        patch.object(PicoTC08Library, "_macos_driver_candidates",
                     return_value=["/old/libusbtc08.dylib", "/new/libusbtc08.dylib"]),
        patch.object(pico_tc08.os.path, "isfile", return_value=True),
        patch.object(pico_tc08.ctypes, "CDLL",
                     side_effect=[OSError("incompatible architecture"), loaded]) as load,
    ):
        assert library._load_driver() is loaded
    assert [call.args[0] for call in load.call_args_list] == [
        "/old/libusbtc08.dylib", "/new/libusbtc08.dylib"
    ]


@pytest.mark.parametrize("architecture, sdk", [("arm64", "ARM64 (Apple Silicon)"),
                                                ("x86_64", "x64 (Intel)")])
def test_macos_loader_reports_matching_sdk_without_windows_instructions(architecture, sdk) -> None:
    library = PicoTC08Library.__new__(PicoTC08Library)
    library._dll_directory_handles = []
    with (
        patch.object(pico_tc08.os, "name", "posix"),
        patch.object(pico_tc08.sys, "platform", "darwin"),
        patch.object(pico_tc08.platform, "machine", return_value=architecture),
        patch.object(pico_tc08, "find_library", return_value=None),
        patch.object(pico_tc08.os.path, "isfile", return_value=False),
        patch.object(pico_tc08.ctypes, "CDLL", side_effect=OSError("incompatible architecture")),
        pytest.raises(PicoTC08Error) as error,
    ):
        library._load_driver()
    assert sdk in str(error.value)
    assert "incompatible architecture" in str(error.value)
    assert "Program Files" not in str(error.value)
