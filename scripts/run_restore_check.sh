#!/usr/bin/env bash
# Locked wrapper around the OpsMemory restore-check script.
# Acquires flock on /var/lib/opsmemory/backup/.lock before invoking pwsh.
#
# Restore-check waits up to 60 seconds for the lock (so it queues behind a
# concurrent backup rather than failing). If the lock is still held after
# 60s, exits 11 — likely a stuck backup, deserves operator attention.
#
# Exit codes:
#   11  lock not acquired within 60s (another job stuck, investigate)
#   *   propagated from the restore-check script

set -euo pipefail

LOCK_DIR=${LOCK_DIR:-/var/lib/opsmemory/backup}
LOCK_FILE="${LOCK_DIR}/.lock"
mkdir -p "$LOCK_DIR"

PWSH=$(command -v pwsh 2>/dev/null || true)
if [[ -z "$PWSH" && -x /snap/bin/pwsh ]]; then
  PWSH=/snap/bin/pwsh
fi
if [[ -z "$PWSH" ]]; then
  echo "ERROR: pwsh not found" >&2
  exit 1
fi

REPO_DIR=${REPO_DIR:-/opt/opsmemory}
SCRIPT="${REPO_DIR}/scripts/restore_check.ps1"
if [[ ! -f "$SCRIPT" ]]; then
  echo "ERROR: restore-check script not found: $SCRIPT" >&2
  exit 1
fi

exec 200>"$LOCK_FILE"
if ! flock -w 60 200; then
  echo "ERROR: lock not acquired within 60s; backup may be stuck (lock=$LOCK_FILE)" >&2
  exit 11
fi

exec "$PWSH" -File "$SCRIPT" "$@"
