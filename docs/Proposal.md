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
8. Log only explicitly selected panels, routing them either into one shared CSV in timestamp order or into independent per-panel CSV files.
9. Query and validate SCPI device identity (`*IDN?`) against the selected profile.
10. Configure an independent polling interval for each SCPI panel.
11. Add/remove unique measurement/source rows where the instrument supports them.
12. Monitor DHO804 CH1-CH4 voltage average, RMS, peak-to-peak, maximum, minimum, and frequency using parameterized measurement commands.
13. Preserve the complete raw serial line while also parsing an initial numeric token when present.
14. Collapse each panel's profile/connection and measurement controls independently so the read-out history can use the released space.
15. Default every panel to a safe `None` profile and display the selected instrument name as the panel title.
16. Configure the DHO804 channel, probe ratio, coupling, bandwidth, vertical/time scale, memory depth, acquisition type, and edge trigger from a reusable oscilloscope setup section.
17. Arm a single trigger, poll until the scope reaches `STOP`, download a RAW WORD record, and save the unmodified payload with a JSON preamble/metadata sidecar.
18. Repeatedly capture DHO804 RAW waveforms into a run-specific directory and add one correlated CSV manifest row per completed capture.
19. Optimise repeated waveform coverage from switching frequency, selected point count, channel state, and a coverage/balanced/edge-detail fidelity target, then query the applied sample rate from the scope.
20. Support zero-added-delay repeated capture, cache stable waveform preamble data, write waveform files through a bounded background queue, and report measured transfer, file-write, blind-gap, and coverage estimates.
21. Toggle every panel between its text history and a live graph. Scalar instruments plot at most two configured rows against shared elapsed time with independent left/right Y axes; DHO804 waveform captures plot calibrated voltage against preamble-derived time in either the configured scope viewport or the full RAW record.
22. Provide a modeless DHO804 Tools window for maximum waveform-record frame queries, automated hardware-record capability testing, per-frame timestamp/RAW verification, diagnostic export, command logging, and guarded expert SCPI access.
23. Save and load all four panels' reusable settings in a versioned JSON configuration file without reconnecting hardware, starting acquisition, or treating a saved oscilloscope safety acknowledgement/setup as currently valid.

## Non-Goals (current release)
- Full command coverage for either instrument in first release.
- Calibration workflows.
- Remote/cloud telemetry.
- Hardware-level synchronization, shared GPIO/trigger wiring, or guaranteed cross-device timing accuracy.
- Production segmented/history-frame logging and converter-event orchestration until the diagnostic workflow has validated lossless frame selection/export on the physical DHO804.

## Architecture Summary
- `Transport` layer: a common interface with serial and VISA implementations.
- `SCPIClient`: thread-safe SCPI write/query operations.
- `Instrument profiles`: declarative instrument-specific command mappings.
- `AcquisitionClock`: one monotonic timebase converted to timezone-aware wall timestamps.
- `Polling workers`: independent background SCPI pollers plus a continuous raw serial reader.
- `Oscilloscope workflow`: validated setup command generation, coverage planning, manual and repeated single-trigger workers, recoverable IEEE binary-block decoding, bounded asynchronous file persistence, timing telemetry, and lossless waveform/metadata export.
- `DHO804 tools`: a modeless, profile-specific diagnostic surface that reuses the panel SCPI client, records frames in scope memory, verifies replay selection/timestamps/RAW payloads, and provides a guarded text SCPI console without opening another VISA session.
- `Plotting`: dependency-free Qt painting with rolling/all-data scalar views, independent two-trace Y scales, little-endian DHO804 WORD decoding, scope division grids, trigger markers, min/max waveform envelopes that preserve narrow peaks, and a fresh-data indicator.
- `Logger`: one shared CSV stream for opted-in panels when shared mode is selected, or independent CSV streams and paths for opted-in panels when shared mode is off.
- `Configuration`: validated, versioned JSON persistence for application logging routing and each panel's profile, endpoint, measurements, acquisition, graph and oscilloscope controls; runtime connection and hardware-applied state are excluded.
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
- RIGOL DHO804 waveform-record validation path: `:RECord:WRECord:FMAX?`, frame count/interval, record operation, replay frame selection/timestamp, then the existing RAW WORD transfer for each selected frame.

