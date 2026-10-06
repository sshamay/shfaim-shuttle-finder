#!/bin/sh
set -eu

# Make sure the stops cache exists and is fresh (20h TTL) before serving.
# The server starts either way: a failed fetch just means /api/nearest
# answers 503 with instructions until the next boot refreshes the data.
python scripts/fetch_lines.py --api-only || {
    echo "WARN: could not refresh stops cache; nearest-stop lookups will 503" >&2
}

PORT="${PORT:-8000}"
exec uvicorn app.web:app --host 0.0.0.0 --port "$PORT"