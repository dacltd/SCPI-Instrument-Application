"""Finder entry point, bundled USB loader, and packaging smoke test."""
from __future__ import annotations

from contextlib import closing
import json
import logging
import os
from pathlib import Path
import sys
import traceback


def main() -> int:
    log_dir = Path(os.environ.get("SCPI_LAB_LOG_DIR", str(Path.home() / "Library/Logs/SCPI Lab Instrument")))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "startup.log"
    # A Finder-launched windowed executable has no useful stderr destination.
    with log_path.open("a", buffering=1) as log:
        sys.stdout = sys.stderr = log
        logging.basicConfig(stream=log, level=logging.WARNING)
        smoke_test = len(sys.argv) == 3 and sys.argv[1] == "--smoke-test"
        try:
            import usb.backend.libusb1

            bundled = Path(getattr(sys, "_MEIPASS", "")) / "libusb-1.0.dylib"
            backend = usb.backend.libusb1.get_backend(
                find_library=(lambda _name: str(bundled)) if getattr(sys, "frozen", False) else None
            )
            if backend is None:
                raise RuntimeError("The USB support library could not be loaded.")
            # PyUSB caches this backend; later PyVISA USB queries use the bundled
            # library without relying on Homebrew or Finder's PATH.
            from PySide6.QtCore import QTimer
            from PySide6.QtWidgets import QApplication
            from dmm_app import __version__
            from dmm_app.commands import INSTRUMENT_PROFILES
            from dmm_app.gui import DMMAppWindow
            from dmm_app.models import InstrumentType
            import pyvisa

            app = QApplication(sys.argv[:1] if smoke_test else sys.argv)
            app.setApplicationVersion(__version__)
            app.setApplicationName("SCPI Lab Instrument")
            app.setOrganizationName("DAC")
            window = DMMAppWindow()
            window.show()
            if smoke_test:
                panel = window._panels[0]
                panel._instrument_combo.setCurrentText(InstrumentType.PICOLOG_TC08.value)
                tc08_settings = panel._tc08_settings_from_controls()
                tc08_measurements = panel._build_tc08_measurements()
                from dmm_app.pico_tc08 import PicoTC08Library, PicoTC08Error
                try:
                    PicoTC08Library()  # Load/bind the driver without opening hardware.
                    tc08_driver_loaded, tc08_driver_error = True, ""
                except PicoTC08Error as exc:
                    tc08_driver_loaded, tc08_driver_error = False, str(exc)
                with closing(pyvisa.ResourceManager("@py")) as manager:
                    report = {
                        "version": __version__,
                        "tc08_profile": InstrumentType.PICOLOG_TC08 in INSTRUMENT_PROFILES,
                        "tc08_channels": len(tc08_settings.channel_types),
                        "tc08_measurements": len(tc08_measurements),
                        "tc08_driver_loaded": tc08_driver_loaded,
                        "tc08_driver_error": tc08_driver_error,
                        "frozen": bool(getattr(sys, "frozen", False)),
                        "usb_library": str(bundled),
                        "usb_backend_loaded": backend is not None,
                        "visa_backend": type(manager.visalib).__module__,
                        "window_visible": window.isVisible(),
                        "panels": len(window._panels),
                        "keithley_profile": InstrumentType.KEITHLEY_2281S in INSTRUMENT_PROFILES,
                        "gsmiv_profile": InstrumentType.GSMIV_POWER in INSTRUMENT_PROFILES,
                    }
                Path(sys.argv[2]).write_text(json.dumps(report, indent=2) + "\n")
                QTimer.singleShot(250, app.quit)
            return app.exec()
        except Exception:
            traceback.print_exc(file=log)
            if not smoke_test:
                try:
                    from PySide6.QtWidgets import QApplication, QMessageBox
                    app = QApplication.instance() or QApplication(sys.argv[:1])
                    QMessageBox.critical(None, "SCPI Lab Instrument could not start",
                                         f"Details were saved to:\n{log_path}")
                except Exception:
                    pass
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
