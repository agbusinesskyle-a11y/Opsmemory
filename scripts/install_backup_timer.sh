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
SCRIPT="${REPO_DIR}/scripts/backup_action_tracker.ps1"
ENV_FILE="${REPO_DIR}/.env"

if [[ ! -f "$SCRIPT" ]]; then
  echo "ERROR: backup script not found at $SCRIPT" >&2
  exit 1
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
ExecStart=$PWSH $SCRIPT
User=opsmemory
Group=opsmemory
StandardOutput=journal
StandardError=journal
# Modest hardening — this is a dump-and-rsync job, no need for write
# access outside its own dirs.
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/var/backups/opsmemory /var/lib/opsmemory
NoNewPrivileges=true
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
