#!/usr/bin/env bash
# Install systemd timer for the weekly OpsMemory restore-check.
# Idempotent — re-runs replace any existing unit files and reload systemd.
#
# Cadence: Sunday at 03:05 America/Phoenix per Codex Chunk 1.5 spec
# (after the daily backup at 02:17, so each weekly check sees the
# freshly-written Saturday-night dump).
#
# On Spark #1 (no GPG private key by design), the restore-check runs in
# `encrypted-structure-only` mode against the latest .dump.gpg file.
# That's still useful — it catches dump corruption / GPG envelope damage
# even without doing a full pg_restore.
#
# Full plaintext-restore verification is the operator drill described
# in docs/07-gpg-key-management.md and runs from the laptop or Spark #2.

set -euo pipefail

PWSH=$(command -v pwsh 2>/dev/null || true)
if [[ -z "$PWSH" && -x /snap/bin/pwsh ]]; then
  PWSH=/snap/bin/pwsh
fi
if [[ -z "$PWSH" ]]; then
  echo "ERROR: pwsh not found. Install with: sudo snap install powershell --classic" >&2
  exit 1
fi

REPO_DIR=${REPO_DIR:-/opt/opsmemory}
WRAPPER="${REPO_DIR}/scripts/run_restore_check.sh"
ENV_FILE="${REPO_DIR}/.env"

if [[ ! -f "$WRAPPER" ]]; then
  echo "ERROR: restore-check wrapper not found at $WRAPPER" >&2
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
SERVICE_FILE="${UNIT_DIR}/opsmemory-restore-check.service"
TIMER_FILE="${UNIT_DIR}/opsmemory-restore-check.timer"

echo "Writing $SERVICE_FILE"
sudo tee "$SERVICE_FILE" >/dev/null <<UNIT
[Unit]
Description=OpsMemory weekly restore-check
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=$ENV_FILE
# Same flock as the daily backup — won't overlap with a fresh backup.
ExecStartPre=/usr/bin/python3 ${REPO_DIR}/scripts/validate_env.py --quiet
ExecStart=$WRAPPER
User=opsmemory
Group=opsmemory
StandardOutput=journal
StandardError=journal
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/var/backups/opsmemory /var/lib/opsmemory
UNIT

echo "Writing $TIMER_FILE"
sudo tee "$TIMER_FILE" >/dev/null <<UNIT
[Unit]
Description=OpsMemory weekly restore-check, Sunday 03:05 America/Phoenix

[Timer]
OnCalendar=Sun *-*-* 03:05:00 America/Phoenix
Persistent=true
RandomizedDelaySec=120
Unit=opsmemory-restore-check.service

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now opsmemory-restore-check.timer

echo "---"
sudo systemctl status opsmemory-restore-check.timer --no-pager | head -15
echo "---"
systemctl list-timers opsmemory-restore-check.timer --no-pager
