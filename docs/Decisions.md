# Decisions

## 2026-02-11 - Python + PySide6 (Qt) desktop application
### Decision
Use a Python desktop app with PySide6 (Qt) for the GUI implementation.

### Why
Avoids system Tk runtime issues on modern macOS and provides a stronger widget/tooling baseline while keeping serial integration straightforward.

### Alternatives considered
- Tkinter: smaller dependency footprint but unstable on current macOS runtime in this environment.
- Electron/Tauri: more packaging complexity for hardware I/O MVP.

### Consequences
- Pros: stable modern GUI runtime and richer UI controls.
- Cons: larger dependency/runtime footprint than Tkinter.

## 2026-02-11 - Supported runtime and dependency baseline
### Decision
Use Homebrew Python 3.12 (or newer) in a project-local virtual environment and install dependencies from `requirements.txt`.

### Why
System Python 3.9 on macOS caused installation/runtime issues while installing GUI dependencies; a modern user-managed interpreter is more reliable.

### Alternatives considered
- Continue with Apple Command Line Tools Python 3.9.
- Pin older PySide6 versions to fit system Python.

### Consequences
- Pros: predictable dependency installs and fewer platform-specific crashes.
- Cons: requires one-time setup of Python toolchain for each developer machine.

## 2026-02-11 - Layered architecture (transport/client/poller/gui)
### Decision
Separate transport, SCPI command handling, polling, and GUI concerns.

### Why
Improves testability and supports extension to additional transports and meter functions without major rewrites.

### Alternatives considered
- Single-file monolithic GUI script.
- GUI directly issuing serial I/O without intermediary abstractions.

### Consequences
- Pros: maintainable and scalable design.
- Cons: slightly more initial code and project structure overhead.

## 2026-02-11 - Command registry for measurement functions
### Decision
Represent supported DMM functions as data entries in a command catalog.

### Why
Adding new functions becomes additive and low risk (new map entry plus UI exposure).

### Alternatives considered
- Hard-coded branching logic for each mode in GUI handlers.
- Dynamic runtime command entry with no schema.

### Consequences
- Pros: cleaner scaling path and clearer ownership of SCPI details.
- Cons: requires discipline to keep command metadata accurate.

## 2026-02-11 - CSV logging with opt-in checkbox and file prompt
### Decision
Give each instrument panel an explicit logging checkbox, retain a shared all-instruments override, prompt for a file destination when either is enabled, and write selected readings to one CSV.

### Why
Matches lab workflow expectations and keeps logging behavior explicit and auditable.

### Alternatives considered
- Always-on default logging.
- Database-backed logging from first release.

### Consequences
- Pros: simple, transparent, selective per instrument, and broadly compatible.
- Cons: no built-in query/index capabilities beyond CSV tools.

## 2026-02-11 - Runtime identity verification
### Decision
Use `*IDN?` as a baseline validation step after connection.

### Why
Reference manual reviewed targets MP730424; runtime check reduces risk when targeting MP730889.

### Alternatives considered
- Assume full command compatibility.
- Block functionality unless exact model string matches expected target.

### Consequences
- Pros: pragmatic safety gate without blocking development.
- Cons: still requires physical verification for complete command coverage.

## 2026-02-11 - Instrument profile selection (MP730889 + Owon SPE6103)
### Decision
Introduce a selectable instrument profile in the GUI and route SCPI measurement/configuration commands through instrument-specific command maps.

### Why
The Multicomp DMM and Owon PSU use different measurement command patterns (for example, `MEAS1?` vs `MEASure:VOLTage?`), so command routing must be explicit to avoid protocol mismatches.

### Alternatives considered
- Keep one global command set and ask users to adapt manually.
- Hard-code branch logic in GUI event handlers without a profile model.

### Consequences
- Pros: scalable structure for adding current and other measurements per instrument.
- Cons: slightly more setup logic and testing surface area.

## 2026-02-11 - Enforce instrument identity validation at connect
### Decision
After connection, query `*IDN?` and reject the session if the returned identity does not match the selected instrument profile.

