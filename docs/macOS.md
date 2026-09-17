# macOS application

Launch **SCPI Lab Instrument.app** from your user Applications folder (`~/Applications`). You can drag the app into the Dock for quick access.

The standalone app includes Python, Qt, PyVISA, PyUSB and libusb. Running it does not require a terminal, an activated virtual environment, or Homebrew. It includes the PicoLog USB TC-08, Keithley 2281S and GSMIV power-capture profiles. Physical USB communication still needs verification with your connected instruments.

Startup errors are recorded in `~/Library/Logs/SCPI Lab Instrument/startup.log`.

## PicoLog USB TC-08

Select **PicoLog USB TC-08** in any instrument panel. The same channel setup,
thermocouple types, temperature units, mains rejection, snapshot, plotting and
CSV logging controls are available on macOS and Windows.

Install [PicoSDK for macOS](https://www.picotech.com/downloads/release-panel/tc-08)
with the architecture matching this app: **ARM64** for an Apple Silicon build,
or **x64** for an Intel build. Pico's SDK download requires its registration form.
The driver is installed separately and is not included in our app bundle.

The app searches the system and user `Library/Frameworks/PicoSDK.framework`
locations automatically, including `Libraries/libusbtc08/libusbtc08.dylib`.
It can also use a compatible driver inside `/Applications/PicoLog.app` or
`~/Applications/PicoLog.app`. The older Intel-only PicoLog driver cannot load
in an Apple Silicon app; install the ARM64 SDK in that case. No terminal
library-path setup is needed for a standard installation. For a custom SDK
location, `PICO_SDK_PATH` can point to its framework or library directory.

Connect the logger by USB, close PicoLog if it has the device open, configure
the desired thermocouple channels, then use **Connect** and **Snapshot**.
Driver loading happens only when connecting a TC-08; other instruments work
without PicoSDK installed.

## Build an updated app

Build on the Mac architecture you want to run on. The bundle targets macOS 13 or later and uses local ad-hoc signing. Distribution to other Macs would require a separate signing and notarization workflow.

From the project folder, using Python 3.12:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-macos-build.txt
brew install libusb
zsh scripts/build_macos.sh
```

The output is `dist/SCPI Lab Instrument.app`. Quit the installed app, then copy the new bundle into `~/Applications`, replacing the previous copy.

`SCPI_BUILD_PYTHON`, `SCPI_DIST_DIR` and `SCPI_BUILD_DIR` optionally override the interpreter and output directories. `SCPI_LIBUSB_PATH` can select a different libusb dylib; its installation must include the `COPYING` licence in the parent directory of `lib`.

## Check the bundle

```sh
codesign --verify --deep --strict 'dist/SCPI Lab Instrument.app'
QT_QPA_PLATFORM=offscreen 'dist/SCPI Lab Instrument.app/Contents/MacOS/SCPI Lab Instrument' --smoke-test /tmp/scpi-smoke.json
cat /tmp/scpi-smoke.json
```

The smoke test opens the main window offscreen, loads the bundled USB backend and PyVISA Python backend, checks that the capture profiles are present, writes a JSON report, and exits. It does not verify communication with a physical instrument. Successful fields are `frozen`, `usb_backend_loaded`, `window_visible`, `keithley_profile` and `gsmiv_profile`, all `true`. `overview_tab` is `Overview`, and `instrument_pages` is 2.

The smoke report also checks `tc08_profile`, eight `tc08_channels`, and selectable
`tc08_measurements`. `tc08_driver_loaded` reports whether the separately installed
Pico driver loads; `tc08_driver_error` explains missing or incompatible drivers.
The smoke test never opens a TC-08 or starts a conversion.
