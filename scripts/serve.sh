#!/usr/bin/env bash
# Start the web app on http://127.0.0.1:8000
#
# Reload is on: a long-running server that silently keeps the old code is easy
# to mistake for a broken fix, so the process restarts when a file changes.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

if ! curl -sf -m 3 http://localhost:8002/status >/dev/null 2>&1; then
  echo "note: Valhalla is not running, walk times will be straight-line estimates."
  echo "      start it with scripts/setup_routing.sh"
fi

exec .venv/bin/python -m uvicorn app.web:app \
  --host 127.0.0.1 --port "${PORT:-8000}" --reload "$@"