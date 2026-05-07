#!/usr/bin/env bash
# OpsMemory deploy helper for chunks 10-13.
#
# Single-script orchestrator that handles:
#   1. Pre-flight (git state, expected files present, chunk tags)
#   2. rsync code from this laptop -> Spark
#   3. Validate required env vars on Spark (with per-chunk gating)
#   4. Run database migrations (0013 -> 0018)
#   5. docker compose up -d --build
#   6. Smoke-check /readyz
#
# Run on Kyle's laptop from the C:\opsmemory checkout in git-bash:
#
#   ./scripts/deploy_chunks_10_through_13.sh
#
# Override defaults via env (see CONFIGURATION below). Each phase
# can be skipped independently for partial deploys, e.g.
#   SKIP_RSYNC=1 ./scripts/deploy_chunks_10_through_13.sh
#   ONLY_PHASE=migrations ./scripts/deploy_chunks_10_through_13.sh
#
# Idempotent. Re-running is safe — rsync is incremental, migrations
# use IF NOT EXISTS, and docker compose up -d --build rebuilds in
# place.
#
# Exit codes:
#   0   all phases passed
#   1   configuration / pre-flight error (no remote changes made)
#   2   remote phase failed mid-deploy (deploy is partial; inspect)

set -euo pipefail

# =============================================================================
# CONFIGURATION
# =============================================================================
# All overridable via env. Defaults match the conventions in
# docs/05-chunk1-runbook.md and the chunk-10..13 runbooks.

: "${SPARK_HOST:=spark}"                          # ssh alias or user@host
: "${SPARK_REPO_DIR:=/opt/opsmemory}"             # remote checkout dir
: "${SPARK_COMPOSE_DIR:=$SPARK_REPO_DIR}"         # where docker-compose.yml lives
: "${SPARK_API_BASE:=https://tracker.kyleconway.ai}"  # for /readyz curl
: "${MIGRATIONS_DIR:=api/migrations}"
: "${EXPECTED_TAG_PREFIX:=chunk-1}"               # we expect to be at chunk-13-close or beyond
: "${SKIP_RSYNC:=0}"
: "${SKIP_ENV_CHECK:=0}"
: "${SKIP_MIGRATIONS:=0}"
: "${SKIP_REBUILD:=0}"
: "${SKIP_HEALTHCHECK:=0}"
: "${ONLY_PHASE:=}"                               # preflight|rsync|env|migrations|rebuild|health
: "${RSYNC_DRY_RUN:=0}"
: "${ASSUME_YES:=0}"                              # skip the final confirm prompt

# Per-chunk env checks. The deploy continues even when a chunk's
# env is missing (so a web-push-only operator can ignore Slack)
# but we surface what's missing so the operator can flip on
# features when ready. Set CHUNK_<N>_REQUIRED=1 to escalate
# missing vars from warning to error.
: "${CHUNK_10_REQUIRED:=0}"   # web push + slack DM
: "${CHUNK_11_REQUIRED:=0}"   # weekly Gmail digest
: "${CHUNK_12_REQUIRED:=0}"   # MCP read-only

# =============================================================================
# Helpers
# =============================================================================

LOG_PREFIX="[deploy]"
ANSI_BOLD="$(tput bold 2>/dev/null || true)"
ANSI_RED="$(tput setaf 1 2>/dev/null || true)"
ANSI_GREEN="$(tput setaf 2 2>/dev/null || true)"
ANSI_YELLOW="$(tput setaf 3 2>/dev/null || true)"
ANSI_CYAN="$(tput setaf 6 2>/dev/null || true)"
ANSI_RESET="$(tput sgr0 2>/dev/null || true)"

log()   { printf "%s%s %s%s\n" "$ANSI_CYAN" "$LOG_PREFIX" "$*" "$ANSI_RESET" >&2; }
warn()  { printf "%s%s WARN %s%s\n" "$ANSI_YELLOW" "$LOG_PREFIX" "$*" "$ANSI_RESET" >&2; }
err()   { printf "%s%s ERROR %s%s\n" "$ANSI_RED" "$LOG_PREFIX" "$*" "$ANSI_RESET" >&2; }
ok()    { printf "%s%s OK %s%s\n" "$ANSI_GREEN" "$LOG_PREFIX" "$*" "$ANSI_RESET" >&2; }

phase_active() {
    local name="$1"
    if [[ -n "$ONLY_PHASE" && "$ONLY_PHASE" != "$name" ]]; then
        return 1
    fi
    return 0
}

ssh_remote() {
    # ssh wrapper. Stderr passes through; stdout is captured by
    # caller when needed.
    ssh -o BatchMode=yes "$SPARK_HOST" "$@"
}

