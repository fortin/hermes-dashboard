#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT/backend${PYTHONPATH:+:$PYTHONPATH}"

if [[ ! -d "$ROOT/backend/.venv" ]]; then
  python3 -m venv "$ROOT/backend/.venv"
  "$ROOT/backend/.venv/bin/pip" install -r "$ROOT/backend/requirements.txt"
fi

exec "$ROOT/backend/.venv/bin/python" -m uvicorn app.main:app \
  --app-dir "$ROOT/backend" \
  --host "${DASHBOARD_HOST:-127.0.0.1}" \
  --port "${DASHBOARD_PORT:-8787}"
