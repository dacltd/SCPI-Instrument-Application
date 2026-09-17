from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class InstrumentType(str, Enum):
    NONE = "None"
    MP730889 = "Multicomp Pro MP730889 DMM"
    OWON_SPE6103 = "OWON SPE6103 PSU"
    RIGOL_DHO804 = "RIGOL DHO804 oscilloscope"
    PICOLOG_TC08 = "PicoLog USB TC-08"
    RAW_SERIAL = "Raw serial monitor"
    KEITHLEY_2281S = "Keithley 2281S-20-6 battery simulator"
    GSMIV_POWER = "GSMIV power telemetry"


class ConnectionKind(str, Enum):
    SERIAL = "Serial"
    VISA = "VISA (USB/LAN)"
    PICOSDK = "PicoSDK USB"


class ProtocolType(str, Enum):
    SCPI = "SCPI"
    RAW_SERIAL = "Raw serial"
    PICOSDK = "PicoSDK"


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
    BATTERY_VOLTAGE = "Battery voltage"
    BATTERY_CURRENT = "Battery current (+ charging)"
    BATTERY_SOC = "Battery state of charge"
    BATTERY_CAPACITY = "Battery remaining capacity"
    BATTERY_OPEN_CIRCUIT_VOLTAGE = "Battery open-circuit voltage"
    BATTERY_RESISTANCE = "Battery internal resistance"
    EXTERNAL_VOLTAGE = "External input voltage"
    CHARGER_INPUT_VOLTAGE = "Charger input voltage"
    SYSTEM_VOLTAGE = "System voltage"
    WAVEFORM_CAPTURE = "Waveform capture"
    TEMPERATURE = "Temperature"


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
