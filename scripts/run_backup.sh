#!/usr/bin/env bash
# Locked wrapper around the OpsMemory backup script.
# Acquires flock on /var/lib/opsmemory/backup/.lock before invoking pwsh.
# Used by:
#   - systemd timer (ExecStart points here)
#   - manual operator runs from the runbook
#
# Exit codes:
#   11  another backup or restore-check holds the lock (non-blocking refusal)
#   *   propagated from the underlying backup script

set -euo pipefail

LOCK_DIR=${LOCK_DIR:-/var/lib/opsmemory/backup}
LOCK_FILE="${LOCK_DIR}/.lock"
mkdir -p "$LOCK_DIR"

# Locate pwsh (snap install isn't always on PATH for systemd users).
PWSH=$(command -v pwsh 2>/dev/null || true)
if [[ -z "$PWSH" && -x /snap/bin/pwsh ]]; then
  PWSH=/snap/bin/pwsh
fi
if [[ -z "$PWSH" ]]; then
  echo "ERROR: pwsh not found" >&2
  exit 1
fi

REPO_DIR=${REPO_DIR:-/opt/opsmemory}
SCRIPT="${REPO_DIR}/scripts/backup_action_tracker.ps1"
if [[ ! -f "$SCRIPT" ]]; then
  echo "ERROR: backup script not found: $SCRIPT" >&2
  exit 1
fi

# fd 200 for the lock. Non-blocking — if a backup is already running, refuse.
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
  echo "ERROR: another OpsMemory backup or restore-check is running (lock=$LOCK_FILE)" >&2
  exit 11
fi

# Lock auto-releases when fd 200 closes at script exit.
exec "$PWSH" "$SCRIPT" "$@"
