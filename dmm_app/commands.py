from __future__ import annotations

from dataclasses import dataclass

from dmm_app.models import (
    ConnectionKind,
    InstrumentType,
    MeasurementFunction,
    ProtocolType,
)


@dataclass(frozen=True)
class MeasurementCommand:
    function: MeasurementFunction
    prepare_commands: tuple[str, ...]
    query_command: str
    unit: str

    def query_for_source(self, source: str = "") -> str:
        return self.query_command.format(source=source)


@dataclass(frozen=True)
class InstrumentProfile:
    instrument: InstrumentType
    protocol: ProtocolType
    connection_kind: ConnectionKind
    idn_query: str
    idn_expected_tokens: tuple[str, ...]
    commands: dict[MeasurementFunction, MeasurementCommand]
    sources: tuple[str, ...] = ()
    maximum_rows: int = 1

    @property
    def supports_identity_query(self) -> bool:
        return bool(self.idn_query)

    @property
    def is_raw_serial(self) -> bool:
        return self.protocol == ProtocolType.RAW_SERIAL


INSTRUMENT_PROFILES: dict[InstrumentType, InstrumentProfile] = {
    InstrumentType.NONE: InstrumentProfile(
        instrument=InstrumentType.NONE,
        protocol=ProtocolType.SCPI,
        connection_kind=ConnectionKind.SERIAL,
        idn_query="",
        idn_expected_tokens=(),
        commands={},
        maximum_rows=0,
    ),
    InstrumentType.MP730889: InstrumentProfile(
        instrument=InstrumentType.MP730889,
        protocol=ProtocolType.SCPI,
        connection_kind=ConnectionKind.SERIAL,
        idn_query="*IDN?",
        idn_expected_tokens=("MULTICOMP", "MP730889"),
        commands={
            MeasurementFunction.VOLTAGE: MeasurementCommand(
                function=MeasurementFunction.VOLTAGE,
                prepare_commands=("SYSTem:REMote", "CONFigure:VOLTage:DC"),
                query_command="MEAS1?",
                unit="V",
            ),
            MeasurementFunction.CURRENT: MeasurementCommand(
                function=MeasurementFunction.CURRENT,
                prepare_commands=("SYSTem:REMote", "CONFigure:CURRent:DC"),
                query_command="MEAS1?",
                unit="A",
            ),
        },
    ),
    InstrumentType.OWON_SPE6103: InstrumentProfile(
        instrument=InstrumentType.OWON_SPE6103,
        protocol=ProtocolType.SCPI,
        connection_kind=ConnectionKind.SERIAL,
        idn_query="*IDN?",
        idn_expected_tokens=("OWON", "SPE6103"),
        commands={
            MeasurementFunction.VOLTAGE: MeasurementCommand(
                function=MeasurementFunction.VOLTAGE,
                prepare_commands=("SYSTem:REMote",),
                query_command="MEASure:VOLTage?",
                unit="V",
            ),
            MeasurementFunction.CURRENT: MeasurementCommand(
                function=MeasurementFunction.CURRENT,
                prepare_commands=("SYSTem:REMote",),
                query_command="MEASure:CURRent?",
                unit="A",
            ),
        },
        maximum_rows=2,
    ),
    InstrumentType.RIGOL_DHO804: InstrumentProfile(
        instrument=InstrumentType.RIGOL_DHO804,
        protocol=ProtocolType.SCPI,
        connection_kind=ConnectionKind.VISA,
        idn_query="*IDN?",
        idn_expected_tokens=("DHO804",),
        sources=("CHANnel1", "CHANnel2", "CHANnel3", "CHANnel4"),
        maximum_rows=8,
        commands={
            MeasurementFunction.VOLTAGE_AVERAGE: MeasurementCommand(
                function=MeasurementFunction.VOLTAGE_AVERAGE,
                prepare_commands=(),
                query_command=":MEASure:ITEM? VAVG,{source}",
                unit="V",
            ),
            MeasurementFunction.VOLTAGE_RMS: MeasurementCommand(
                function=MeasurementFunction.VOLTAGE_RMS,
                prepare_commands=(),
                query_command=":MEASure:ITEM? VRMS,{source}",
                unit="V",
            ),
            MeasurementFunction.VOLTAGE_PEAK_TO_PEAK: MeasurementCommand(
                function=MeasurementFunction.VOLTAGE_PEAK_TO_PEAK,
                prepare_commands=(),
                query_command=":MEASure:ITEM? VPP,{source}",
                unit="V",
            ),
            MeasurementFunction.VOLTAGE_MAXIMUM: MeasurementCommand(
                function=MeasurementFunction.VOLTAGE_MAXIMUM,
                prepare_commands=(),
                query_command=":MEASure:ITEM? VMAX,{source}",
                unit="V",
            ),
            MeasurementFunction.VOLTAGE_MINIMUM: MeasurementCommand(
                function=MeasurementFunction.VOLTAGE_MINIMUM,
                prepare_commands=(),
                query_command=":MEASure:ITEM? VMIN,{source}",
                unit="V",
            ),
            MeasurementFunction.FREQUENCY: MeasurementCommand(
                function=MeasurementFunction.FREQUENCY,
                prepare_commands=(),
                query_command=":MEASure:ITEM? FREQuency,{source}",
                unit="Hz",
            ),
        },
    ),
    InstrumentType.PICOLOG_TC08: InstrumentProfile(
        instrument=InstrumentType.PICOLOG_TC08,
        protocol=ProtocolType.PICOSDK,
        connection_kind=ConnectionKind.PICOSDK,
        idn_query="",
        idn_expected_tokens=(),
        sources=(
            "Channel 1",
            "Channel 2",
            "Channel 3",
            "Channel 4",
            "Channel 5",
            "Channel 6",
            "Channel 7",
            "Channel 8",
            "Cold junction",
        ),
        maximum_rows=9,
        commands={
            MeasurementFunction.TEMPERATURE: MeasurementCommand(
                function=MeasurementFunction.TEMPERATURE,
                prepare_commands=(),
                query_command="",
                unit="°C",
            )
        },
    ),
    InstrumentType.RAW_SERIAL: InstrumentProfile(
        instrument=InstrumentType.RAW_SERIAL,
        protocol=ProtocolType.RAW_SERIAL,
        connection_kind=ConnectionKind.SERIAL,
        idn_query="",
        idn_expected_tokens=(),
        commands={
            MeasurementFunction.RAW_DATA: MeasurementCommand(
                function=MeasurementFunction.RAW_DATA,
                prepare_commands=(),
                query_command="",
                unit="",
            )
        },
    ),
}


def idn_matches_profile(profile: InstrumentProfile, idn: str) -> bool:
    normalized = (idn or "").upper()
    return any(token in normalized for token in profile.idn_expected_tokens)
