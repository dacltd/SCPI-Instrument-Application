from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Final

from dmm_app.models import SerialSettings, VisaSettings

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - import guard for environments without pyserial
    serial = None
    list_ports = None

try:
    import pyvisa
except ImportError:  # pragma: no cover - import guard for environments without PyVISA
    pyvisa = None


class Transport(ABC):
    @abstractmethod
    def open(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def write(self, payload: bytes) -> None:
        raise NotImplementedError

    @abstractmethod
    def read_until(self, terminator: bytes) -> bytes:
        raise NotImplementedError

    def read_binary_response(self) -> bytes:
        """Read one complete binary response; line transports use their terminator."""
        return self.read_until(b"\n")

    def recover_binary_response(self) -> None:
        """Discard an interrupted binary response when the transport supports it."""

    @property
    @abstractmethod
    def is_open(self) -> bool:
        raise NotImplementedError


class SerialTransport(Transport):
    def __init__(self, settings: SerialSettings):
        self._settings: Final[SerialSettings] = settings
        self._connection = None

    @staticmethod
    def list_serial_ports() -> list[str]:
        if list_ports is None:
            return []
        return [port.device for port in list_ports.comports()]

    def open(self) -> None:
        if serial is None:
            raise RuntimeError("pyserial is not installed. Install dependencies first.")
        if self._connection and self._connection.is_open:
            return
        self._connection = serial.Serial(
            port=self._settings.port,
            baudrate=self._settings.baudrate,
            bytesize=self._settings.bytesize,
            parity=self._settings.parity,
            stopbits=self._settings.stopbits,
            timeout=self._settings.timeout_seconds,
        )

    def close(self) -> None:
        if self._connection and self._connection.is_open:
            self._connection.close()

    def write(self, payload: bytes) -> None:
        if not self._connection or not self._connection.is_open:
            raise RuntimeError("Serial connection is not open.")
        self._connection.write(payload)

    def read_until(self, terminator: bytes) -> bytes:
        if not self._connection or not self._connection.is_open:
            raise RuntimeError("Serial connection is not open.")
        return self._connection.read_until(expected=terminator)

    @property
    def is_open(self) -> bool:
        return bool(self._connection and self._connection.is_open)


class VisaTransport(Transport):
    """SCPI transport for USBTMC and TCP/IP resources exposed through VISA."""

    def __init__(self, settings: VisaSettings):
        self._settings: Final[VisaSettings] = settings
        self._resource_manager = None
        self._resource = None

    @staticmethod
    def _create_resource_manager():
        if pyvisa is None:
            raise RuntimeError(
                "PyVISA is not installed. Install dependencies from requirements.txt first."
            )
        try:
            return pyvisa.ResourceManager()
        except Exception:
            return pyvisa.ResourceManager("@py")

    @classmethod
    def list_resources(cls) -> list[str]:
        if pyvisa is None:
            return []
        manager = None
        try:
            manager = cls._create_resource_manager()
            return list(manager.list_resources())
        except Exception:
            return []
        finally:
            if manager is not None:
                try:
                    manager.close()
                except Exception:
                    pass

    def open(self) -> None:
        if self.is_open:
            return
        self._resource_manager = self._create_resource_manager()
        self._resource = self._resource_manager.open_resource(self._settings.resource_name)
        self._resource.timeout = int(self._settings.timeout_seconds * 1000)

    def close(self) -> None:
        if self._resource is not None:
            try:
                self._resource.close()
            finally:
                self._resource = None
        if self._resource_manager is not None:
            try:
                self._resource_manager.close()
            finally:
                self._resource_manager = None

    def write(self, payload: bytes) -> None:
        if not self.is_open:
            raise RuntimeError("VISA connection is not open.")
        self._resource.write_raw(payload)

    def read_until(self, terminator: bytes) -> bytes:
        del terminator
        if not self.is_open:
            raise RuntimeError("VISA connection is not open.")
        return self._resource.read_raw()

    def read_binary_response(self) -> bytes:
        """Read a complete IEEE 488.2 block even when VISA returns an early chunk."""
        if not self.is_open:
            raise RuntimeError("VISA connection is not open.")
        try:
            response = bytearray(self._resource.read_raw())
        except Exception as exc:
            raise BinaryResponseError(
                "VISA binary transfer failed before the IEEE header was received."
            ) from exc
        total_length = _ieee_binary_response_length(response)
        while total_length is None or len(response) < total_length:
            if total_length is None:
                requested_bytes = int(getattr(self._resource, "chunk_size", 20 * 1024))
            else:
                # Ask for the remaining block plus a possible CR/LF terminator so it
                # cannot be left queued ahead of the next SCPI response.
                requested_bytes = total_length - len(response) + 2
            try:
                chunk = self._resource.read_raw(size=max(2, requested_bytes))
            except Exception as exc:
                expected = "a complete IEEE header" if total_length is None else f"{total_length:,} bytes"
                raise BinaryResponseError(
                    f"VISA binary transfer stopped after {len(response):,} bytes; "
                    f"expected {expected}."
                ) from exc
            if not chunk:
                expected = "the IEEE header" if total_length is None else f"{total_length:,} bytes"
                raise BinaryResponseError(
                    f"VISA binary response ended before {expected} was received."
                )
            response.extend(chunk)
            total_length = _ieee_binary_response_length(response)
        # The DHO may include a line terminator or USBTMC padding after the declared
        # block. It has been consumed from the transport, but it is not part of the
        # IEEE response and must not be interpreted as a second block.
        return bytes(response[:total_length])

    def recover_binary_response(self) -> None:
        """Clear a failed VISA transfer, reopening the session if clear is unavailable."""
        if not self.is_open:
            raise RuntimeError("VISA connection is not open.")
        clear = getattr(self._resource, "clear", None)
        if callable(clear):
            try:
                clear()
                return
            except Exception:
                pass
        self.close()
        self.open()

    @property
    def is_open(self) -> bool:
        if self._resource is None:
            return False
        return not bool(getattr(self._resource, "is_closed", False))


def _ieee_binary_response_length(response: bytes | bytearray) -> int | None:
    """Return the end offset of the first definite-length IEEE block when known."""
    offset = 0
    while offset < len(response) and response[offset] in b"\r\n ":
        offset += 1
    if len(response) < offset + 2:
        return None
    if response[offset : offset + 1] != b"#":
        preview = bytes(response[offset : offset + 16]).hex(" ")
        raise BinaryResponseError(
            "VISA binary response does not start with an IEEE block header "
            f"(prefix: {preview or 'empty'})."
        )
    digits_byte = response[offset + 1 : offset + 2]
    if not digits_byte.isdigit() or digits_byte == b"0":
        raise BinaryResponseError("VISA binary response has an invalid definite-length header.")
    length_digits = int(digits_byte)
    header_end = offset + 2 + length_digits
    if len(response) < header_end:
        return None
    length_field = response[offset + 2 : header_end]
    if not length_field.isdigit():
        raise BinaryResponseError("VISA binary response has an invalid byte-count field.")
    return header_end + int(length_field)


class BinaryResponseError(ValueError):
    """The transport received an invalid or interrupted IEEE binary response."""
