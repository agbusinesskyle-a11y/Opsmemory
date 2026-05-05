#!/usr/bin/env bash
# Install systemd timer for the daily OpsMemory backup.
# Idempotent — re-runs replace any existing unit files and reload systemd.
#
# Cadence: daily at 02:17 America/Phoenix per Codex Chunk 1 spec.
# Service runs as the unprivileged 'opsmemory' user (must already exist).
# Weekly automated restore-check timer is deferred to Chunk 1.5.

set -euo pipefail

# Locate pwsh (snap install puts it under /snap/bin which isn't always on PATH).
PWSH=$(command -v pwsh 2>/dev/null || true)
if [[ -z "$PWSH" && -x /snap/bin/pwsh ]]; then
  PWSH=/snap/bin/pwsh
fi
if [[ -z "$PWSH" ]]; then
  echo "ERROR: pwsh not found. Install with: sudo snap install powershell --classic" >&2
  exit 1
fi

REPO_DIR=${REPO_DIR:-/opt/opsmemory}
WRAPPER="${REPO_DIR}/scripts/run_backup.sh"
ENV_FILE="${REPO_DIR}/.env"

if [[ ! -f "$WRAPPER" ]]; then
  echo "ERROR: backup wrapper not found at $WRAPPER" >&2
  exit 1
fi
if [[ ! -x "$WRAPPER" ]]; then
  chmod +x "$WRAPPER"
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: env file not found at $ENV_FILE" >&2
  exit 1
fi
if ! id opsmemory >/dev/null 2>&1; then
  echo "ERROR: 'opsmemory' system user not found. Create it first." >&2
  exit 1
fi

UNIT_DIR=/etc/systemd/system
SERVICE_FILE="${UNIT_DIR}/opsmemory-backup.service"
TIMER_FILE="${UNIT_DIR}/opsmemory-backup.timer"

echo "Writing $SERVICE_FILE"
sudo tee "$SERVICE_FILE" >/dev/null <<UNIT
[Unit]
Description=OpsMemory action_tracker daily backup
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=$ENV_FILE
# Preflight: validate .env before doing any work. ExecStartPre exits non-zero
# if env is invalid, which prevents the backup from running with a broken
# config (e.g. placeholder DSN, missing CF_ACCESS_AUD, dev-mode flags in
# production).
ExecStartPre=/usr/bin/python3 ${REPO_DIR}/scripts/validate_env.py --quiet
# run_backup.sh acquires flock on /var/lib/opsmemory/backup/.lock before
# invoking pwsh, preventing concurrent backups or backup/restore-check
# overlap (which could otherwise grab a half-written dump).
ExecStart=$WRAPPER
User=opsmemory
Group=opsmemory
StandardOutput=journal
StandardError=journal
# Modest hardening — this is a dump-and-rsync job, no need for write
# access outside its own dirs.
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/var/backups/opsmemory /var/lib/opsmemory
# Note: NoNewPrivileges=true is intentionally OFF because snap-confine
# (used by snap-installed pwsh) requires cap_dac_override at exec time.
# Setting it makes snap pwsh exit immediately with "snap-confine is
# packaged without necessary permissions". When pwsh is installed
# from the Microsoft apt repo instead of snap, NoNewPrivileges=true
# can be re-enabled.
UNIT

echo "Writing $TIMER_FILE"
sudo tee "$TIMER_FILE" >/dev/null <<UNIT
[Unit]
Description=OpsMemory daily backup at 02:17 America/Phoenix

[Timer]
OnCalendar=*-*-* 02:17:00 America/Phoenix
Persistent=true
RandomizedDelaySec=120
Unit=opsmemory-backup.service

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now opsmemory-backup.timer

echo "---"
sudo systemctl status opsmemory-backup.timer --no-pager | head -15
echo "---"
systemctl list-timers opsmemory-backup.timer --no-pager
