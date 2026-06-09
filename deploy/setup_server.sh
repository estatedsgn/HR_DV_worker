#!/usr/bin/env bash
#
# HR_DV_worker — first-time server provisioning (Ubuntu 22.04 / 24.04).
#
# Run as root on a fresh VPS:
#     bash deploy/setup_server.sh
#
# Idempotent: safe to re-run. It installs system packages, Docker, Tailscale,
# Node.js + Claude Code, creates the `hrdv` service user, and prepares the app
# directory. It does NOT start the worker — that happens in deploy/README.md
# after you copy the .env and run migrations.
#
set -euo pipefail

APP_USER="hrdv"
APP_DIR="/opt/HR_DV_worker"
REPO_URL="${REPO_URL:-}"      # optional: export REPO_URL=git@github.com:you/HR_DV_worker.git
NODE_MAJOR="22"

log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/setup_server.sh" >&2
  exit 1
fi

# --- 1. Base packages -------------------------------------------------------
log "Installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y \
  ca-certificates curl gnupg git ufw fail2ban tzdata \
  python3 python3-venv python3-pip build-essential

# --- 2. Service user --------------------------------------------------------
log "Creating service user '$APP_USER'"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash "$APP_USER"
fi

# --- 3. Docker (for Postgres) ----------------------------------------------
log "Installing Docker engine + compose plugin"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
systemctl enable --now docker
usermod -aG docker "$APP_USER"

# --- 4. Tailscale (private access from your phone/PC) ----------------------
log "Installing Tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
echo "   -> After this script, run:  tailscale up   (then authenticate in the browser link)"

# --- 5. Node.js + Claude Code ----------------------------------------------
log "Installing Node.js ${NODE_MAJOR}.x + Claude Code"
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
  apt-get install -y nodejs
fi
npm install -g @anthropic-ai/claude-code

# --- 6. App directory + clone ----------------------------------------------
log "Preparing app directory at $APP_DIR"
mkdir -p "$APP_DIR"
if [[ -n "$REPO_URL" && ! -d "$APP_DIR/.git" ]]; then
  git clone "$REPO_URL" "$APP_DIR"
fi
mkdir -p "$APP_DIR/runtime_logs"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# --- 7. Firewall ------------------------------------------------------------
log "Configuring firewall (SSH only on public iface; rely on Tailscale for the rest)"
ufw allow OpenSSH || true
ufw --force enable

log "Base provisioning done."
cat <<'NEXT'

Next steps (see deploy/README.md for detail):
  1. tailscale up                       # authenticate this server into your tailnet
  2. Copy your .env into /opt/HR_DV_worker/.env  (NEVER commit it)
  3. cd /opt/HR_DV_worker && bash deploy/deploy_app.sh   # venv, deps, postgres, migrations
  4. Install + enable systemd services (deploy/README.md step 5)
  5. su - hrdv ; cd /opt/HR_DV_worker ; tmux new -s claude ; claude   # log Claude in
NEXT
