# User Guide

## Purpose

Monitor up to four SCPI, PicoSDK, or raw serial devices in one desktop window and correlate their data in a shared timestamped CSV log.

## Supported profiles

- Multicomp Pro MP730889 DMM over serial SCPI
- OWON SPE6103 PSU over serial SCPI
- RIGOL DHO804 oscilloscope over VISA USBTMC or LAN
- PicoLog USB TC-08 thermocouple logger through the native PicoSDK driver
- Generic line-oriented raw serial device

## Installation

### Windows application

The GitHub Actions workflow produces a self-contained Windows x64 application;
Python is not required on the target PC. Open the repository's **Actions**
page, select the latest successful **Windows test and application build**, and
download the `SCPI-Lab-Instrument-App-<version>-windows-x64` artifact. Extract
the artifact and the application ZIP inside it, then run
`SCPI Lab Instrument App.exe`.

Windows may show a SmartScreen warning because this initial build is not
code-signed. VISA USB devices must have a compatible Windows driver. The bundle
contains PyVISA-py, PyUSB, and libusb, and it can also use an installed vendor
VISA runtime such as NI-VISA.

The PicoLog USB TC-08 needs Pico Technology's **64-bit PicoSDK** installed on
the Windows PC. Download it from the official
[Pico Technology downloads page](https://www.picotech.com/downloads), select
PicoLog TC-08 and the 64-bit SDK, install it, and then restart this application.
The GitHub application bundle calls the installed `usbtc08.dll` directly; it
does not bundle or silently install Pico's driver. Installing PicoLog software
may also install the driver, but installing the 64-bit PicoSDK is the explicit
supported setup.

### Run from source

Python 3.12 or newer is recommended.

```bash
cd "/Users/david/Code/Git/SCPI Lab Instrument App"
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The RIGOL profile uses PyVISA and the pure-Python PyVISA backend. A platform VISA installation may also be used when present.

## Launch

```bash
python -m dmm_app.main
```

## Workspace

The main window contains four independent instrument panels. Each panel has its own profile, connection, measurement rows, acquisition-output mode where applicable, interval, start/stop controls, logging opt-in, latest values, and output history. The top toolbar controls shared discovery, coordinated start/stop, and whether opted-in instruments use one shared CSV.

All four panels initially show `No Instrument Selected` and default to the `None` profile. Choose a profile to enable that panel's connection and measurement controls. The panel title then changes to the selected instrument name and returns to `No Instrument Selected` if `None` is selected again.

Use the `−` button beside `Profile & Connection` or `Measurement` to collapse that section independently. The output history expands into the released space. Use the resulting `+` button to restore the section; collapsing controls does not disconnect a device or stop acquisition.

### Text and graph views

Every instrument panel has a `Graph view` button above its output area. It switches the text history to a live graph without stopping acquisition or changing logging. Graph view temporarily collapses the panel's control sections to provide useful plotting space; `Text view` restores the sections to their previous expanded/collapsed state.

A flashing red dot in the graph's top-right corner means that readings are currently arriving. It does not appear merely because the instrument is connected: it starts after the first streamed result, disappears immediately when acquisition is stopped or the device is disconnected, and disappears automatically if results become stale. The stale timeout follows the configured scalar polling interval; repeated waveform mode allows at least five seconds for capture and transfer.

Changing the profile in another panel uses the connection lists already cached by the application; it does not rerun VISA discovery and therefore does not interrupt an active oscilloscope transfer. If `Refresh` is pressed during an active VISA acquisition, serial ports are refreshed but VISA discovery is deferred until the VISA acquisition has stopped.

For scalar instruments, elapsed time is the shared X-axis. Select `Last 60 s`, `Last 10 min`, or `All data`. The Y range continually rescales with an 8% margin as readings arrive. Up to two measurement rows are graphed:

- Measurement row 1 uses the blue trace and left Y-axis.
- Measurement row 2 uses the orange trace and right Y-axis.
- Each Y-axis has its own range and unit, so an OWON voltage and current remain readable together.
- Additional rows remain available in Text view and CSV logging but are not plotted.

For a DHO804 RAW capture, Graph view displays the latest completed waveform. `Scope viewport` uses the applied time/div across ten horizontal divisions and V/div across eight vertical divisions, with dashed trigger-time and trigger-level markers. `Full RAW record` displays the complete memory record using the preamble's real sample interval. Large records are rendered as min/max envelopes so narrow overshoot is not lost through simple point skipping.

### Select logging destinations

`Log this instrument` is always the control that decides whether a panel is logged. A connected or running panel can therefore remain visible and monitored without being written to disk.

To combine selected instruments in one file:

1. Enable `Use shared CSV` and choose the common CSV when prompted. `Choose shared file` can change that destination later.
2. Enable `Log this instrument` only in the panels that should be recorded. These panel selections use the common file immediately and do not ask for another filename.
3. Leave the checkbox off for any monitored device that should not be logged.

To give instruments separate files, leave `Use shared CSV` off and enable `Log this instrument` in each required panel. Each panel asks for its own filename. Cancelling leaves that panel unchecked. The panel checkbox tooltip shows its current destination.

If `Use shared CSV` is turned off while panels are selected, the app warns that their logging will also be disabled. Continuing unticks all of those panels; enable each required panel again to choose its individual file. Cancelling the warning leaves shared mode and all panel selections unchanged. The previous shared filename remains remembered for later reuse.

### Save and load panel configurations

Click `Save config…` to store the complete four-panel workspace in a readable, versioned JSON file. The file includes:

- Instrument profiles, manually entered or discovered endpoint text, baud rates and line endings.
- Measurement rows and sources, acquisition output modes and intervals.
- Text/Graph selection, graph range and the remembered expanded/collapsed sections.
- All DHO804 setup fields and logging-coverage goal.
- TC-08 mains rejection, temperature units, and all eight thermocouple types.
- Shared/individual logging mode, panel logging selections and their CSV destinations.

Click `Load config…` and select a saved `.json` file to restore the workspace. Stop acquisition, finish any DHO804 Tools operation and disconnect every instrument first; the app refuses to replace a live panel configuration. The complete file is checked for the supported format, version, profiles, measurements and control selections before it is applied.

Loading is deliberately non-operational: devices remain disconnected, workers do not start, previous readings and graph samples are not restored, and DHO804 settings are not considered applied to the hardware. The DHO804 ground/probe safety checkbox is always cleared, even if it was selected when the file was saved. Reconnect the scope, verify the present physical connection, select the safety checkbox and click `Apply setup` before capturing.

## Connect a serial SCPI instrument

1. Select the DMM or PSU profile in a panel.
2. Select a serial port and baud rate. Click `Refresh` if the port is missing.
3. Click `Connect`.
4. The app requests `*IDN?` and rejects a profile/device mismatch.
5. Select the measurement rows and polling interval.

The MP730889 is intentionally limited to one row. The SPE6103 can monitor voltage and current in the same cycle.

## Connect a PicoLog USB TC-08

1. Install the 64-bit PicoSDK on Windows, connect one TC-08 by USB, and close
   PicoLog or any other program that may already have the logger open.
2. Select `PicoLog USB TC-08` in a panel.
3. Expand `PicoLog TC-08 Setup`. Choose 50 or 60 Hz mains rejection, choose the
   display units, and select the thermocouple type for every connected channel.
   Leave unused channels as `Disabled` to reduce conversion time. Channel 1
   defaults to Type K.
4. In `Measurement`, add the channels you want to display or log. `Cold
   junction` is also available as a measurement source. Every selected physical
   channel must be enabled in the setup section.
5. Click `Connect`. The first available USB TC-08 is opened and its driver,
   hardware, serial, and calibration information is shown in the panel output.
6. Click `Snapshot` for one complete conversion or `Start` for repeated
   conversions. Enable `Log this instrument` to include those readings in the
   individual or shared CSV.

The TC-08 converts enabled channels sequentially. The panel reports the
driver's minimum conversion time after connection. A short application interval
does not make the hardware convert faster: each repeated read completes a full
on-demand conversion, then waits for any remaining configured interval. This
first integration supports one TC-08 per panel selection and opens the first
available unit.

## Connect a RIGOL DHO804

1. Connect the scope by USB device cable or configure its LAN connection.
2. Select `RIGOL DHO804 oscilloscope` in a panel.
3. Select a discovered VISA resource. A resource string can also be typed directly when discovery does not list it.
4. Click `Connect`; the returned identity must contain `DHO804`.
5. Select a measurement such as `Voltage average` and a source from `CHANnel1` through `CHANnel4`.
6. Use `Add` for more unique measurement/source pairs.

The profile supports voltage average, RMS, peak-to-peak, maximum, minimum, and frequency. The queried channel must be enabled and have a usable waveform for the scope to return a meaningful result.

Typical resource strings resemble `USB0::...::INSTR` for USBTMC or `TCPIP0::<address>::INSTR` for LAN. The exact string is supplied by VISA discovery and depends on the instrument configuration.

### SMPS scope setup

Expand `Oscilloscope Setup` in the DHO804 panel. The section starts with the attached workflow's moderate repeated-capture defaults:

- CH1, with `Only selected channel` enabled
- ×10 probe, DC coupling, full 70 MHz bandwidth
- 10 V/div and 1 µs/div
- Normal acquisition, 1 Mpoint memory and 1,000,000 transfer points
- Rising-edge single trigger at −24 V with a 30-second wait timeout
- RAW waveform mode and WORD transfer format

Adjust the voltage scale and trigger level for the actual switch-node range. Enter the switching frequency in kHz, select 2–5 visible cycles, and use `Calculate time/div`; the result follows `cycles / (10 × switching frequency)`. You can enter a smaller time/div directly when inspecting an edge.

### Optimise waveform logging coverage

`Calculate time/div` is intended for choosing what is visible on screen. `Optimise logging coverage` instead uses the selected transfer point count to capture as much time as possible while preserving a chosen minimum waveform fidelity.

1. Enter the switching frequency in `Switch kHz`.
2. Select the memory depth and point count. Selecting a memory depth fills in its maximum point count; you may then enter fewer transfer points if required.
3. Select a `Logging goal`:
   - `Maximum coverage` targets 100 samples per switching cycle.
   - `Balanced` targets 250 samples per switching cycle and is the recommended starting point for cycle-to-cycle SMPS behaviour.
   - `Edge/ringing detail` targets the maximum DHO804 sample rate and gives up record span to preserve fast transitions.
4. Click `Optimise logging coverage`.

The button calculates time/div, selects Normal acquisition and `Repeated RAW waveforms`, and sets the minimum interval to `0` (re-arm as soon as capture and transfer permit). It does not change the selected memory depth or point count because those directly control file size. The preview shows planned sample rate, samples per cycle, record span, switching cycles per capture, and RAW WORD payload size.

With one selected channel, the planner uses the DHO804's 1.25 GSa/s maximum. If other channels are not explicitly disabled it uses the conservative 312.5 MSa/s three/four-channel limit. After `Apply setup`, the app queries `:ACQuire:SRATe?` and replaces the plan with the scope's actual sample rate and resulting span because the instrument couples timebase, sample rate and memory depth.

At 500 kHz with one million points, the three plans are approximately:

| Goal | Planned rate | Record span | Switching cycles | WORD payload |
|---|---:|---:|---:|---:|
| Maximum coverage | 50 MSa/s | 20 ms | 10,000 | 1.91 MiB |
| Balanced | 125 MSa/s | 8 ms | 4,000 | 1.91 MiB |
| Edge/ringing detail | 1.25 GSa/s | 0.8 ms | 400 | 1.91 MiB |

Larger records can reduce the proportion of fixed SCPI overhead, but increase transfer time, memory use and disk consumption. Start with 1 Mpoint and `Balanced`, then use the measured coverage result to decide whether more span or more edge detail is useful.

The safety checkbox is intentionally required before `Apply setup` is enabled. The DHO804 is non-isolated and its channel, chassis and interface grounds are common. Connect a passive probe ground only to a return that has been verified to be at oscilloscope earth potential; otherwise use an appropriately rated differential probe. Never defeat the oscilloscope protective earth.

Click `Apply setup` to send the complete setup and prepare RAW WORD transfer. If a field is changed afterwards, apply the setup again before capturing.

### DHO804 Tools and burst capability test

Click `Tools…` beside `Optimise logging coverage` to open the modeless DHO804 Tools window. The main four-panel workspace remains visible. The tools window reuses that panel's existing SCPI/VISA connection; it never opens a second VISA session and never performs resource discovery.

The `Waveform recording` tab is the validation step required before production hardware-burst logging is enabled:

1. Stop normal acquisition and apply the desired oscilloscope setup.
2. Click `Query maximum` to run `:RECord:WRECord:FMAX?` for the current memory-depth/channel configuration.
3. Start with 10 test frames, a `1e-8` second requested frame interval, and a 60-second timeout.
4. Choose a parent output directory.
5. Click `Run capability test`.

The scope records the requested frames internally before the app begins USB/LAN transfer. The test then selects each replay frame, reads its scope-provided timestamp and waveform preamble, retrieves the full RAW WORD block, verifies its point count, and saves `frame_XXXXXX.bin` plus `frame_XXXXXX.bin.json`. It also calculates a SHA-256 digest and writes `diagnostic.json` for the whole test. A repeated digest is labelled `Unproven: duplicate payload`: identical data may be legitimate for a stable signal, but it cannot prove that frame selection changed, so use a naturally varying or modulated test waveform when possible.

The capability test is intentionally bounded to 1,000 frames and an estimated 2 GiB of RAW data. RIGOL documents that waveform recording is unavailable in UltraAcquire mode, so use Normal or Peak detect. The number of points in each hardware-recorded frame follows the selected memory depth, even if the ordinary transfer-point field is smaller.

`Stop operation` asks recording or frame export to stop safely. A diagnostic operation changes recording/playback state; when it finishes or fails, the app disables waveform recording, stops the scope, marks the normal setup as unapplied, and requires `Apply setup` before normal capture or logging.

The expert `SCPI console` tab supports line-oriented ASCII queries and writes. It is disabled during any normal acquisition or tools operation. Binary `:WAVeform:DATA?` queries are blocked, query commands cannot be sent as an unread write, and reset-like writes require confirmation. Every operation appears in `Command log`; any console write marks the normal setup as unapplied.

### Single RAW waveform capture

1. Apply the oscilloscope setup.
2. Click `Single capture…` and choose a `.bin` destination.
3. The app sends `:SINGle` in a background worker and polls `:TRIGger:STATus?`.
4. After the scope reports `STOP`, the app reads the waveform preamble and downloads `:WAVeform:DATA?`.
5. The chosen file receives the exact WORD payload. A neighboring `.bin.json` file records the setup, DHO identity, software-correlated capture time, preamble/scaling fields, byte count and point count.

The capture also creates a `Waveform capture` entry in the selected shared or individual CSV when logging is enabled for that panel. Its timestamp is when the app first observes `STOP`, not a hardware-precise trigger timestamp.

RIGOL documents WORD as two bytes per point and specifies the scaling preamble, but the programming guide does not state the WORD byte order. A known 1.4 MHz sine-wave capture on DHO804 firmware `00.01.03` validated little-endian WORD data. The graph therefore applies `voltage = (word − y_origin − y_reference) × y_increment`; the original bytes and all scaling fields remain preserved for auditability.

Use 100 kpoints or 1 Mpoint for repeated work. A 1 Mpoint WORD capture is about 2 MB. Use 10–25 Mpoints for startup, shutdown, load steps or rare faults; these transfers create longer blind periods, and 25 Mpoints requires only one enabled channel on the DHO804.

### Repeated RAW waveform logging

The DHO804 panel's `Output` selector controls what its `Start` button does:

- `Measurement values` repeatedly queries the configured average/RMS/peak/frequency rows and writes scalar results to the panel's selected CSV.
- `Repeated RAW waveforms` repeatedly arms a single trigger, waits for `STOP`, downloads a RAW WORD record, and saves it with a JSON sidecar. The interval is a minimum start-to-start interval. Set it to `0` to add no deliberate wait; capture and USB/LAN transfer still impose unavoidable blind time.

To use repeated waveform logging:

1. Enable `Log this instrument` for the DHO804 panel. In shared mode it uses the selected shared CSV without another prompt; otherwise choose the oscilloscope's individual CSV when prompted.
2. Configure the oscilloscope, verify safety, and click `Apply setup`.
3. Select `Repeated RAW waveforms` under `Output`.
4. Set the minimum interval, normally `0` for maximum coverage, and click `Start`. Click `Stop` to finish after the current transfer and any already queued file writes.

The first capture queries the waveform preamble. Because applied settings cannot change during a run, later captures reuse it and avoid that extra SCPI round trip. Binary and JSON files are written through a bounded background queue so normal disk latency does not delay the next arm; if storage cannot keep up, the bounded queue applies back-pressure instead of allowing memory use to grow without limit.

Large VISA responses may arrive in multiple transport chunks. The app reads the IEEE block header, obtains the announced payload length, and continues reading until the complete block has arrived. Transport terminators or padding beyond the declared block are consumed but excluded from decoding. Empty blocks are rejected rather than being logged as zero-point captures.

If a repeated capture receives a malformed, empty, or interrupted IEEE block, the app records an `Acquisition recovery` message in Text view, clears the VISA session (or reopens it if clearing is unavailable), restores the RAW WORD transfer settings, and re-arms the scope. A successful capture resets the failure count. Three consecutive IEEE failures stop acquisition so a persistent cable, VISA, or instrument fault cannot loop silently.

For a selected CSV named `test_run.csv`, waveform files are stored under:

```text
test_run_waveforms/instrument_1/
  dho804_ch1_run0001_20260819T123456_123456_000001.bin
  dho804_ch1_run0001_20260819T123456_123456_000001.bin.json
```

Each completed capture adds one `Waveform capture` row to `test_run.csv`. Its `value` is the received point count, its `unit` is `points`, and `raw_response` is the corresponding `.bin` path. The sample array remains in the binary file so the CSV stays manageable. In shared mode, separate `instrument_N` subdirectories prevent waveform files from different panels colliding; individual CSV mode uses the same directory convention.

The panel graph updates to the newest successfully saved waveform. This plotting work happens after capture and does not replace or modify the `.bin` file.

After the second capture, the logging preview reports record span, measured start-to-start cadence, captures per minute, estimated blind gap and timeline coverage, RAW transfer time/throughput, file-write time, payload size and projected GiB/hour. Coverage is estimated as:

```text
record span / measured start-to-start interval × 100%
```

The same values are stored under `timing` in each JSON sidecar. They are software measurements, not hardware trigger timestamps; pre-trigger buffering and status-query latency can affect the estimate.

### Choosing a scope capture strategy

- **One long RAW capture** gives the strongest continuity guarantee: there are no gaps within the captured record. Use a large memory depth and an appropriate sample rate for startup, shutdown or a bounded interval where every switching cycle matters.
- **Repeated RAW logging** can run indefinitely and is implemented by this application, but the scope cannot acquire while the stopped waveform is being transferred. Use the optimiser and zero interval to minimise that gap.
- **Hardware burst/segmented recording** can capture many triggered frames into scope memory without a USB/LAN transfer between frames, so it should give much better within-burst coverage. It is not yet enabled in the app: full RAW frame retrieval, timestamps and re-arm timing must first be verified on the physical DHO804. After the internal memory fills there will still be a longer transfer pause, so burst recording is not unlimited gapless streaming.

## Monitor raw serial output

1. Select `Raw serial monitor` in a panel.
2. Select the port, baud rate, and the device's line ending (LF, CRLF, or CR).
3. Click `Connect`, then `Start`.

The monitor never sends SCPI commands. It continuously records complete terminated lines. If a line begins with a number, that number is also stored in the numeric `value` column; the full text always remains in `raw_response`.

## Synchronized acquisition

1. Connect every instrument that participates in the run.
2. Configure each SCPI panel's rows or oscilloscope output mode and interval.
3. For one correlated file, enable `Use shared CSV`, choose its file, and enable `Log this instrument` in each participating panel. For separate files, leave shared mode off and enable each participating panel, choosing its individual destination when prompted.
4. Click `Start all connected`.

Start All prepares each stopped, connected worker, resets the shared elapsed clock, and releases the workers through one coordination gate. Each completed response is timestamped from the same monotonic timebase. `Stop all` stops every worker.

This is software correlation: ordinary readings are timestamped when the application completes a read, while a waveform capture is timestamped when the app observes the scope's `STOP` state. USB/LAN/serial latency and status-polling delay can introduce skew. Edge/single trigger control is included, but shared external hardware-trigger synchronization is not.

## CSV format

Each row contains:

```text
timestamp,elapsed_seconds,acquisition_run,instrument_window,measurement_slot,device_name,connection,device_idn,function,source,value,unit,raw_response
```

- `timestamp` is timezone-aware and includes microseconds.
- `elapsed_seconds` is derived from the shared monotonic clock with nine decimal places.
- `acquisition_run` increments whenever Start All resets elapsed time, so repeated runs in one file remain distinguishable. Individually started readings before the first Start All use run 0.
- `instrument_window` and `measurement_slot` are 1-based.
- `connection` identifies the serial port, VISA resource, or PicoSDK USB connection.
- `raw_response` preserves exactly what was decoded from the instrument, excluding a raw serial line terminator.
- For a waveform-capture row, `raw_response` contains the associated RAW `.bin` path rather than the waveform samples themselves.

Events collected during each UI queue drain are sorted by their elapsed timestamp before being written.

`Log this instrument` opts one panel into logging. `Use shared CSV` changes where selected panels write; it never opts an unchecked panel into logging. Choosing a shared file alone does not start logging.

## Troubleshooting

- No serial ports: check the cable, adapter driver, OS permissions, then click `Refresh all connections`.
- No VISA resources: confirm the USB device/LAN interface is enabled on the scope. A known VISA resource can be entered manually.
- PyVISA backend error: reactivate the environment and run `python -m pip install -r requirements.txt`.
- TC-08 driver not found: install the 64-bit PicoSDK, then restart the app. A
  32-bit SDK cannot be loaded by this 64-bit Windows application.
- TC-08 device not found: connect the USB logger directly, close PicoLog and
  other programs using it, then reconnect. Only one application can hold a
  TC-08 handle at a time.
- TC-08 channel reports `OVER-RANGE`: check thermocouple wiring and type, and
  confirm the input is within the logger and thermocouple range.
- Instrument mismatch: select the profile matching the connected device and reconnect.
- DHO804 returns an invalid/overflow value: enable the selected channel, ensure a waveform is being acquired, and check vertical/timebase settings.
- Single capture times out: confirm that the waveform crosses the selected edge and trigger level, or increase the timeout.
- Waveform file is smaller than expected: verify selected memory depth/points and confirm only the intended channels are enabled.
- Actual rate differs from the optimiser preview: use the `Applied actual` result; the DHO804 chooses its actual rate from timebase, memory depth and active channels.
- Coverage remains low with interval `0`: inspect the reported RAW transfer time. Reducing point count usually improves capture cadence; lowering sample rate increases the time represented by each transferred payload.
- Repeated `Acquisition recovery` messages or stop after three IEEE errors: check the USB/LAN cable and VISA resource, reduce waveform point count to test transfer stability, and compare USB with LAN. The app retries isolated corrupt or interrupted blocks automatically; repeated failures indicate that the transport or scope is not delivering its announced payload reliably.
- Missing raw serial lines: verify baud rate and line ending. This release uses 8 data bits, no parity, and one stop bit.

## Current gaps to validate on hardware

- Confirm the DHO804 identity and measurement responses for the installed firmware and chosen USB/LAN interface.
- Confirm whether software receive timestamps provide enough accuracy for the SMPS switching investigation.
- Validate optimiser plans against `:ACQuire:SRATe?`, preamble `x_increment`, and measured trigger-to-trigger timing on the physical DHO804 over both USB and LAN.
- Run DHO804 Tools with a varying input signal and validate that replay-frame selection produces distinct full RAW WORD payloads, usable hardware timestamps, and sensible inter-frame spacing before enabling production hardware burst/segmented capture.
- Validate TC-08 driver discovery, device information, all required thermocouple
  types, overflow reporting, conversion cadence, and long-duration stop/reconnect
  behavior on the physical Windows bench system.
- If software timing is insufficient, define the required accuracy and shared trigger wiring before adding cross-device external-trigger synchronization.
