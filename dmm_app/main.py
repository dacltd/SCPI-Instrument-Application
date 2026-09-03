from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from PySide6.QtWidgets import QApplication

from dmm_app import __version__
from dmm_app.gui import DMMAppWindow


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SCPI Lab Instrument App")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="construct the GUI offscreen and exit without discovering instruments",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def smoke_test_application() -> int:
    """Construct the packaged GUI without hardware access and exit."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = DMMAppWindow(discover_endpoints=False)
    window.close()
    app.processEvents()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.smoke_test:
        return smoke_test_application()

    app = QApplication(sys.argv)
    window = DMMAppWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
