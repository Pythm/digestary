#!/usr/bin/env bash
# Entrypoint: seed the databases (idempotent), then start the API.
set -e
cd /app

# Seed databases + food catalog (safe to re-run: only seeds if foods is empty)
python init_db.py || echo "WARNING: init_db reported an issue; continuing anyway"

# Start uvicorn
exec python -m uvicorn server:app --host 0.0.0.0 --port 8000
