"""PyInstaller entry point for the Windows desktop application."""

from dmm_app.main import main


if __name__ == "__main__":
    raise SystemExit(main())
