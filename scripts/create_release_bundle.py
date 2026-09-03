"""Create a versioned Windows application ZIP and checksum."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path


def create_bundle(
    *,
    dist_dir: Path,
    output_dir: Path,
    version: str,
    commit: str,
    run_number: str,
) -> tuple[Path, Path, Path]:
    """Create the release ZIP, checksum, and build metadata."""
    if not dist_dir.is_dir():
        raise ValueError(f"Distribution directory does not exist: {dist_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_name = f"SCPI-Lab-Instrument-App-{version}-windows-x64"
    staging_root = output_dir / bundle_name
    if staging_root.exists():
        shutil.rmtree(staging_root)
    shutil.copytree(dist_dir, staging_root)

    build_info = {
        "application": "SCPI Lab Instrument App",
        "version": version,
        "source_commit": commit,
        "github_run_number": run_number,
        "platform": "windows-x64",
        "built_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    embedded_info_path = staging_root / "build-info.json"
    embedded_info_path.write_text(
        json.dumps(build_info, indent=2) + "\n",
        encoding="utf-8",
    )

    metadata_path = output_dir / f"{bundle_name}-build-info.json"
    shutil.copy2(embedded_info_path, metadata_path)

    archive_path = Path(
        shutil.make_archive(
            str(output_dir / bundle_name),
            "zip",
            root_dir=output_dir,
            base_dir=bundle_name,
        )
    )
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path = output_dir / f"{archive_path.name}.sha256"
    checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="ascii")

    shutil.rmtree(staging_root)
    return archive_path, checksum_path, metadata_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--run-number", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    create_bundle(
        dist_dir=args.dist_dir,
        output_dir=args.output_dir,
        version=args.version,
        commit=args.commit,
        run_number=args.run_number,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
