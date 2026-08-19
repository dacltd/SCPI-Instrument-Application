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

    @property
    def is_open(self) -> bool:
        if self._resource is None:
            return False
        return not bool(getattr(self._resource, "is_closed", False))
