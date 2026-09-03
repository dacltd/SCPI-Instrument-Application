"""PicoSDK adapter and acquisition worker for the USB TC-08."""

from __future__ import annotations

import ctypes
import ntpath
import os
import threading
import time
from collections.abc import Callable
from ctypes.util import find_library
from dataclasses import dataclass

from dmm_app.clock import AcquisitionClock
from dmm_app.models import InstrumentType, MeasurementFunction, Reading

TC08_CHANNELS = tuple(range(1, 9))
TC08_THERMOCOUPLE_TYPES = ("B", "E", "J", "K", "N", "R", "S", "T")
TC08_TEMPERATURE_UNITS = {
    "C": (0, "°C"),
    "F": (1, "°F"),
    "K": (2, "K"),
    "R": (3, "°R"),
}

TC08_ERROR_NAMES = {
    0: "OK",
    1: "operating system not supported",
    2: "no channels configured",
    3: "invalid parameter",
    4: "hardware variant not supported",
    5: "incorrect acquisition mode",
    6: "device enumeration incomplete",
    7: "device not responding",
    8: "firmware download failed",
    9: "device configuration failed",
    10: "device not found",
    11: "driver thread failed",
    12: "USB pipe information failed",
    13: "device is not calibrated",
    14: "Pico USB support library is too old",
    15: "communication with the device was lost",
}


class PicoTC08Error(RuntimeError):
    """A PicoSDK driver or TC-08 operation failed."""


class PicoTC08Library:
    """Small ctypes wrapper around the installed 64-bit ``usbtc08`` driver."""

    def __init__(self, dll=None):
        self._dll_directory_handles: list[object] = []
        self._dll = dll or self._load_driver()
        self._bind_symbols()

    @staticmethod
    def _windows_driver_candidates(
        environment: dict[str, str], discovered: str | None
    ) -> tuple[list[str], str | None]:
        candidates: list[str] = []

        sdk_override = environment.get("PICO_SDK_PATH")
        if sdk_override:
            candidates.extend(
                [
                    ntpath.join(sdk_override, "lib", "usbtc08.dll"),
                    ntpath.join(sdk_override, "usbtc08.dll"),
                ]
            )

        for variable in ("ProgramW6432", "ProgramFiles"):
            program_files = environment.get(variable)
            if program_files:
                candidates.append(
                    ntpath.join(
                        program_files,
                        "Pico Technology",
                        "SDK",
                        "lib",
                        "usbtc08.dll",
                    )
                )

        program_files_x86 = environment.get("ProgramFiles(x86)")
        x86_candidate = (
            ntpath.join(
                program_files_x86,
                "Pico Technology",
                "SDK",
                "lib",
                "usbtc08.dll",
            )
            if program_files_x86
            else None
        )
        if discovered and not (
            x86_candidate
            and ntpath.normcase(discovered) == ntpath.normcase(x86_candidate)
        ):
            candidates.append(discovered)
        candidates.append("usbtc08.dll")
        return list(dict.fromkeys(candidates)), x86_candidate

    def _load_driver(self):
        if os.name == "nt":
            candidates, x86_candidate = self._windows_driver_candidates(
                dict(os.environ), find_library("usbtc08")
            )
        else:
            discovered = find_library("usbtc08")
            candidates = [candidate for candidate in (discovered, "usbtc08") if candidate]
            x86_candidate = None

        errors: list[str] = []
        for candidate in dict.fromkeys(candidates):
            is_absolute_windows_path = os.name == "nt" and ntpath.isabs(candidate)
            if is_absolute_windows_path and not os.path.isfile(candidate):
                continue
            directory_handle = None
            try:
                if os.name == "nt":
                    if is_absolute_windows_path and hasattr(os, "add_dll_directory"):
                        directory_handle = os.add_dll_directory(ntpath.dirname(candidate))
                    loaded = ctypes.WinDLL(candidate)
                    if directory_handle is not None:
                        self._dll_directory_handles.append(directory_handle)
                    return loaded
                return ctypes.CDLL(candidate)
            except OSError as exc:
                errors.append(f"{candidate}: {exc}")
                if directory_handle is not None:
                    directory_handle.close()

        message = (
            "The 64-bit PicoSDK USB TC-08 driver could not be found or loaded. "
            "Install the 64-bit PicoSDK with USB TC-08 support selected, then restart "
            "the application. The expected driver location is "
            r"C:\Program Files\Pico Technology\SDK\lib\usbtc08.dll."
        )
        if x86_candidate and os.path.isfile(x86_candidate):
            message += (
                f" A 32-bit SDK installation was detected at {x86_candidate}; this "
                "64-bit application cannot load that DLL."
            )
        if errors:
            message += f" Load attempts: {'; '.join(errors)}"
        raise PicoTC08Error(message)

    def _bind(self, name: str, restype, argtypes: list[object]):
        function = getattr(self._dll, name)
        function.restype = restype
        function.argtypes = argtypes
        return function

    def _bind_symbols(self) -> None:
        self._open_unit = self._bind("usb_tc08_open_unit", ctypes.c_int16, [])
        self._close_unit = self._bind(
            "usb_tc08_close_unit", ctypes.c_int16, [ctypes.c_int16]
        )
        self._set_mains = self._bind(
            "usb_tc08_set_mains",
            ctypes.c_int16,
            [ctypes.c_int16, ctypes.c_int16],
        )
        self._set_channel = self._bind(
            "usb_tc08_set_channel",
            ctypes.c_int16,
            [ctypes.c_int16, ctypes.c_int16, ctypes.c_int8],
        )
        self._minimum_interval = self._bind(
            "usb_tc08_get_minimum_interval_ms", ctypes.c_int32, [ctypes.c_int16]
        )
        self._get_single = self._bind(
            "usb_tc08_get_single",
            ctypes.c_int16,
            [
                ctypes.c_int16,
                ctypes.POINTER(ctypes.c_float),
                ctypes.POINTER(ctypes.c_int16),
                ctypes.c_int16,
            ],
        )
        self._get_last_error = self._bind(
            "usb_tc08_get_last_error", ctypes.c_int16, [ctypes.c_int16]
        )
        self._get_formatted_info = self._bind(
            "usb_tc08_get_formatted_info",
            ctypes.c_int16,
            [ctypes.c_int16, ctypes.POINTER(ctypes.c_char), ctypes.c_int16],
        )

    def open_unit(self) -> int:
        return int(self._open_unit())

    def close_unit(self, handle: int) -> int:
        return int(self._close_unit(handle))

    def set_mains(self, handle: int, mains_hz: int) -> int:
        return int(self._set_mains(handle, 1 if mains_hz == 60 else 0))

    def set_channel(self, handle: int, channel: int, thermocouple_type: str) -> int:
        code = ord(thermocouple_type) if thermocouple_type else ord(" ")
        return int(self._set_channel(handle, channel, code))

    def minimum_interval_ms(self, handle: int) -> int:
        return int(self._minimum_interval(handle))

    def get_single(self, handle: int, units_code: int) -> tuple[int, tuple[float, ...], int]:
        temperatures = (ctypes.c_float * 9)()
        overflow = ctypes.c_int16()
        status = int(self._get_single(handle, temperatures, ctypes.byref(overflow), units_code))
        return status, tuple(float(value) for value in temperatures), int(overflow.value)

    def last_error(self, handle: int) -> int:
        return int(self._get_last_error(handle))

    def formatted_info(self, handle: int) -> str:
        buffer = ctypes.create_string_buffer(512)
        result = int(self._get_formatted_info(handle, buffer, len(buffer)))
        return buffer.value.decode("utf-8", errors="replace").strip() if result > 0 else ""


