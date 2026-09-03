# Windows packaging

GitHub Actions builds the application on a Windows runner with PyInstaller and
`packaging/scpi-lab-instrument-app.spec`.

The result is a folder-based application. The workflow constructs the complete
GUI in an offscreen smoke test, adds `build-info.json`, creates a versioned ZIP
and SHA-256 checksum, and uploads them as one GitHub Actions artifact.

The target Windows PC does not require Python or a source checkout.
