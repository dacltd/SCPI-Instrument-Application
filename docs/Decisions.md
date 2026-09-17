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
Give each instrument panel an explicit logging checkbox, retain a shared all-instruments override, prompt for a file destination when either is enabled, and write selected readings to one CSV. This routing behaviour was superseded by the 2026-08-20 decision below; the per-panel opt-in remains.

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
- Cons: trigger-observed timestamps include polling/transport latency; little-endian voltage conversion is hardware-validated for DHO804 firmware `00.01.03` and must be rechecked if a future firmware behaves differently.

## 2026-08-19 - RAW waveform files with CSV manifest rows

### Decision
Keep large DHO804 waveform arrays in individual RAW WORD `.bin` files with JSON sidecars, and write one correlated summary/path row per capture to the panel's selected shared or individual CSV. Provide repeated waveform capture as an explicit alternative to scalar measurement polling.

### Why
A 1 Mpoint waveform is about 2 MB, so placing every sample directly in a CSV would make it unwieldy and would mix incompatible scalar and array-shaped data. Manifest rows keep timing searchable while preserving each waveform losslessly.

### Consequences
- Pros: repeated captures are explicit, waveform files remain lossless, and shared mode can correlate them with selected instruments while individual mode can keep runs separate.
- Cons: a complete run consists of the CSV plus its associated waveform directory, and voltage decoding still requires the JSON preamble.

## 2026-08-19 - Per-panel text and graph views

### Decision
Give every instrument panel a Text/Graph output toggle. Plot at most two scalar measurement rows against shared elapsed time with independent left/right Y axes, and plot the latest DHO804 RAW waveform using its preamble and applied scope divisions.

### Why
Operators need to see trends without exporting the CSV, while voltage/current pairs require independent scales. Oscilloscope records need calibrated time/voltage presentation without placing a million samples in the UI or CSV.

### Consequences
- Pros: live DMM/PSU trends, readable dual-unit OWON plots, and immediate visual verification of saved scope captures without extra plotting dependencies.
- Cons: only the first two scalar rows are graphed, graph view temporarily collapses controls for space, and the initial scope view shows the latest capture rather than persistence across many captures.

## 2026-08-19 - Recoverable IEEE transfers and fresh-data indication

### Decision
Treat malformed, empty, and interrupted IEEE waveform blocks as recoverable during repeated RAW logging. Clear or reopen the VISA session, restore the DHO804 RAW transfer commands, and retry, but stop after three consecutive failures. Show a flashing red graph dot only while an active stream has delivered fresh data.

### Why
Large scope transfers can fail transiently without meaning that the instrument has disconnected. Stopping on the first bad block loses useful unattended logging time, while continuing indefinitely would hide a persistent fault. Connection state alone also cannot tell an operator whether new results are actually arriving.

### Consequences
- Pros: isolated transfer faults recover automatically, persistent faults remain visible and bounded, and Graph view gives immediate feedback about real data flow.
- Cons: each recovery adds a capture gap, the failed record cannot be reconstructed, and the freshness timeout is an application-level indication rather than proof of the instrument's internal acquisition state.

## 2026-08-19 - Do not rediscover VISA during active acquisition

### Decision
Cache serial and VISA endpoint lists after explicit discovery. Profile changes populate their panel from that cache, and refresh requests defer VISA discovery while any VISA worker is active.

### Why
Opening a second VISA resource manager for discovery can disturb an in-progress USBTMC binary response. Panel configuration must remain independent from acquisition already running in another panel.

### Consequences
- Pros: adding or changing an instrument profile cannot corrupt an oscilloscope stream, and explicit refresh remains safe during capture.
- Cons: newly connected VISA devices will not appear until active VISA acquisition is stopped and discovery is refreshed.

## 2026-08-19 - Modeless DHO804 tools and staged burst validation

### Decision
Place waveform-record diagnostics and expert SCPI access in a separate modeless DHO804 Tools window instead of expanding the four-panel workspace. Automate a bounded record/replay test that saves and hashes every selected RAW frame, but do not expose waveform recording as a production logging mode until the physical DHO804 proves that replay selection, timestamps and full RAW export work together.

