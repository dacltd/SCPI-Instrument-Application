# Timed bench automation

The **Automation** top-level tab coordinates the OWON SPE6103 and Keithley
2281S through their existing connections. Its Sequence, Setup and Run pages
share one sequence. This first version uses timed steps; graphical editing and
conditional transitions are future extensions of the same format.

## Start with the supplied USB check

1. Add and connect **OWON SPE6103**, **Keithley 2281S-20-6 battery simulator**
   and **GSMIV power telemetry**. Select voltage/current measurements on the
   OWON; use 250 ms polling initially on the two SCPI instruments. Select the
   telephone's UART at 115200 baud, LF, with no other serial monitor open.
   Use the GSMIV `m5stack-fire-power-validation` awake build for these examples.
2. Wire the OWON to the **upstream external input**, and the 2281S to **BAT**
   with the real battery disconnected. The charger-side VBUS is a different
   node, normally regulated near 4.8 V. Add the reference DMM/current shunt and
   temperature instrument as required. Scope transient captures are operated
   separately in this release; disconnect its app panel before automation.
3. Select simulator mode and an appropriate battery model on the 2281S. The
   longer example requires a model covering VOC 3.10–3.95 V. Record its model,
   resistance and limits. Both instrument outputs must be **off initially**.
   Preflight checks identities, output states, simulator mode, model voltage
   range and the Keithley error queue before issuing any setting writes.
4. Open **Automation → Load example → USB capture check**. Review the steps
   and **Setup** assignments. Double-click a cell to edit it. Role/action and
   output/method values have dropdowns. Numerical values use V, A, % and seconds.
   The OWON startup/recovery limit is 3 A as requested. Battery current limits remain
   assumptions to review, not measured limits:

   | Setting | Initial example value |
   | --- | --- |
   | OWON external voltage | **10 V**, confirmed by the operator |
   | OWON source current limit | **3 A** at startup/recovery, as requested for inrush headroom |
   | 2281S discharge current limit | **2 A** |
   | 2281S current protection | **2.5 A** |
   | 2281S terminal voltage protection | **4.25 V** for the deployed 4.20 V charger |

   To change a value outside the example's allowed range, first review and edit
   that bound on Setup. The sequence selects **static** battery simulation.
   A VOC setting is open-circuit model voltage, not loaded terminal VBAT.
5. On **Run**, enter board serial/revision, firmware commit/build, model,
   temperature, wiring/load state, instrument calibration and reference details.
   Check the setup-review box and choose **Start sequence…**. Choose a parent
   folder on your Mac. The app creates a fresh uniquely named capture folder,
   snapshots the sequence/workspace, starts shared logging for all connected
   supported scalar instruments, then runs the sequence. Stop any existing
   acquisition/download before starting.
6. Inspect the first capture: observed voltage, positive-for-charging current,
   firmware validity flags, continuity and settling. Only then run the longer
   **Battery policy plateaus** example. Save any edited sequence for reuse.

The USB example has 390 seconds (6.5 minutes) of dwell, plus command time.
The longer example has 2,415 seconds (40.25 minutes) of dwell, plus command time.
Times are provisional: the initial boot holds for 30 seconds then captures for
60 seconds; subsequent plateaus settle for 15 seconds then capture for 60.
Extend these where drift/noise indicates they are too short. A time delay does
not itself establish measurement stability.

## Execution, pause and abort

Steps execute in table order: **command → setting read-back → settling → capture**.
A hold step sends no command. Settings remain active between steps. The next step
starts after the previous one completes; slow communication extends the run.
The runner never skips settling to catch up to an absolute schedule.

- Measurements continue throughout commands, settling, capture and pause.
- **Pause** freezes the remaining dwell and prevents the next command. An
  already executing command/read-back finishes. Outputs remain at their current
  settings; pause does not freeze a dynamic physical process.
- **Abort** stops future steps and attempts every configured abort action after
  current I/O finishes. Errors, invalid scalar readings, lost acquisition and
  logging failures also abort. Abort is not an instantaneous hardware interlock.
- Setup and conflicting manual acquisition/connection controls are locked during
  the run, including cleanup. A close request initiates abort and defers closing;
  close the window again after cleanup finishes.
- **Stop all acquisition** during automation requests abort first and stops
  acquisition after cleanup. Outside automation it only stops acquisition.
- Successful completion leaves acquisition running for post-test evidence. Use
  **Stop capture** on Automation before starting another run.
- Failed or aborted sequences automatically stop their capture after cleanup,
  including failures during preflight. Controls stay locked until pending
  instrument reads finish. The status retains the failure and cleanup details,
  then reports **Capture stopped** when a new run can be started.
- **Stop capture** during a sequence requests abort and waits for cleanup first.
  Stopping capture itself does not change instrument outputs.

