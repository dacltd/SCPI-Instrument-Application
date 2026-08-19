# User Guide

## Purpose

Monitor up to four SCPI or raw serial devices in one desktop window and correlate their data in a shared timestamped CSV log.

## Supported profiles

- Multicomp Pro MP730889 DMM over serial SCPI
- OWON SPE6103 PSU over serial SCPI
- RIGOL DHO804 oscilloscope over VISA USBTMC or LAN
- Generic line-oriented raw serial device

## Installation

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

The main window contains four independent instrument panels. Each panel has its own profile, connection, measurement rows, acquisition-output mode where applicable, interval, start/stop controls, logging opt-in, latest values, and output history. The top toolbar controls shared discovery, coordinated start/stop, and the shared CSV log.

All four panels initially show `No Instrument Selected` and default to the `None` profile. Choose a profile to enable that panel's connection and measurement controls. The panel title then changes to the selected instrument name and returns to `No Instrument Selected` if `None` is selected again.

Use the `−` button beside `Profile & Connection` or `Measurement` to collapse that section independently. The output history expands into the released space. Use the resulting `+` button to restore the section; collapsing controls does not disconnect a device or stop acquisition.

## Connect a serial SCPI instrument

1. Select the DMM or PSU profile in a panel.
2. Select a serial port and baud rate. Click `Refresh` if the port is missing.
3. Click `Connect`.
4. The app requests `*IDN?` and rejects a profile/device mismatch.
5. Select the measurement rows and polling interval.

The MP730889 is intentionally limited to one row. The SPE6103 can monitor voltage and current in the same cycle.

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

### Single RAW waveform capture

1. Apply the oscilloscope setup.
2. Click `Single capture…` and choose a `.bin` destination.
3. The app sends `:SINGle` in a background worker and polls `:TRIGger:STATus?`.
4. After the scope reports `STOP`, the app reads the waveform preamble and downloads `:WAVeform:DATA?`.
5. The chosen file receives the exact WORD payload. A neighboring `.bin.json` file records the setup, DHO identity, software-correlated capture time, preamble/scaling fields, byte count and point count.

The capture also creates a `Waveform capture` entry in the shared CSV when logging is enabled. Its timestamp is when the app first observes `STOP`, not a hardware-precise trigger timestamp.

RIGOL documents WORD as two bytes per point and specifies the scaling preamble, but the programming guide does not state the WORD byte order. The app therefore preserves the original bytes and records this limitation instead of guessing. Calibrated voltage conversion will be added after byte order is confirmed against a known waveform on the physical DHO804.

Use 100 kpoints or 1 Mpoint for repeated work. A 1 Mpoint WORD capture is about 2 MB. Use 10–25 Mpoints for startup, shutdown, load steps or rare faults; these transfers create longer blind periods, and 25 Mpoints requires only one enabled channel on the DHO804.

### Repeated RAW waveform logging

The DHO804 panel's `Output` selector controls what its `Start` button does:

- `Measurement values` repeatedly queries the configured average/RMS/peak/frequency rows and writes scalar results to the shared CSV.
- `Repeated RAW waveforms` repeatedly arms a single trigger, waits for `STOP`, downloads a RAW WORD record, and saves it with a JSON sidecar. The interval is a minimum start-to-start interval. Set it to `0` to add no deliberate wait; capture and USB/LAN transfer still impose unavoidable blind time.

To use repeated waveform logging:

1. Choose the shared CSV file and enable `Log this instrument` for the DHO804 panel, or enable `Shared CSV log`.
2. Configure the oscilloscope, verify safety, and click `Apply setup`.
3. Select `Repeated RAW waveforms` under `Output`.
4. Set the minimum interval, normally `0` for maximum coverage, and click `Start`. Click `Stop` to finish after the current transfer and any already queued file writes.

The first capture queries the waveform preamble. Because applied settings cannot change during a run, later captures reuse it and avoid that extra SCPI round trip. Binary and JSON files are written through a bounded background queue so normal disk latency does not delay the next arm; if storage cannot keep up, the bounded queue applies back-pressure instead of allowing memory use to grow without limit.

