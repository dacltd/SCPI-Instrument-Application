from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class InstrumentType(str, Enum):
    NONE = "None"
    MP730889 = "Multicomp Pro MP730889 DMM"
    OWON_SPE6103 = "OWON SPE6103 PSU"
    RIGOL_DHO804 = "RIGOL DHO804 oscilloscope"
    RAW_SERIAL = "Raw serial monitor"


class ConnectionKind(str, Enum):
    SERIAL = "Serial"
    VISA = "VISA (USB/LAN)"


class ProtocolType(str, Enum):
    SCPI = "SCPI"
    RAW_SERIAL = "Raw serial"


class MeasurementFunction(str, Enum):
    VOLTAGE = "Voltage"
    CURRENT = "Current"
    VOLTAGE_AVERAGE = "Voltage average"
    VOLTAGE_RMS = "Voltage RMS"
    VOLTAGE_PEAK_TO_PEAK = "Voltage peak-to-peak"
    VOLTAGE_MAXIMUM = "Voltage maximum"
    VOLTAGE_MINIMUM = "Voltage minimum"
    FREQUENCY = "Frequency"
    RAW_DATA = "Serial data"
    WAVEFORM_CAPTURE = "Waveform capture"


@dataclass(frozen=True)
class SerialSettings:
    port: str
    baudrate: int
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    timeout_seconds: float = 1.0


@dataclass(frozen=True)
class VisaSettings:
    resource_name: str
    timeout_seconds: float = 2.0


@dataclass(frozen=True)
class Reading:
    timestamp: datetime
    elapsed_seconds: float
    acquisition_run: int
    instrument_index: int
    slot_index: int
    instrument: InstrumentType
    connection: str
    device_idn: str
    function: MeasurementFunction
    source: str
    raw_response: str
    value: float | None
    unit: str