Both supplied sequences attempt **OWON output OFF**, then **leave the battery
unchanged**, on completion or abort. The longer sequence returns battery VOC to
3.8 V at its final timed step; abort holds whatever battery state was reached.
Cleanup never silently enables an output. Communication/protection failures
can prevent the requested end state: cleanup errors are shown in Run and recorded.
A preflight failure sends no setting writes and does not perform cleanup.

## Recorded evidence and timing

Each run directory contains:

- `readings.csv`: existing measurement schema plus rows with `device_name=Automation`
  for command requests/read-backs, settling/capture boundaries, pause/resume,
  cleanup and final outcome. Marker details are JSON in `raw_response`.
- `events.jsonl`: independently flushed automation events with the same shared
  clock; retained even if the measurement CSV fails. Frequent countdown updates
  appear in the UI only.
- `sequence.json`: the exact validated sequence used, including allowed bounds
  and completion/abort actions.
- `workspace.json`: connection/acquisition configuration and logging routes.
- `instruments.json`: connected profiles, endpoints and cached identities; fresh
  queried controller identities are also recorded in preflight events.
- `assignments.json` and `run-notes.txt`: role-to-slot mapping (zero-based slot
  indices) and operator evidence. CSV instrument numbers remain one-based.

Use timestamps and phase markers when selecting steady-state data. Rows arriving
in different GUI batches need not be physically ordered in the CSV: sort by
`acquisition_run` and `elapsed_seconds` for analysis. These timestamps describe
host command handling or receipt of readings. USB commands and voltage/current
queries are sequential; they do **not** hardware-synchronize instrument ADCs.
Use the scope for short rail dips and shutdown margins.

The SCPI client serializes each setting/write-back group against polling, using
one existing session per instrument. No second instrument connection is opened.
Acquisition may wait briefly while that group completes.

## Battery-policy coverage and remaining work

The longer example covers battery VOC points 3.10, 3.20, 3.40, 3.50, 3.80 and
3.95 V with external power and battery-only; a 10 V source-current-limit ladder
from 0.5 A down to 0.01 A, with 3 A startup/recovery; and a live upstream input sweep through 3–10 V.
It includes source insertion/removal and returns to a 3.8 V battery-only state.

Source current limits are **not requested battery currents**. Use the calibrated
battery-lead reference to identify the achieved currents and add/refine points
around the proposed +8/+20 mA classifier boundaries. Forward-mode IBAT zero is
not proof of zero battery discharge. The 2281S readout must meet the proposal's
reference-uncertainty requirements before being used as qualification evidence.

These are one-board characterization templates. They do not automate modem
loads, charger EN_CHG/HIZ, the firmware charge-pause checkpoint, temperatures,
production sleep cycles, solar profiles or the required minimum 12-board matrix.
The live VEXT sweep is not the settled checkpoint qualification. A static
simulator does not qualify real-cell stored-charge progress. The proposed
3.20 V shutdown, 3.04 V VSYSMIN and 3.95 V ceiling still require their firmware
implementation and release gates. Low-voltage battery-only points may stop the
DUT; review telemetry continuity and use a dedicated shutdown test with a scope.

## Sequence file format

JSON schema version 1 is a data format: no Python, arbitrary SCPI, shell commands,
loops or conditions are executed. Load examples in the app or use the JSON files
in [sequences](sequences/). A step is:

```json
{
  "label": "Weak external source",
  "instrument": "source",
  "action": "current_limit",
  "value": 0.05,
  "settle_s": 15,
  "capture_s": 60
}
```

Top-level fields are `version`, `name`, `notes`, `instruments`, `steps`,
`on_complete`, `on_abort`, and optional `required_observers` (`["gsmiv_power"]`
for both supplied examples). Role definitions contain `model` and numerical
`limits`, each expressed as `[minimum, maximum]`. The model IDs are
`owon_spe6103` and `keithley_2281s`. Each end/abort list must cover every role
exactly once; its order is preserved. Allowed end actions are `output` with
`false`, or `hold` with `null`.

| Instrument | Named actions |
| --- | --- |
| Both | `output` (boolean), `hold` (null), `current_limit` (A) |
| OWON | `voltage` (V) |
| 2281S | `voc` (V), `soc` (%), `method` (`"static"`/`"dynamic"`), `current_protection` (A), `voltage_protection` (V) |

Every numerical action needs an allowed range. Limits/dwells reject non-finite
values, booleans in numerical fields, unsupported fields and out-of-range values.
Explicit voltage/VOC and current-limit steps must precede output enable.
2281S method/limit/protection changes require its output off; VOC/SOC can change
while running. No model upload/recall, automatic error clearing, or reset occurs.

### Command sources