For a shared CSV named `test_run.csv`, waveform files are stored under:

```text
test_run_waveforms/instrument_1/
  dho804_ch1_run0001_20260819T123456_123456_000001.bin
  dho804_ch1_run0001_20260819T123456_123456_000001.bin.json
```

Each completed capture adds one `Waveform capture` row to `test_run.csv`. Its `value` is the received point count, its `unit` is `points`, and `raw_response` is the corresponding `.bin` path. The sample array remains in the binary file so the shared CSV stays manageable.

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
3. Choose a log file, then enable `Log this instrument` in each participating panel. Alternatively, enable `Shared CSV log` to include every panel.
4. Click `Start all connected`.

Start All prepares each stopped, connected worker, resets the shared elapsed clock, and releases the workers through one coordination gate. Each completed response is timestamped from the same monotonic timebase. `Stop all` stops every worker.

This is software correlation: ordinary readings are timestamped when the application completes a read, while a waveform capture is timestamped when the app observes the scope's `STOP` state. USB/LAN/serial latency and status-polling delay can introduce skew. Edge/single trigger control is included, but shared external hardware-trigger synchronization is not.

## Shared CSV format

Each row contains:

```text
timestamp,elapsed_seconds,acquisition_run,instrument_window,measurement_slot,device_name,connection,device_idn,function,source,value,unit,raw_response
```

- `timestamp` is timezone-aware and includes microseconds.
- `elapsed_seconds` is derived from the shared monotonic clock with nine decimal places.
- `acquisition_run` increments whenever Start All resets elapsed time, so repeated runs in one file remain distinguishable. Individually started readings before the first Start All use run 0.
- `instrument_window` and `measurement_slot` are 1-based.
- `connection` identifies the serial port or VISA resource.
- `raw_response` preserves exactly what was decoded from the instrument, excluding a raw serial line terminator.
- For a waveform-capture row, `raw_response` contains the associated RAW `.bin` path rather than the waveform samples themselves.

Events collected during each UI queue drain are sorted by their elapsed timestamp before being written.

`Log this instrument` opts one panel into the selected CSV. `Shared CSV log` overrides those individual selections and includes every panel; turning it off restores the individual selections. Choosing a file alone does not start logging.

## Troubleshooting

- No serial ports: check the cable, adapter driver, OS permissions, then click `Refresh all connections`.
- No VISA resources: confirm the USB device/LAN interface is enabled on the scope. A known VISA resource can be entered manually.
- PyVISA backend error: reactivate the environment and run `python -m pip install -r requirements.txt`.
- Instrument mismatch: select the profile matching the connected device and reconnect.
- DHO804 returns an invalid/overflow value: enable the selected channel, ensure a waveform is being acquired, and check vertical/timebase settings.
- Single capture times out: confirm that the waveform crosses the selected edge and trigger level, or increase the timeout.
- Waveform file is smaller than expected: verify selected memory depth/points and confirm only the intended channels are enabled.
- Actual rate differs from the optimiser preview: use the `Applied actual` result; the DHO804 chooses its actual rate from timebase, memory depth and active channels.
- Coverage remains low with interval `0`: inspect the reported RAW transfer time. Reducing point count usually improves capture cadence; lowering sample rate increases the time represented by each transferred payload.
- Missing raw serial lines: verify baud rate and line ending. This release uses 8 data bits, no parity, and one stop bit.

## Current gaps to validate on hardware

- Confirm the DHO804 identity and measurement responses for the installed firmware and chosen USB/LAN interface.
- Confirm whether software receive timestamps provide enough accuracy for the SMPS switching investigation.
- Confirm WORD byte order against a known waveform before enabling automatic voltage conversion.
- Validate optimiser plans against `:ACQuire:SRATe?`, preamble `x_increment`, and measured trigger-to-trigger timing on the physical DHO804 over both USB and LAN.
- Validate waveform-record frame selection, RAW WORD export and frame timestamps before enabling hardware burst/segmented capture.
- If software timing is insufficient, define the required accuracy and shared trigger wiring before adding cross-device external-trigger synchronization.
