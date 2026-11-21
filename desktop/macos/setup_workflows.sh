#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if ! command -v uv >/dev/null 2>&1; then
  echo 'Install uv first (https://docs.astral.sh/uv/), then rerun this script.' >&2
  exit 1
fi
CPU="$(uname -m)"
if [[ "$CPU" == arm64 ]]; then CPU=aarch64; fi
RUNTIME="cpython-3.12-macos-${CPU}-none"
if [[ ! -x "$ROOT/.workflows-venv/bin/python" ]]; then
  uv python install "$RUNTIME"
  uv venv --python "$RUNTIME" "$ROOT/.workflows-venv"
fi
uv pip install --python "$ROOT/.workflows-venv/bin/python" -r "$ROOT/workflows/requirements.lock.txt"
"$ROOT/.workflows-venv/bin/python" -c 'import mistralai.workflows; print("Workflows runtime ready")'