@dataclass(frozen=True)
class PicoTC08Settings:
    mains_hz: int
    units: str
    channel_types: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.mains_hz not in {50, 60}:
            raise ValueError("TC-08 mains rejection must be 50 or 60 Hz.")
        if self.units not in TC08_TEMPERATURE_UNITS:
            raise ValueError("TC-08 temperature units are not supported.")
        if len(self.channel_types) != 8:
            raise ValueError("TC-08 configuration must contain eight channel types.")
        invalid = [
            channel_type
            for channel_type in self.channel_types
            if channel_type and channel_type not in TC08_THERMOCOUPLE_TYPES
        ]
        if invalid:
            raise ValueError(f"Unsupported TC-08 thermocouple type: {invalid[0]}")
        if not any(self.channel_types):
            raise ValueError("Enable at least one TC-08 thermocouple channel.")

    @property
    def units_code(self) -> int:
        return TC08_TEMPERATURE_UNITS[self.units][0]

    @property
    def unit_label(self) -> str:
        return TC08_TEMPERATURE_UNITS[self.units][1]


class PicoTC08Device:
    """Open, configure, and read one USB TC-08 through PicoSDK."""

    def __init__(self, library: PicoTC08Library | None = None):
        self._library = library
        self._handle: int | None = None
        self._settings: PicoTC08Settings | None = None
        self._device_idn = "PicoLog USB TC-08"
        self._minimum_interval_ms = 0
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    @property
    def device_idn(self) -> str:
        return self._device_idn

    @property
    def minimum_interval_ms(self) -> int:
        return self._minimum_interval_ms

    def _driver(self) -> PicoTC08Library:
        if self._library is None:
            self._library = PicoTC08Library()
        return self._library

    def _error(self, operation: str, handle: int | None = None) -> PicoTC08Error:
        driver = self._driver()
        error_handle = (self._handle or 0) if handle is None else handle
        try:
            code = driver.last_error(error_handle)
        except Exception:
            code = -1
        description = TC08_ERROR_NAMES.get(code, f"unknown error {code}")
        return PicoTC08Error(f"TC-08 {operation} failed: {description}.")

    def open(self) -> None:
        with self._lock:
            if self._handle is not None:
                return
            driver = self._driver()
            handle = driver.open_unit()
            if handle <= 0:
                raise self._error("open")
            self._handle = handle
            info = driver.formatted_info(handle)
            if info:
                self._device_idn = " | ".join(info.splitlines())

    def configure(self, settings: PicoTC08Settings) -> None:
        with self._lock:
            if self._handle is None:
                raise PicoTC08Error("TC-08 is not open.")
            driver = self._driver()
            if driver.set_mains(self._handle, settings.mains_hz) <= 0:
                raise self._error("mains-filter setup")
            for channel, channel_type in zip(
                TC08_CHANNELS, settings.channel_types, strict=True
            ):
                if driver.set_channel(self._handle, channel, channel_type) <= 0:
                    raise self._error(f"channel {channel} setup")
            minimum_interval = driver.minimum_interval_ms(self._handle)
            if minimum_interval <= 0:
                raise self._error("minimum-interval query")
            self._settings = settings
            self._minimum_interval_ms = minimum_interval

    def read_temperatures(self) -> tuple[tuple[float, ...], int]:
        with self._lock:
            if self._handle is None or self._settings is None:
                raise PicoTC08Error("TC-08 is not open and configured.")
            status, temperatures, overflow = self._driver().get_single(
                self._handle, self._settings.units_code
            )
            if status <= 0:
                raise self._error("temperature read")
            return temperatures, overflow

    def close(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            handle = self._handle
            status = self._driver().close_unit(handle)
            error = self._error("close", handle) if status <= 0 else None
            self._handle = None
            self._settings = None
            self._minimum_interval_ms = 0
            if error is not None:
                raise error


@dataclass(frozen=True)
class PicoTC08Measurement:
    slot_index: int
    channel: int
    source: str


class PicoTC08Worker(threading.Thread):
    """Poll complete TC-08 conversions and emit selected channels."""

    def __init__(
        self,
        *,
        device: PicoTC08Device,
        clock: AcquisitionClock,
        instrument_index: int,
        connection: str,
        device_idn: str,
        measurements: list[PicoTC08Measurement],
        unit: str,
        interval_seconds: float,
        on_reading: Callable[[Reading], None],
        on_error: Callable[[str], None],
        start_gate: threading.Event | None = None,
    ):
        super().__init__(daemon=True)
        self._device = device
        self._clock = clock
        self._instrument_index = instrument_index
        self._connection = connection
        self._device_idn = device_idn
        self._measurements = measurements
        self._unit = unit
        self._interval_seconds = interval_seconds
        self._on_reading = on_reading
        self._on_error = on_error
        self._start_gate = start_gate
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if self._start_gate is not None:
            self._start_gate.wait()
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                temperatures, overflow_flags = self._device.read_temperatures()
                for measurement in self._measurements:
                    if self._stop_event.is_set():
                        break
                    value = temperatures[measurement.channel]
                    over_range = bool(overflow_flags & (1 << measurement.channel))
                    raw = "OVER-RANGE" if over_range else f"{value:.7g}"
                    timestamp, elapsed_seconds, acquisition_run = self._clock.capture()
                    self._on_reading(
                        Reading(
                            timestamp=timestamp,
                            elapsed_seconds=elapsed_seconds,
                            acquisition_run=acquisition_run,
                            instrument_index=self._instrument_index,
                            slot_index=measurement.slot_index,
                            instrument=InstrumentType.PICOLOG_TC08,
                            connection=self._connection,
                            device_idn=self._device_idn,
                            function=MeasurementFunction.TEMPERATURE,
                            source=measurement.source,
                            raw_response=raw,
                            value=None if over_range else value,
                            unit=self._unit,
                        )
                    )
            except Exception as exc:  # pragma: no cover - hardware error path
                if not self._stop_event.is_set():
                    self._on_error(str(exc))
                return

            remaining = self._interval_seconds - (time.monotonic() - started)
            if remaining > 0:
                self._stop_event.wait(remaining)
