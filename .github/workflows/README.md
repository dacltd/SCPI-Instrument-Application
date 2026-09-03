# GitHub Actions workflows

`windows-build.yml` runs on pushes to `main`, pull requests, and manual
workflow dispatch.

It installs the locked Python 3.12 environment, lints and tests the source,
builds the folder-based Windows app, smoke-tests the packaged executable, then
uploads a versioned ZIP, SHA-256 checksum, and build metadata.

Download the `SCPI-Lab-Instrument-App-<version>-windows-x64` artifact from a
successful workflow run, extract the application ZIP, and run
`SCPI Lab Instrument App.exe`.
