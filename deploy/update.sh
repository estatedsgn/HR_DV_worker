#!/usr/bin/env bash
#
# HR_DV_worker — pull latest code and restart services.
# Run as hrdv (with sudo rights for systemctl) from the app dir:
#     bash deploy/update.sh
#
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

echo "==> Pulling latest code"
git pull --ff-only

echo "==> Re-running app bootstrap (deps + migrations)"
bash deploy/deploy_app.sh

echo "==> Restarting services"
sudo systemctl restart hrdv-autopilot hrdv-supervisor
sudo systemctl --no-pager --full status hrdv-autopilot hrdv-supervisor | head -n 20