### Why
The main panel already contains the controls needed during ordinary acquisition, while frame tables and command history need substantially more space. The programming guide documents recording and replay commands but does not explicitly guarantee that `:WAVeform:DATA?` returns the currently selected replay frame in RAW mode.

### Consequences
- Pros: physical capability testing is repeatable and auditable, advanced controls do not clutter normal operation, and the console remains useful for future firmware investigation.
- Pros: tools and acquisition share one locked SCPI client, preventing a second VISA session or interleaved responses from corrupting binary transfers.
- Cons: production burst logging remains explicitly gated on bench results, and state-changing tool operations require the normal scope setup to be reapplied.

## 2026-08-20 - Shared CSV is a destination mode, not a log-all override

### Decision

Make each panel's `Log this instrument` checkbox the sole participation control. When `Use shared CSV` is enabled, all opted-in panels write to one selected file without further filename prompts. When it is disabled, opting in a panel asks for that panel's individual CSV. Turning off active shared mode requires confirmation and unticks every participating panel so no instrument is silently redirected to an old or implicit individual destination.

### Why

Operators may need to monitor connected instruments that should not become part of the recorded dataset. Participation and destination are separate choices: a panel checkbox answers whether to log, while shared mode answers where the selected panels log.

### Consequences

- Pros: monitored-only instruments remain excluded, shared runs require only one filename, and independent runs have explicit per-panel destinations.
- Pros: cancelling the shared-mode exit preserves the active configuration; accepting it produces a clear no-logging state before individual files are selected.
- Cons: switching from shared to individual mode deliberately requires re-enabling and assigning each desired panel.

## 2026-08-20 - Versioned JSON workspace configurations load into a safe idle state

### Decision

Provide top-level `Save config…` and `Load config…` actions for a versioned JSON document covering all four panels and logging routing. Require every instrument to be disconnected and idle before load. Restore reusable UI settings but never restore connection/acquisition state, readings, waveform data, DHO804 safety acknowledgement, or the claim that scope settings have already been applied.

### Why

Bench arrangements are repeatedly reused and manually rebuilding profiles, measurement rows and detailed oscilloscope controls is slow and error-prone. Connection and safety state describe the present physical bench, however, so treating yesterday's saved state as proof of today's wiring would be unsafe.

### Consequences

- Pros: repeatable setups, human-readable files, explicit schema versioning and validation before mutation.
- Pros: manually entered VISA/serial endpoints remain reusable even when discovery does not currently find the device.
- Cons: operators must reconnect devices and re-verify/apply DHO804 settings after every load.

## 2026-09-03 - Load the installed native PicoSDK driver for USB TC-08 support

### Decision

Add the PicoLog USB TC-08 as a first-class instrument profile and call the
installed 64-bit `usbtc08` driver through a small, typed `ctypes` adapter. Load
the DLL only when a TC-08 connection is requested. Use PicoSDK Get Single mode
for snapshots and repeated full conversions in the first release.

### Why

The TC-08 API is a stable native C interface and the required driver is already
distributed by Pico Technology. A local adapter keeps the packaged application
self-contained at the Python layer, avoids depending on a third-party wrapper,
and lets Windows CI build and smoke-test the application without connected lab
hardware or proprietary driver installation.

### Consequences

- Pros: eight thermocouple channels, cold-junction readings, all standard
  thermocouple types, four temperature units, mains rejection, overflow status,
  graphs, CSV logging, and saved configurations fit the existing panel model.
- Pros: users who do not use a TC-08 can run the application without PicoSDK.
- Cons: the target Windows PC must have the 64-bit PicoSDK installed separately,
  and GitHub CI can validate the adapter with a simulated driver but cannot
  validate a physical logger.
- Cons: Get Single mode timestamps conversions on receipt and is best suited to
  ordinary temperature logging; a later hardware-validated streaming mode would
  be needed for uninterrupted device-clock acquisition at the fastest cadence.
