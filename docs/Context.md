# Context

## Project one liner
Four-panel desktop acquisition application for correlating SCPI measurements and raw serial device output on one local software timebase.

## Current objective
Monitor up to four independently connected serial or VISA instruments, including a RIGOL DHO804 and a raw serial data source, and write all readings to one synchronized CSV timeline.

## Workspace path
- `/Users/david/Code/Git/SCPI Lab Instrument App`

## Repo map
- `dmm_app/`: application source code.
- `dmm_app/models.py`: domain models (serial settings, measurement function, reading).
- `dmm_app/clock.py`: shared monotonic acquisition clock.
- `dmm_app/transport.py`: transport abstraction with serial and VISA implementations.
- `dmm_app/scpi.py`: SCPI client wrapper for command/query.
- `dmm_app/commands.py`: instrument profiles and measurement command catalog.
- `dmm_app/poller.py`: background polling worker.
- `dmm_app/oscilloscope.py`: DHO804 setup model, trigger worker, and RAW waveform export.
- `dmm_app/logging_util.py`: CSV logging helper.
- `dmm_app/gui.py`: PySide6 (Qt) GUI and orchestration.
- `dmm_app/main.py`: app entrypoint.
- `docs/`: project docs and decision logs.
- `requirements.txt`: runtime dependencies.

## Key decisions (with dates)
- 2026-02-11: Chose Python desktop app for rapid delivery and straightforward serial integration.
- 2026-02-11: Migrated GUI from Tkinter to PySide6 (Qt) for better cross-platform runtime stability and richer UI controls.
- 2026-02-11: Standardized runtime on Homebrew Python 3.12 virtual environments to avoid Apple system Python + Tk/packaging issues.
- 2026-02-11: Added instrument-profile selection to support SCPI command nuances between MP730889 DMM and Owon SPE6103 PSU.
- 2026-02-11: Added connection-time IDN validation so selected instrument must match the connected device identity string.
- 2026-02-11: Added multi-row measurement scheduling for Owon so voltage and current can be queried in the same interval cycle.
- 2026-02-11: Added duplicate-function guards for multi-row measurements.
- 2026-02-11: Chose modular architecture (transport/client/poller/commands/gui) to support future expansion to all supported instrument functions.
- 2026-02-11: Chose CSV as the initial log format for interoperability with lab workflows.

## Constraints
- OS: macOS development environment; target desktop OS may include Windows/macOS/Linux.
- Tooling: Python 3.12+ recommended, `pyserial`, `PyVISA`, `PyVISA-py`, `PySide6` (Qt), virtual environment per repo.
- Security: local-only communication with instrument; no remote service exposure in MVP.
- Performance targets:
  - Poll interval configurable from 200 ms to 60,000 ms.
  - UI remains responsive during polling through background worker thread.

## Interfaces/contracts
- Device transport contract:
  - Open/close connection.
  - Write bytes.
  - Read until terminator.
- SCPI command contract:
  - Command strings terminated with newline.
  - Query returns ASCII text line.
- Data logging contract:
  - CSV columns include wall timestamp, elapsed seconds, instrument window, endpoint, measurement/source, numeric value, and raw response.
  - Each instrument panel has an independent logging opt-in; the shared logging control includes every panel as an override.
  - Repeated DHO804 waveform logging stores RAW WORD payloads and JSON metadata beside the CSV and writes one CSV manifest row per capture.
- GUI interaction contract:
  - User selects instrument profile prior to connection.
  - User selects serial port/baud or a VISA resource prior to connection.
  - Enabling either per-panel or shared logging triggers file selection when no destination exists.
  - OWON supports multiple measurement rows; MP is single-row only.
  - DHO804 `Start` supports either scalar measurement polling or repeated single-trigger RAW waveform logging.

## Open questions / risks
- Manual reviewed was for MP730424; MP730889 command parity and transport behavior need hardware validation.
- Line termination and timeout details may vary by firmware revision.
- Some devices require explicit remote-control enablement before SCPI commands.
- Serial parameters beyond baud (parity/stop bits) may need exposure for certain interfaces/adapters.
- PySide6 installation can fail on older/system Python distributions; team should align on one supported Python runtime.

## Next actions
- Add OWON oscilloscope support as an additional profile (ID validation + SCPI command map + capability gating in GUI).
- Validate DHO804 VISA discovery, identity response, and measurement queries on physical hardware.
- Validate DHO804 setup, single-trigger status, RAW WORD payload size and preamble on physical hardware.
- Confirm WORD byte order against a known waveform before adding automatic voltage conversion.
- Validate repeated DHO804 capture cadence, stop behavior, and generated manifest paths on physical hardware.
- Define shared external-trigger synchronization only if software-correlated timestamps prove insufficient.
- Add Keithley Battery Simulator support as a new instrument profile with connect/control/read capability.
- Add OWON SPE stepped output sequencing: allow configuring voltage/current mode steps with per-step time and target voltage/current entries.
- Validate MP730889 `*IDN?` response and baseline SCPI commands on real hardware.
- Confirm required serial framing details and update defaults if needed.
- Add GUI controls for advanced serial options (parity, data bits, stop bits, timeout).
- Add additional measurement functions to `dmm_app/commands.py`.
- Add physical-hardware integration tests after the bench devices are available.
- Add structured status/error indicators (warning/error levels) in UI.
- Add log rotation or session-based file naming option.
- Add model-specific capability gating after ID query.
- Add packaging guidance (PyInstaller or equivalent) once runtime/toolchain is finalized.

## Glossary
- DMM: Digital Multimeter.
- SCPI: Standard Commands for Programmable Instruments.
- MVP: Minimum Viable Product.
- IDN: Instrument identity query (`*IDN?`).
- Polling: Periodic query loop for measurement collection.
- CSV: Comma Separated Values log format.
- Venv: Python virtual environment for isolated dependencies.