- [OWON SP/SPE/SPS programming manual](https://files.owon.com.cn/software/Application/SP_and_SPE_SPS_programming_manual.pdf),
  pages 7–10: `VOLTage`, **`CURRent`** (current setting), `OUTPut` and remote mode.
  `CURRent:LIMit` is OCP, not the current-setting command. No error-query command
  is documented for this profile, so settings are verified by read-back.
- [Keithley 2281S reference manual](https://download.tek.com/manual/077114601_2281_Ref_Mar_2019.pdf),
  sections 7-14, 7-39–48, 7-76 and 7-166: simulator mode, discharge limit,
  method/VOC/SOC/protection, battery output and error queue.

Software tests use simulated instruments. Physical command compatibility,
read-back tolerances, switching behaviour and dwell sufficiency remain bench checks.

### Multicomp acquisition startup (0.4.1)

The DMM configures its selected function once per connection/measurement change,
then reuses it for snapshots and polling. Its configurable post-configuration
settling wait defaults to 5 seconds. Automation waits for its first valid reading
before starting instrument settings; lack of readiness times out after 75 seconds.
No DMM readings are captured during that initial settling wait. See the
[Multicomp settling instructions](UserGuide.md#multicomp-snapshots-and-settling-041)
for front-panel changes and manual reapplication of the setup.

### Failed-run recovery (0.4.2)

For the reported 2281S error `723,"Not permitted with capacitor voltage > VLOW"`,
the saved capture failed during preflight, before any sequence setting writes.
Version 0.4.1 left acquisition running after that failure; **Stop all acquisition**
is the recovery control in that version. Version 0.4.2 stops failed/aborted capture
automatically and provides **Stop capture** for retained successful-run logging.

The existing capture cannot identify the operation that originally generated 723.
`SYSTem:ERRor?` reads and removes the oldest error queue entry (2281S reference
manual section 7-166). Preflight now checks the error queue before its state/range
queries, and again after them. The message distinguishes an already queued error
(originating command unknown) from one found after those queries. Acquisition
queries may already have run; an already queued error is not proof of a historical
or harmless condition. The app records the returned error and fails the run; it
does not issue a blanket clear/reset, change VLOW, or automatically retry.
Review the instrument's error/event log and resolve its reported condition before
retrying. Further queued errors can still block the next attempt.

### Read-back acceptance windows (0.4.3)

**Automation → Setup → Read-back ±** sets an absolute tolerance for each numerical
setting, in that setting's units (V, A, or percentage points for SOC). It checks
the SCPI **setting read-back**, not measured terminal voltage/current. The actual
read-back must satisfy both `abs(actual - requested) <= tolerance` and the test's
allowed minimum/maximum; tolerance never widens those bounds. Output and method
states still require exact matches. Changing a tolerance resets setup review.

The default 2281S `voltage_protection` tolerance is now **±0.05 V**, accepting the
observed request 4.25 V / read-back 4.2 V within the example's 4.2–4.3 V bounds.
It is a targeted acceptance window for this observed read-back, not a claim about
the hardware trip threshold or every firmware version's rounding algorithm.
The [2281S datasheet](https://download.tek.com/datasheet/1KW-60206-0_2281S_Datasheet_050119.pdf),
page 8, lists OVP resolution of 125 mV and setting accuracy ±(0.25% + 0.25 V).
Thus a 4.2 V read-back does not guarantee a trip at precisely 4.2 V, nor does a
successful read-back establish sufficient charger headroom. Verify behaviour on
the bench. Other defaults remain unchanged, including VOC **±0.005 V**.

Both bundled examples explicitly specify the protection tolerance. Older JSON
files without a tolerance use the displayed defaults; explicitly saved values
are respected. Optional role-level format:

```json
"readback_tolerances": {"voltage_protection": 0.05, "voc": 0.005}
```

Only bounded numerical actions accept tolerances. Values must be finite,
nonnegative numbers (zero means exact numerical equality); unknown actions,
boolean values, and tolerances beyond the device's numerical span are rejected.
The executed `sequence.json` records effective tolerances including defaults.
Each successful numerical command event records `requested`, `readback`, signed
`difference`, `tolerance`, `allowed_min` and `allowed_max`. Failures display the
requested/actual values and acceptance window. An instrument-reported SCPI error
still fails the command regardless of numerical tolerance.

#### Per-step overrides

The **Sequence** table also has **Read-back ±**. Leave it **blank** to inherit the
instrument setting's default from Setup. Enter a number to override it **for that
step only**; `0` means exact numerical equality. Overrides move with their steps
and survive save/load. They do not modify subsequent steps or relax test bounds.
Output, method and hold steps must leave this column blank.

In JSON, use optional `"readback_tolerance": 0.05` in the step object. The
protection-setting step in both bundled examples explicitly uses `0.05` V.
Effective tolerance and `tolerance_source` (`step` or `instrument`) are recorded
in command-complete events, including the requested value and actual read-back.
The executed script preserves every step override as well as effective instrument
defaults. Existing scripts without this field continue to load.

The bundled sequences allow OWON current limits up to **3 A** and set 3 A before
external output enable and on source recovery. Saved custom files retain their
own bounds and steps: reload an updated example, or set the source current-limit
maximum to 3 A in Setup and edit the startup/recovery steps to 3 A.
