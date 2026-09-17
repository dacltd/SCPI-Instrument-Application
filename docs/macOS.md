# macOS application

Launch **SCPI Lab Instrument.app** from your user Applications folder (`~/Applications`). You can drag the app into the Dock for quick access.

The standalone app includes Python, Qt, PyVISA, PyUSB and libusb. Running it does not require a terminal, an activated virtual environment, or Homebrew. It includes the Keithley 2281S and GSMIV power-capture profiles. Physical USB communication still needs verification with your connected instruments.

Startup errors are recorded in `~/Library/Logs/SCPI Lab Instrument/startup.log`.

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

The smoke test opens the main window offscreen, loads the bundled USB backend and PyVISA Python backend, checks that the capture profiles are present, writes a JSON report, and exits. It does not verify communication with a physical instrument. Successful fields are `frozen`, `usb_backend_loaded`, `window_visible`, `keithley_profile` and `gsmiv_profile`, all `true`, with four panels.
