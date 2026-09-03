# SCPI Lab Instrument App

A PySide6 desktop application for monitoring up to four SCPI, PicoSDK, or raw
serial devices and recording their measurements on one shared timeline.

## Download the Windows application

The Windows build is produced by GitHub Actions and does not require Python on
the target PC.

1. Open the repository's **Actions** page.
2. Select the latest successful **Windows test and application build** run.
3. Download the `SCPI-Lab-Instrument-App-<version>-windows-x64` artifact.
4. Extract the downloaded artifact, then extract the versioned application ZIP
   inside it.
5. Run `SCPI Lab Instrument App.exe` from the extracted application folder.

Windows may show a SmartScreen warning because the executable is not
code-signed. Use **More info** and **Run anyway** only when the download came
from this repository's successful workflow run.

For USB VISA instruments, the bundle includes the pure-Python PyVISA backend,
PyUSB, and libusb. The Windows device must still have a compatible USB driver.
An instrument vendor's VISA runtime (for example, NI-VISA) can also be used.

To use a PicoLog USB TC-08, install Pico Technology's current **64-bit
PicoSDK** on the Windows PC before starting the app. The application uses the
installed `usbtc08.dll`; the Pico driver is not redistributed in the GitHub
artifact. PicoSDK is available from the official
[Pico Technology downloads page](https://www.picotech.com/downloads).
For a 64-bit installation, the DLL is normally at
`C:\Program Files\Pico Technology\SDK\lib\usbtc08.dll`. A DLL under
`C:\Program Files (x86)` is normally the 32-bit SDK and cannot be loaded by
this 64-bit application.

## Run from source

Python 3.12 is required.

```bash
uv sync --extra dev
uv run python -m dmm_app.main
```

See [the user guide](docs/UserGuide.md) for instrument setup and operation.
