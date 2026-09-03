# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


project_root = Path(SPECPATH).parent
hidden_imports = sorted(
    set(
        collect_submodules(
            "pyvisa_py",
            filter=lambda name: not name.startswith("pyvisa_py.testsuite"),
        )
        + collect_submodules("libusb_package")
        + collect_submodules("serial")
        + collect_submodules("usb")
    )
)
package_data = collect_data_files("pyvisa") + collect_data_files("pyvisa_py")
package_binaries = collect_dynamic_libs("libusb_package")

application_analysis = Analysis(
    [str(project_root / "scripts" / "windows_entry.py")],
    pathex=[str(project_root)],
    binaries=package_binaries,
    datas=package_data,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
python_archive = PYZ(application_analysis.pure)

application_executable = EXE(
    python_archive,
    application_analysis.scripts,
    [],
    exclude_binaries=True,
    name="SCPI Lab Instrument App",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

bundle = COLLECT(
    application_executable,
    application_analysis.binaries,
    application_analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SCPI Lab Instrument App",
)
