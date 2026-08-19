# SCPI Lab Instrument App Proposal

## Goal
Build a desktop acquisition workspace for engineers to monitor up to four SCPI or raw serial devices at once, correlate their readings on one local software timebase, and scale to additional instruments and transports without redesigning the GUI or acquisition pipeline.

## Scope (Current release)
1. Show four identical, independently configurable instrument panels in a 2×2 workspace.
2. Support Multicomp MP730889 DMM and Owon SPE6103 PSU serial-SCPI profiles.
3. Support a RIGOL DHO804 oscilloscope profile over VISA (USBTMC or LAN).
4. Support a raw serial monitor with selectable port, baud rate, and LF/CRLF/CR line termination.
5. Connect, disconnect, start, stop, and snapshot each applicable panel independently.
6. Provide coordinated `Start all connected` and `Stop all` controls.
7. Give every acquisition event a timezone-aware wall-clock timestamp and nanosecond-derived elapsed time from one shared monotonic software clock.
8. Merge readings from individually selected panels into one shared CSV in timestamp order, with an all-panels override.
9. Query and validate SCPI device identity (`*IDN?`) against the selected profile.
10. Configure an independent polling interval for each SCPI panel.
11. Add/remove unique measurement/source rows where the instrument supports them.
12. Monitor DHO804 CH1-CH4 voltage average, RMS, peak-to-peak, maximum, minimum, and frequency using parameterized measurement commands.
13. Preserve the complete raw serial line while also parsing an initial numeric token when present.
14. Collapse each panel's profile/connection and measurement controls independently so the read-out history can use the released space.
15. Default every panel to a safe `None` profile and display the selected instrument name as the panel title.
16. Configure the DHO804 channel, probe ratio, coupling, bandwidth, vertical/time scale, memory depth, acquisition type, and edge trigger from a reusable oscilloscope setup section.
17. Arm a single trigger, poll until the scope reaches `STOP`, download a RAW WORD record, and save the unmodified payload with a JSON preamble/metadata sidecar.
18. Repeatedly capture DHO804 RAW waveforms into a run-specific directory and add one correlated shared-CSV manifest row per completed capture.
19. Optimise repeated waveform coverage from switching frequency, selected point count, channel state, and a coverage/balanced/edge-detail fidelity target, then query the applied sample rate from the scope.
20. Support zero-added-delay repeated capture, cache stable waveform preamble data, write waveform files through a bounded background queue, and report measured transfer, file-write, blind-gap, and coverage estimates.

## Non-Goals (current release)
- Full command coverage for either instrument in first release.
- Calibration workflows.
- Remote/cloud telemetry.
- Hardware-level synchronization, shared GPIO/trigger wiring, or guaranteed cross-device timing accuracy.
- Segmented/history-frame RAW retrieval and converter-event orchestration until lossless frame selection/export has been validated on the physical DHO804.
- Automatic WORD-to-voltage conversion until DHO804 byte order has been confirmed on physical hardware.

## Architecture Summary
- `Transport` layer: a common interface with serial and VISA implementations.
- `SCPIClient`: thread-safe SCPI write/query operations.
- `Instrument profiles`: declarative instrument-specific command mappings.
- `AcquisitionClock`: one monotonic timebase converted to timezone-aware wall timestamps.
- `Polling workers`: independent background SCPI pollers plus a continuous raw serial reader.
- `Oscilloscope workflow`: validated setup command generation, coverage planning, manual and repeated single-trigger workers, IEEE binary-block decoding, bounded asynchronous file persistence, timing telemetry, and lossless waveform/metadata export.
- `Logger`: one CSV persistence stream filtered by per-panel opt-ins or an all-panels override.
- `GUI`: PySide6 (Qt) four-panel workspace with independent and coordinated controls.

## SCPI Baseline Used
From the reviewed Multicomp and Owon SCPI manuals:
- Identity: `*IDN?`
- MP730889 voltage read path: `SYSTem:REMote` + `CONFigure:VOLTage:DC` then `MEAS1?`
- MP730889 current read path: `SYSTem:REMote` + `CONFigure:CURRent:DC` then `MEAS1?`
- Owon SPE6103 voltage read path: `SYSTem:REMote` then `MEASure:VOLTage?`
- Owon SPE6103 current read path: `SYSTem:REMote` then `MEASure:CURRent?`
- RIGOL DHO804 channel measurement path: `:MEASure:ITEM? <item>,CHANnel<n>`; `<item>` is selected from VAVG, VRMS, VPP, VMAX, VMIN, and FREQuency.
- RIGOL DHO804 SMPS setup path: channel display/probe/coupling/bandwidth, main timebase, normal acquisition, memory depth, edge trigger and single sweep.
- RIGOL DHO804 capture path: `:SINGle`, poll `:TRIGger:STATus?` for `STOP`, then query RAW WORD waveform data and its ten-field preamble.
- RIGOL DHO804 applied-rate verification: `:ACQuire:SRATe?` after setup, because sample rate, timebase and memory depth are coupled by the instrument.

