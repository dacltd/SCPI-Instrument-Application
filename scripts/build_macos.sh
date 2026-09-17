#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
cd "$project_dir"
python_bin="${SCPI_BUILD_PYTHON:-$project_dir/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
    print -u2 "Create .venv and install requirements-macos-build.txt first."
    exit 1
fi
"$python_bin" -m PyInstaller --noconfirm \
    --distpath "${SCPI_DIST_DIR:-$project_dir/dist}" \
    --workpath "${SCPI_BUILD_DIR:-$project_dir/build/macos}" \
    packaging/scpi_macos.spec
print "App: ${SCPI_DIST_DIR:-$project_dir/dist}/SCPI Lab Instrument.app"