### Why
Prevents sending the wrong SCPI command set to a mismatched device and avoids silent misconfiguration.

### Alternatives considered
- Warn only and allow continued operation.
- No validation and rely on user discipline.

### Consequences
- Pros: safer operation and clearer operator feedback.
- Cons: requires maintaining profile ID match tokens and may need tuning for firmware/model variants.

## 2026-02-11 - Multi-row measurements with capability gating
### Decision
Support multiple measurement rows for OWON (voltage/current) and lock MP730889 to a single active measurement row.

### Why
OWON can read both voltage and current via separate SCPI queries in one cycle, while MP730889 is constrained to one active measurement mode at a time.

### Alternatives considered
- Keep a single global measurement row for all devices.
- Allow multi-row on MP and accept mode-switch side effects.

### Consequences
- Pros: aligns UI behavior to instrument capability and minimizes operator error.
- Cons: adds dynamic row state management and per-cycle batching logic.

## 2026-08-19 - Four-panel acquisition workspace
### Decision
Use four independent instrument panels in a fixed 2×2 workspace, with both per-panel controls and coordinated start/stop controls.

### Why
The primary workflow needs simultaneous DMM, PSU, oscilloscope, and device-under-test serial monitoring without opening separate application processes.

### Consequences
- Pros: each connection remains isolated while the operator can view and control the complete test setup in one window.
- Cons: the application needs a larger minimum window and concurrent worker lifecycle management.

## 2026-08-19 - Shared software acquisition clock
### Decision
Timestamp every completed read against one application-owned monotonic clock and derive a timezone-aware wall timestamp from that same origin. Coordinated Start All uses a common worker gate and resets elapsed time.

### Why
This gives all serial, USB, and LAN readings a comparable local timeline without requiring hardware changes.

### Consequences
- Pros: robust against system clock adjustments and suitable for correlating ordinary lab observations.
- Cons: timestamps represent response receipt, so transport latency and sequential SCPI query time remain part of the measurement uncertainty. Hardware triggers are a future option if tighter accuracy is required.

## 2026-08-19 - VISA transport and parameterized oscilloscope profile
### Decision
Add a common VISA transport for USBTMC/LAN instruments and model DHO804 measurements as function/source pairs.

### Why
Oscilloscope channels are sources rather than separate instrument profiles, and VISA keeps the transport reusable for other instrument families.

### Consequences
- Pros: supports discovered or manually entered VISA resources and makes additional channel-based instruments additive.
- Cons: adds PyVISA/PyVISA-py runtime dependencies and still requires physical hardware validation.

## 2026-08-19 - Lossless DHO804 single-shot waveform capture
### Decision
Configure SMPS acquisition through a dedicated oscilloscope setup model, arm a single edge trigger in a worker thread, poll for STOP, and export the RAW WORD bytes with a JSON preamble/metadata sidecar.

### Why
Moderate RAW records preserve switching-edge detail without blocking the UI or repeatedly transferring the scope's full memory. Keeping setup, identity and timing beside the payload makes captures auditable.

### Consequences
- Pros: implements repeatable 100 kpoint–25 Mpoint captures and preserves source data without a lossy or undocumented conversion.
- Cons: trigger-observed timestamps include polling/transport latency, and calibrated voltage conversion remains blocked on physical confirmation of WORD byte order.

## 2026-08-19 - RAW waveform files with CSV manifest rows

### Decision
Keep large DHO804 waveform arrays in individual RAW WORD `.bin` files with JSON sidecars, and write one correlated summary/path row per capture to the selected shared CSV. Provide repeated waveform capture as an explicit alternative to scalar measurement polling.

### Why
A 1 Mpoint waveform is about 2 MB, so placing every sample directly in the shared CSV would make it unwieldy and would mix incompatible scalar and array-shaped data. Manifest rows keep cross-instrument timing searchable while preserving each waveform losslessly.

### Consequences
- Pros: repeated captures are explicit, waveform files remain lossless, and the shared CSV correlates them with other instruments.
- Cons: a complete run consists of the CSV plus its associated waveform directory, and voltage decoding still requires the JSON preamble and confirmed WORD byte order.
