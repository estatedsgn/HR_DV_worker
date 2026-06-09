#!/usr/bin/env bash
#
# HR_DV_worker — application bootstrap / update.
#
# Run as the hrdv user (or root) from the app directory AFTER setup_server.sh
# and after placing .env:
#     cd /opt/HR_DV_worker && bash deploy/deploy_app.sh
#
# Idempotent: creates/updates the virtualenv, installs the package, brings up
# Postgres in Docker, and applies Alembic migrations. Safe to re-run on every
# code update (git pull && bash deploy/deploy_app.sh).
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

if [[ ! -f .env ]]; then
  echo "Missing $APP_DIR/.env — copy it over before running (see .env.example)." >&2
  exit 1
fi

# --- 1. Virtualenv + deps ---------------------------------------------------
log "Creating/updating virtualenv"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/pip install -e .

# --- 2. Postgres (Docker) ---------------------------------------------------
log "Starting Postgres (docker compose)"
docker compose up -d postgres

log "Waiting for Postgres to be healthy"
for i in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -U hr_dv_worker -d hr_dv_worker >/dev/null 2>&1; then
    echo "   Postgres is ready."
    break
  fi
  sleep 2
done

# --- 3. Migrations ----------------------------------------------------------
log "Applying Alembic migrations"
./.venv/bin/alembic upgrade head

mkdir -p runtime_logs

log "App bootstrap complete. Manage services with:"
echo "   sudo systemctl restart hrdv-autopilot hrdv-supervisor"
