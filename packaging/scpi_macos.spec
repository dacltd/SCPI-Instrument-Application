# Build with: python -m PyInstaller packaging/scpi_macos.spec
import os
import platform
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

root = Path(SPECPATH).parent
candidates = [Path(os.environ["SCPI_LIBUSB_PATH"])] if os.environ.get("SCPI_LIBUSB_PATH") else [
    Path("/opt/homebrew/lib/libusb-1.0.dylib"),
    Path("/usr/local/lib/libusb-1.0.dylib"),
]
libusb = next((path for path in candidates if path.is_file()), None)
if libusb is None:
    raise SystemExit("Install libusb with 'brew install libusb', or set SCPI_LIBUSB_PATH.")

# Keep the LGPL licence with the bundled libusb binary.
licence = libusb.resolve().parent.parent / "COPYING"
if not licence.is_file():
    raise SystemExit(f"Missing libusb licence: {licence}")
datas = [(str(root / "docs"), "docs"), (str(licence), "licenses/libusb")]
for package in ("pyvisa", "pyvisa-py", "pyusb", "pyserial", "PySide6", "shiboken6"):
    datas += copy_metadata(package)

analysis = Analysis(
    [str(root / "packaging/macos_entry.py")],
    pathex=[str(root)],
    binaries=[(str(libusb), ".")],
    datas=datas,
    hiddenimports=collect_submodules("pyvisa_py") + collect_submodules("usb.backend"),
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True,
    name="SCPI Lab Instrument", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, argv_emulation=False,
    target_arch=platform.machine(), codesign_identity=None, entitlements_file=None,
)
collection = COLLECT(exe, analysis.binaries, analysis.datas,
                     strip=False, upx=False, name="SCPI Lab Instrument")
app = BUNDLE(
    collection, name="SCPI Lab Instrument.app",
    bundle_identifier="uk.co.dacltd.scpilab",
    info_plist={
        "CFBundleDisplayName": "SCPI Lab Instrument",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
        "NSLocalNetworkUsageDescription": "Discover and connect to laboratory instruments on your network.",
    },
)