confirm() {
    local prompt="$1"
    if [[ "$ASSUME_YES" == "1" ]]; then
        return 0
    fi
    printf "%s%s %s [y/N] %s" "$ANSI_BOLD" "$LOG_PREFIX" "$prompt" "$ANSI_RESET" >&2
    local reply
    read -r reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

# =============================================================================
# PHASE 1 — Pre-flight
# =============================================================================

phase_preflight() {
    phase_active preflight || return 0
    log "Phase 1: pre-flight checks"

    # Confirm we're inside the opsmemory repo.
    if [[ ! -f api/app/main.py || ! -f docker-compose.yml ]]; then
        err "this script must be run from the opsmemory repo root (cwd=$PWD)"
        return 1
    fi

    # Confirm we're on a clean commit so what's deployed matches
    # what's on disk. dirty rsync means partial-shipped state.
    if ! git diff --quiet || ! git diff --cached --quiet; then
        warn "working tree has uncommitted changes — rsync would copy them"
        if ! confirm "continue anyway?"; then
            err "pre-flight aborted by user"
            return 1
        fi
    fi

    # Confirm we're at-or-past chunk-13-close. Detached / non-tag
    # OK; we just want to surface what we're shipping.
    local current_sha
    current_sha=$(git rev-parse HEAD)
    log "current commit: $current_sha"
    local nearest_tag
    nearest_tag=$(git describe --tags --abbrev=0 2>/dev/null || echo "<none>")
    log "nearest tag:    $nearest_tag"
    if [[ "$nearest_tag" != chunk-1[0-3]-close && "$nearest_tag" != "<none>" ]]; then
        warn "deploy expected chunk-10/11/12/13-close, got $nearest_tag"
    fi

    # Confirm the new files actually exist locally (so we don't
    # rsync an empty directory after a bad checkout).
    local missing=()
    local expected=(
        "$MIGRATIONS_DIR/0013_notifications.sql"
        "$MIGRATIONS_DIR/0017_weekly_digest.sql"
        "$MIGRATIONS_DIR/0018_sop_suggestions.sql"
        "api/app/notifications/sender.py"
        "api/app/notifications/slack_sender.py"
        "api/app/notifications/gmail_sender.py"
        "api/app/notifications/weekly_digest.py"
        "api/app/v1_notifications.py"
        "api/app/v1_weekly_digest.py"
        "api/app/v1_sop_suggestions.py"
        "api/app/sop_suggester.py"
        "api/mcp/server.py"
        "api/mcp/sanitize.py"
        "scripts/run_notification_scheduler.py"
        "scripts/run_weekly_digest.py"
        "scripts/run_sop_suggester.py"
    )
    for f in "${expected[@]}"; do
        [[ -f "$f" ]] || missing+=("$f")
    done
    if (( ${#missing[@]} > 0 )); then
        err "missing expected files (rsync would deploy a broken tree):"
        for f in "${missing[@]}"; do
            err "  - $f"
        done
        return 1
    fi
    ok "all chunk 10-13 files present locally"

    # Confirm SSH connectivity early so we fail before doing
    # anything destructive.
    if [[ "$SKIP_RSYNC" != "1" || "$SKIP_ENV_CHECK" != "1" \
        || "$SKIP_MIGRATIONS" != "1" || "$SKIP_REBUILD" != "1" ]]; then
        log "ssh probe: $SPARK_HOST"
        if ! ssh_remote 'echo deploy-ssh-ok' >/dev/null 2>&1; then
            err "ssh to $SPARK_HOST failed (BatchMode). Configure ~/.ssh/config or run ssh-agent first."
            return 1
        fi
        ok "ssh reachable"
    fi
}

# =============================================================================
# PHASE 2 — rsync
# =============================================================================

phase_rsync() {
    [[ "$SKIP_RSYNC" == "1" ]] && { log "skip: rsync"; return 0; }
    phase_active rsync || return 0
    log "Phase 2: rsync $PWD -> $SPARK_HOST:$SPARK_REPO_DIR"

    # Whitelist what gets shipped. Don't rsync .git, .venv, __pycache__, etc.
    local rsync_flags=(-av --delete-excluded
        --exclude '.git'
        --exclude '.venv'
        --exclude '__pycache__'
        --exclude '*.pyc'
        --exclude '.pytest_cache'
        --exclude 'node_modules'
        --exclude '.env'
        --exclude '.env.local'
        --exclude '_*.py'
    )
    if [[ "$RSYNC_DRY_RUN" == "1" ]]; then
        rsync_flags+=(--dry-run)
        warn "RSYNC_DRY_RUN=1 — no remote changes"
    fi

    # NB: trailing slashes matter. ./ source = "this directory's
    # contents"; "$SPARK_REPO_DIR/" target = "into this directory".
    if ! rsync "${rsync_flags[@]}" ./ "$SPARK_HOST:$SPARK_REPO_DIR/"; then
        err "rsync failed"
        return 2
    fi
    ok "rsync complete"
}

# =============================================================================
# PHASE 3 — Env-var check on Spark
# =============================================================================

phase_env_check() {
    [[ "$SKIP_ENV_CHECK" == "1" ]] && { log "skip: env check"; return 0; }
    phase_active env || return 0
    log "Phase 3: env-var check on Spark"

    # Read .env from the Spark side. We don't print values — only
    # which vars are set vs unset.
    local missing_chunk10=()
    local missing_chunk11=()
    local missing_chunk12=()

    local check_script
    read -r -d '' check_script <<'REMOTE_BASH' || true
set -e
cd "$SPARK_REPO_DIR"
if [[ ! -f .env ]]; then
    echo "ENV_FILE_MISSING"
    exit 0
fi
# Source .env in a subshell so we don't pollute parent env.
(
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
    # Chunk 10 — Web Push + Slack DM
    for v in VAPID_PUBLIC_KEY VAPID_PRIVATE_KEY VAPID_SUBJECT \
             N8N_NOTIFICATION_WEBHOOK_URL ; do
        [[ -n "${!v:-}" ]] || echo "C10_MISSING $v"
    done
    # Chunk 11 — weekly Gmail digest
    for v in N8N_GMAIL_DIGEST_WEBHOOK_URL ; do
        [[ -n "${!v:-}" ]] || echo "C11_MISSING $v"
    done
    # Chunk 12 — MCP. The MCP service-account key lives outside
    # the API .env (the MCP server is its own process). We can't
    # enforce it here.
    :
)
REMOTE_BASH

    local check_out
    check_out=$(SPARK_REPO_DIR="$SPARK_REPO_DIR" ssh_remote \
        "SPARK_REPO_DIR=$SPARK_REPO_DIR bash -s" <<<"$check_script" || true)

    if echo "$check_out" | grep -q "ENV_FILE_MISSING"; then
        err "$SPARK_HOST:$SPARK_REPO_DIR/.env is missing — copy .env.example and fill production values"
        return 1
    fi

    while IFS= read -r line; do
        case "$line" in
            "C10_MISSING "*) missing_chunk10+=("${line#C10_MISSING }") ;;
            "C11_MISSING "*) missing_chunk11+=("${line#C11_MISSING }") ;;
            "C12_MISSING "*) missing_chunk12+=("${line#C12_MISSING }") ;;
        esac
    done <<<"$check_out"

    if (( ${#missing_chunk10[@]} > 0 )); then
        warn "Chunk 10 (notifications) env missing: ${missing_chunk10[*]}"
        if [[ "$CHUNK_10_REQUIRED" == "1" ]]; then
            err "CHUNK_10_REQUIRED=1 — fix and re-run"
            return 1
        fi
        warn "  -> notification scheduler will preflight-fail on --send mode"
    else
        ok "Chunk 10 env complete"
    fi

    if (( ${#missing_chunk11[@]} > 0 )); then
        warn "Chunk 11 (Gmail digest) env missing: ${missing_chunk11[*]}"
        if [[ "$CHUNK_11_REQUIRED" == "1" ]]; then
            err "CHUNK_11_REQUIRED=1 — fix and re-run"
            return 1
        fi
        warn "  -> weekly Gmail digest runner will preflight-fail on --send"
    else
        ok "Chunk 11 env complete"
    fi

    log "Chunk 12 (MCP): service-account key is checked at MCP server startup, not here"
}

# =============================================================================
# PHASE 4 — migrations
# =============================================================================

phase_migrations() {
    [[ "$SKIP_MIGRATIONS" == "1" ]] && { log "skip: migrations"; return 0; }
    phase_active migrations || return 0
    log "Phase 4: run migrations"

    # The chunk-1 deploy runs migrations inside the API container
    # (docker compose exec). Mirror that. If the container isn't
    # running yet (first deploy), this fails loudly — operator
    # then runs phase 5 (rebuild) first and re-runs phase 4.
    log "checking compose state"
    if ! ssh_remote "cd '$SPARK_COMPOSE_DIR' && docker compose ps --quiet opsmemory-api 2>/dev/null | grep -q ." ; then
        warn "opsmemory-api container not running — skipping migrations"
        warn "  -> rebuild first, then re-run with ONLY_PHASE=migrations"
        return 0
    fi

    log "applying migrations 0013..0018 (idempotent; existing rows skipped)"
    if ! ssh_remote "cd '$SPARK_COMPOSE_DIR' && docker compose exec -T opsmemory-api python3 scripts/migrate.py" ; then
        err "migrate.py failed"
        return 2
    fi
    ok "migrations complete"
}

# =============================================================================
# PHASE 5 — rebuild
# =============================================================================

phase_rebuild() {
    [[ "$SKIP_REBUILD" == "1" ]] && { log "skip: rebuild"; return 0; }
    phase_active rebuild || return 0
    log "Phase 5: docker compose up -d --build (rebuild + restart)"

    if ! confirm "rebuild + restart the opsmemory-api container on $SPARK_HOST?"; then
        warn "rebuild aborted by user"
        return 0
    fi

    if ! ssh_remote "cd '$SPARK_COMPOSE_DIR' && docker compose up -d --build opsmemory-api" ; then
        err "docker compose up failed"
        return 2
    fi
    ok "rebuild + restart complete"
}

# =============================================================================
# PHASE 6 — healthcheck
# =============================================================================

phase_healthcheck() {
    [[ "$SKIP_HEALTHCHECK" == "1" ]] && { log "skip: healthcheck"; return 0; }
    phase_active health || return 0
    log "Phase 6: /readyz smoke check"

    # Ping /readyz from the laptop side via Cloudflare.
    # Allow a few retries in case the container is still booting.
    local attempt=0
    local max_attempts=12
    local sleep_seconds=5
    while (( attempt < max_attempts )); do
        attempt=$(( attempt + 1 ))
        if curl -sf -m 10 "$SPARK_API_BASE/readyz" >/dev/null; then
            ok "/readyz returned 200 after attempt $attempt"
            return 0
        fi
        log "attempt $attempt/$max_attempts /readyz not yet healthy; sleeping ${sleep_seconds}s"
        sleep "$sleep_seconds"
    done
    err "/readyz still failing after $max_attempts attempts"
    err "  -> ssh $SPARK_HOST 'cd $SPARK_COMPOSE_DIR && docker compose logs --tail=80 opsmemory-api'"
    return 2
}

# =============================================================================
# Main
# =============================================================================

main() {
    log "OpsMemory deploy: chunks 10-13"
    log "  SPARK_HOST=$SPARK_HOST"
    log "  SPARK_REPO_DIR=$SPARK_REPO_DIR"
    log "  ONLY_PHASE=${ONLY_PHASE:-<all>}"
    log "  SKIP_RSYNC=$SKIP_RSYNC SKIP_ENV_CHECK=$SKIP_ENV_CHECK SKIP_MIGRATIONS=$SKIP_MIGRATIONS SKIP_REBUILD=$SKIP_REBUILD SKIP_HEALTHCHECK=$SKIP_HEALTHCHECK"

    phase_preflight        || return 1
    phase_rsync            || return 2
    phase_env_check        || return 1
    phase_migrations       || return 2
    phase_rebuild          || return 2
    phase_healthcheck      || return 2

    ok "deploy complete"
    cat >&2 <<'POSTDEPLOY'

Post-deploy operator steps (run these once per chunk on Spark
when you're ready to flip the feature on):

  Chunk 10 (notifications):
    Visit https://tracker.kyleconway.ai → Settings tab →
      Enable Web Push on this browser. The first push fires
      on the next scheduler tick (default Mon-Fri 7am Phoenix).
    Install the systemd timer per docs/11-notifications-runbook.md
      § One-time setup #4.

  Chunk 11 (weekly Gmail digest):
    Build the n8n webhook per docs/12-weekly-digest-runbook.md
      § One-time setup #2.
    Seed the recipient allowlist via:
      curl -X POST .../v1/weekly_digest/allowlist -d '...'
    Install the Mon-8am-Phoenix timer per § One-time setup #4.

  Chunk 12 (MCP):
    Bootstrap a service account:
      python3 scripts/bootstrap_service_account.py \
        --name opsmemory-mcp --scopes mcp:read
    Wire python -m api.mcp.server into your Claude Desktop /
      Kyle AI Assistant config (see docs/13-mcp-runbook.md).

  Chunk 13 (SOP suggestions):
    First dry-run on real data:
      docker compose exec opsmemory-api python3 \
        scripts/run_sop_suggester.py
    If clusters look useful, --commit and review:
      curl .../v1/sop_suggestions
POSTDEPLOY
}

main "$@"
