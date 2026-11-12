#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
python_bin="${PYTHON_BIN:-$project_root/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then python_bin="$project_root/.tools/dpt-venv/bin/python"; fi
if [[ ! -x "$python_bin" ]]; then python_bin=python3; fi
(sleep 2; open http://localhost:4387) &
exec "$python_bin" -m app.server