Note: The reviewed Multicomp manual is for MP730424. Runtime model verification via `*IDN?` is required for MP730889 compatibility.
The DHO804 command form and measurement tokens follow the official [RIGOL DHO800/DHO900 Programming Guide](https://download.rigol.com/en/Manual/Digital%20Oscilloscope/DHO800/DHO800900_ProgrammingGuide_EN.pdf).

## Extensibility Plan
- Add additional functions by extending instrument profiles.
- Add an OWON oscilloscope profile through the same VISA and function/source abstractions.
- Add Keithley Battery Simulator support as a new instrument profile (ID validation + command map + control/read workflows).
- Add advanced serial settings (parity/stop bits/timeout) in the GUI without changing core architecture.
- Add an optional external hardware synchronization input after timing requirements are established.
- Add a hardware burst/segmented mode for rare converter events after confirming that every selected DHO804 frame can be retrieved as the expected RAW WORD record with a reliable timestamp. Internal recording should use frame count/interval and defer USB/LAN transfer until the burst completes.
- Add scalar fixed-range controls and optional multi-waveform persistence/overlay after the initial automatically scaled graph workflow is validated in use.
- Extend the current IEEE-specific retry policy into structured error codes and policies for other recoverable SCPI failures.

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
- `Log this instrument` is always the participation control. `Use shared CSV` routes opted-in panels to one selected file without automatically including unchecked panels or prompting them for additional filenames.
- With shared mode off, enabling a panel's logging asks for that panel's individual CSV. Turning shared mode off confirms that every participating panel will be unticked and must be re-enabled with an individual destination.
- Logged rows contain acquisition run, panel, endpoint, measurement/source, wall timestamp, elapsed seconds, value, and raw response.
- Collapsing either control section preserves connection/acquisition state and expands the panel's read-out area.
- On startup every panel reads `No Instrument Selected`; selecting a profile updates its title and enables its controls.
- DHO804 setup can restrict acquisition to one channel and configure ×10 probing, DC coupling, full bandwidth, timebase, memory, normal acquisition, and rising/falling/either-edge single trigger.
- A single capture waits for `STOP` off the UI thread and saves an exact WORD payload plus JSON setup, timing, identity, and waveform-preamble metadata.
- Repeated waveform mode uses `Start`/`Stop`, saves every completed RAW capture under a directory derived from its selected shared or individual CSV name, and records its point count and file path in that CSV.
- The logging-coverage optimiser provides 100-samples/cycle, 250-samples/cycle, and maximum-sample-rate profiles; it updates time/div, selects normal repeated RAW capture, and sets the minimum interval to zero.
- After setup, the GUI reports the actual queried sample rate and calculated record span; during logging it reports measured start-to-start cadence, estimated blind gap/coverage, RAW transfer time, and file-write time.
- Repeated capture queries the stable preamble once per run and uses a bounded background writer so ordinary disk writes do not delay re-arming; each JSON sidecar preserves the per-capture timing telemetry.
- VISA waveform reads follow the IEEE block's announced byte count across transport chunks and reject incomplete or empty payloads before creating waveform files.
- Repeated RAW logging clears or reopens a failed VISA stream, restores the DHO transfer settings, and retries malformed, empty, or interrupted IEEE blocks. Three consecutive transfer failures stop acquisition with a clear error.
- Each panel can switch between Text and Graph without affecting acquisition. Graph view temporarily collapses the control sections to maximise plot area and restores their prior state on return to Text.
- A flashing red dot appears at the graph's top right only after fresh data arrives during an active stream; it disappears on Stop/disconnect or when readings become stale.
- Changing another panel's profile uses cached endpoints, and endpoint refresh defers VISA discovery while a VISA worker is active, so UI configuration cannot disturb an in-progress waveform transfer.
- `Save config…` records all four panels and logging routing in versioned JSON. `Load config…` validates the complete document before applying it, requires all instruments to be disconnected, preserves manually entered endpoints, and never reconnects, starts acquisition, restores readings, or marks DHO804 safety/setup as applied.
- Scalar graphs display measurement slots one and two only, using blue/left and orange/right independently auto-scaled axes with one shared selectable time window.
- DHO804 graphs decode the hardware-validated little-endian WORD payload with the stored preamble and display the latest waveform using the configured time/div, V/div and trigger level or the complete RAW record.
- DHO804 Tools remains modeless but serialises all operations through the panel's existing SCPI client. Its controls lock during normal acquisition, and the panel's acquisition controls lock during a diagnostic operation.
- The burst capability test saves each selected frame as RAW WORD plus JSON, records its hardware timestamp and SHA-256 digest, identifies duplicate payloads as unproven selection, and writes an aggregate diagnostic JSON.
- Raw console queries and writes execute off the UI thread; binary waveform queries and unread query-as-write operations are blocked, and any console write invalidates the applied normal scope setup.
