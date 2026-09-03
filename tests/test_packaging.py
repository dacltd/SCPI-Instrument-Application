"""Tests for the packaged entry point and Windows release bundle."""

import hashlib
import json
import zipfile

from dmm_app.main import main
from scripts.create_release_bundle import create_bundle


def test_smoke_mode_constructs_gui_without_discovering_hardware() -> None:
    assert main(["--smoke-test"]) == 0


def test_create_bundle_adds_metadata_and_checksum(tmp_path) -> None:
    dist_dir = tmp_path / "dist" / "SCPI Lab Instrument App"
    dist_dir.mkdir(parents=True)
    (dist_dir / "SCPI Lab Instrument App.exe").write_bytes(b"test executable")

    output_dir = tmp_path / "release"
    archive, checksum, metadata = create_bundle(
        dist_dir=dist_dir,
        output_dir=output_dir,
        version="0.1.0",
        commit="abc123",
        run_number="42",
    )

    expected_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert checksum.read_text(encoding="ascii") == f"{expected_digest}  {archive.name}\n"

    build_info = json.loads(metadata.read_text(encoding="utf-8"))
    assert build_info["version"] == "0.1.0"
    assert build_info["source_commit"] == "abc123"
    assert build_info["github_run_number"] == "42"

    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
    root = "SCPI-Lab-Instrument-App-0.1.0-windows-x64"
    assert f"{root}/SCPI Lab Instrument App.exe" in names
    assert f"{root}/build-info.json" in names