Note: The reviewed Multicomp manual is for MP730424. Runtime model verification via `*IDN?` is required for MP730889 compatibility.
The DHO804 command form and measurement tokens follow the official [RIGOL DHO800/DHO900 Programming Guide](https://download.rigol.com/en/Manual/Digital%20Oscilloscope/DHO800/DHO800900_ProgrammingGuide_EN.pdf).

## Extensibility Plan
- Add additional functions by extending instrument profiles.
- Add an OWON oscilloscope profile through the same VISA and function/source abstractions.
- Add Keithley Battery Simulator support as a new instrument profile (ID validation + command map + control/read workflows).
- Add advanced serial settings (parity/stop bits/timeout) in the GUI without changing core architecture.
- Add an optional external hardware synchronization input after timing requirements are established.
- Add a hardware burst/segmented mode for rare converter events after confirming that every selected DHO804 frame can be retrieved as the expected RAW WORD record with a reliable timestamp. Internal recording should use frame count/interval and defer USB/LAN transfer until the burst completes.
- Confirm DHO804 WORD byte order on hardware, then add calibrated CSV/plot export using the stored preamble.
- Introduce structured error codes and retry policies in SCPI layer.

## Open validation questions

- Is the raw device output line-oriented text using 8-N-1 framing, or does it require binary/unframed capture or advanced serial settings?
- Which DHO804 voltage statistic best represents the SMPS event in practice (average, RMS, peak-to-peak, maximum, or minimum)?
- After physical testing, is software receive-time correlation accurate enough, or is a shared hardware trigger and explicit scope trigger control required?
- Is USBTMC or LAN the preferred DHO804 connection for the bench setup? Both are supported through VISA, but their latency and discovery behavior differ.
- Do DHO804 waveform-record playback frames remain individually addressable through `:WAVeform:DATA?` as full RAW WORD records on the installed firmware, and what is the measured hardware re-arm gap?
- Which external signal should be used if startup, load-step, protection or rare-event triggering is added?

## Acceptance Criteria
- The application displays exactly four independent instrument panels.
- User can connect serial profiles by port/baud and the DHO804 by a discovered or manually entered VISA resource.
- User can issue `*IDN?` for any SCPI panel.
- Connection is rejected if selected profile does not match returned IDN.
- User can select voltage/current readings and DHO804 measurement/source pairs and read live values.
- OWON supports multi-row voltage/current polling in one interval cycle.
- MP remains single-row and prevents multi-measurement configuration.
- Raw serial captures complete terminated lines without issuing SCPI commands.
- `Start all connected` releases all prepared workers from one coordination gate and resets the shared elapsed clock.
- Poll interval is editable and enforced independently per SCPI panel.
- Snapshot captures all configured rows in the selected panel.
- Per-panel logging writes only selected instruments; `Shared CSV log` includes every panel in the same file.
- Logged rows contain acquisition run, panel, endpoint, measurement/source, wall timestamp, elapsed seconds, value, and raw response.
- Collapsing either control section preserves connection/acquisition state and expands the panel's read-out area.
- On startup every panel reads `No Instrument Selected`; selecting a profile updates its title and enables its controls.
- DHO804 setup can restrict acquisition to one channel and configure ×10 probing, DC coupling, full bandwidth, timebase, memory, normal acquisition, and rising/falling/either-edge single trigger.
- A single capture waits for `STOP` off the UI thread and saves an exact WORD payload plus JSON setup, timing, identity, and waveform-preamble metadata.
- Repeated waveform mode uses `Start`/`Stop`, saves every completed RAW capture under a directory derived from the shared CSV name, and records its point count and file path in the CSV.
- The logging-coverage optimiser provides 100-samples/cycle, 250-samples/cycle, and maximum-sample-rate profiles; it updates time/div, selects normal repeated RAW capture, and sets the minimum interval to zero.
- After setup, the GUI reports the actual queried sample rate and calculated record span; during logging it reports measured start-to-start cadence, estimated blind gap/coverage, RAW transfer time, and file-write time.
- Repeated capture queries the stable preamble once per run and uses a bounded background writer so ordinary disk writes do not delay re-arming; each JSON sidecar preserves the per-capture timing telemetry.
