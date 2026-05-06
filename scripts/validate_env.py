#!/usr/bin/env python3
"""Validate the OpsMemory production .env contract.

Used by:
  - manual operator runs during deploy ("does my .env look right?")
  - systemd ExecStartPre as a preflight before the API container starts
  - Chunk 1.5 backup wrappers (run_backup.sh / run_restore_check.sh
    can call this before doing work)

Behavior:
  - Reads the .env at $OPSMEMORY_ENV_FILE (default: /opt/opsmemory/.env)
    OR --env-file PATH OR --use-environ to read os.environ instead.
  - Production (ENVIRONMENT=production) fails closed: any required-var
    miss is fatal AND the dev-mode flags must be off.
  - Optional features are fatal when enabled but missing their deps:
      READYZ_REQUIRE_BACKUP=true  → BACKUP_STATUS_FILE
      READYZ_REQUIRE_RESTORE=true → RESTORE_STATUS_FILE
      GPG_ENABLED=true            → BACKUP_GPG_RECIPIENT
      B2_ENABLED=true             → B2_BUCKET + B2_KEY_ID + B2_APPLICATION_KEY
  - Logs variable NAMES on failure. Never logs values.

Exit codes:
  0   all required checks pass (warnings, if any, are still printed)
  1   one or more fatal errors
  2   --strict was passed AND warnings were present (CI mode)
  3   --env-file path missing or unreadable
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Var:
    name: str
    required: Callable[[dict[str, str]], bool]
    description: str


def _is_production(env: dict[str, str]) -> bool:
    return env.get("ENVIRONMENT", "").lower() == "production"


def _truthy(env: dict[str, str], key: str) -> bool:
    return env.get(key, "").strip().lower() in ("true", "1", "yes", "on")


SCHEMA: tuple[Var, ...] = (
    # Identity / runtime baseline
    Var("ENVIRONMENT", lambda e: True,
        "production | development. Affects fail-closed behavior."),
    Var("APP_VERSION", lambda e: False,
        "Image / build label, free-form."),
    Var("LOG_LEVEL", lambda e: False,
        "DEBUG | INFO | WARNING | ERROR. Default INFO."),

    # Auth contract
    Var("AUTH_MODE", lambda e: True,
        "cloudflare | local. Production must be cloudflare."),
    Var("CF_ACCESS_TEAM_DOMAIN",
        lambda e: e.get("AUTH_MODE", "").lower() == "cloudflare",
        "Cloudflare Access team domain (https://<team>.cloudflareaccess.com)."),
    Var("CF_ACCESS_AUD",
        lambda e: e.get("AUTH_MODE", "").lower() == "cloudflare",
        "Cloudflare Access application AUD tag for tracker.kyleconway.ai."),
    Var("LOCAL_DEV_EMAIL",
        lambda e: e.get("AUTH_MODE", "").lower() == "local",
        "Email of the seeded dev user (must exist in users table)."),
    Var("ALLOW_DEV_USER_SWITCH", lambda e: False,
        "true | false. Production forbids true."),
    # SERVICE_KEY_PEPPER (legacy, no version) is required UNLESS the operator
    # configured the versioned scheme. With versioning, the versioned envs
    # are required instead. validate_env doesn't enumerate every possible
    # version key; it just checks that one of the two schemes is present.
    Var("SERVICE_KEY_PEPPER",
        lambda e: not bool(e.get("SERVICE_KEY_PEPPER_ACTIVE_VERSION", "").strip()),
        "32-byte random hex for service-account HMAC. Replace with versioned scheme for rotation."),

    # Database
    Var("DATABASE_URL", lambda e: True,
        "Runtime app DSN (opsmemory_app role, narrow grants)."),
    Var("DB_POOL_MIN", lambda e: False,
        "asyncpg pool min size. Default 1."),
    Var("DB_POOL_MAX", lambda e: False,
        "asyncpg pool max size. Default 10."),
    Var("DB_STATEMENT_TIMEOUT_MS", lambda e: False,
        "Per-connection statement_timeout (ms). Default 30000."),
    Var("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", lambda e: False,
        "Per-connection idle_in_transaction timeout (ms). Default 30000."),

    # Web
    Var("WEB_ROOT", lambda e: False,
        "PWA static-files root inside container. Default /app/web."),

    # Health/readiness
    Var("READYZ_REQUIRE_BACKUP", lambda e: False,
        "true | false. When true, /readyz fails on stale or missing backup status."),
    Var("READYZ_BACKUP_MAX_AGE_HOURS", lambda e: False,
        "Threshold for backup_age check. Default 36."),
    Var("READYZ_REQUIRE_RESTORE", lambda e: False,
        "true | false. When true, /readyz fails on stale or missing restore status."),
    Var("READYZ_RESTORE_MAX_AGE_HOURS", lambda e: False,
        "Threshold for restore_age check. Default 192."),
    Var("BACKUP_STATUS_FILE", lambda e: _truthy(e, "READYZ_REQUIRE_BACKUP"),
        "Path to backup status JSON written by backup script."),
    Var("RESTORE_STATUS_FILE", lambda e: _truthy(e, "READYZ_REQUIRE_RESTORE"),
        "Path to restore status JSON written by restore-check."),

    # Backup script
    Var("POSTGRES_CONTAINER", lambda e: False,
        "Existing pg container name. Default 'postgres'."),
    Var("ACTION_TRACKER_DB_ROLE", lambda e: False,
        "DDL role used by backup. Default 'opsmemory_owner'."),
    Var("ACTION_TRACKER_DB_NAME", lambda e: False,
        "DB name. Default 'action_tracker'."),
    Var("BACKUP_ROOT", lambda e: False,
        "Local backup directory. Default /var/backups/opsmemory/action_tracker."),
    Var("BACKUP_RETENTION_DAYS", lambda e: False,
        "Local retention horizon. Default 14."),
    Var("BACKUP_SPARK2_TARGET", lambda e: False,
        "Optional rsync target for Spark #2."),
    Var("BACKUP_ALERT_WEBHOOK_URL", lambda e: False,
        "Optional failure-alert webhook (n8n)."),

    # Restore-check
    Var("RESTORE_TEST_ADMIN_USER", lambda e: False,
        "Existing pg superuser name. Default 'openbrain' (per Spark #1 deploy)."),
    Var("RESTORE_TEST_ADMIN_DB", lambda e: False,
        "Existing pg admin DB. Default 'openbrain'."),
    Var("RESTORE_TEST_DB", lambda e: False,
        "Test DB name created by restore-check. Default 'action_tracker_restore_test'."),

    # Network
    Var("SPARK_NETWORK_NAME", lambda e: False,
        "Existing Docker network name on Spark."),

    # Chunk 1.5+ feature flags (forward-compatible)
    Var("GPG_ENABLED", lambda e: False,
        "Chunk 1.5: encrypt backups before leaving home network."),
    Var("BACKUP_GPG_RECIPIENT",
        lambda e: _truthy(e, "GPG_ENABLED"),
        "Public-key recipient for GPG encryption."),
    Var("B2_ENABLED", lambda e: False,
        "Chunk 1.5: upload backups to Backblaze B2 offsite."),
    Var("B2_BUCKET",
        lambda e: _truthy(e, "B2_ENABLED"),
        "Backblaze B2 bucket name."),
    Var("B2_KEY_ID",
        lambda e: _truthy(e, "B2_ENABLED"),
        "Backblaze B2 application key ID."),
    Var("B2_APPLICATION_KEY",
        lambda e: _truthy(e, "B2_ENABLED"),
        "Backblaze B2 application key."),
)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        # Strip optional surrounding quotes (single or double).
        v = value.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        out[key] = v
    return out


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

@dataclass
class Result:
    fatal: list[str]
    warn: list[str]


def check(env: dict[str, str]) -> Result:
    fatal: list[str] = []
    warn: list[str] = []
    prod = _is_production(env)

    # Required-by-schema check.
    for v in SCHEMA:
        present = bool(env.get(v.name, "").strip())
        if v.required(env):
            if not present:
                fatal.append(f"{v.name} required ({v.description})")
        else:
            # Optional: warn only if obviously missing in production AND it's
            # tied to a feature flag that's also unset.
            pass

    # Production fail-closed: AUTH_MODE=local is forbidden in production.
    if prod and env.get("AUTH_MODE", "").lower() == "local":
        fatal.append("AUTH_MODE=local forbidden when ENVIRONMENT=production")
    if prod and _truthy(env, "ALLOW_DEV_USER_SWITCH"):
        fatal.append("ALLOW_DEV_USER_SWITCH=true forbidden when ENVIRONMENT=production")

    # SERVICE_KEY_PEPPER must look random (basic sanity, not a quality check).
    pepper = env.get("SERVICE_KEY_PEPPER", "")
    if pepper and len(pepper) < 32:
        fatal.append("SERVICE_KEY_PEPPER shorter than 32 chars; regenerate with"
                     " python3 -c 'import secrets; print(secrets.token_hex(32))'")
    if pepper.startswith("<") and pepper.endswith(">"):
        fatal.append("SERVICE_KEY_PEPPER still has placeholder pointy-brackets;"
                     " replace with real value")

    # CF_ACCESS_TEAM_DOMAIN should be https://*.cloudflareaccess.com
    if env.get("AUTH_MODE", "").lower() == "cloudflare":
        team = env.get("CF_ACCESS_TEAM_DOMAIN", "")
        if team and not team.startswith("https://"):
            fatal.append("CF_ACCESS_TEAM_DOMAIN must start with https://")
        if team and not team.endswith("cloudflareaccess.com"):
            fatal.append("CF_ACCESS_TEAM_DOMAIN must end with cloudflareaccess.com")
        aud = env.get("CF_ACCESS_AUD", "")
        if aud and (aud.startswith("<") or len(aud) < 16):
            fatal.append("CF_ACCESS_AUD looks like a placeholder; replace with the"
                         " AUD tag from the Cloudflare Access app Overview tab")

    # Database URL sanity (no hardcoded placeholder password).
    db_url = env.get("DATABASE_URL", "")
    if "<password>" in db_url or "<owner-password>" in db_url or "<app-password>" in db_url:
        fatal.append("DATABASE_URL still contains placeholder password — fill it in")

    # READYZ_REQUIRE_BACKUP+age sanity
    if _truthy(env, "READYZ_REQUIRE_BACKUP"):
        try:
            int(env.get("READYZ_BACKUP_MAX_AGE_HOURS", "36"))
        except ValueError:
            fatal.append("READYZ_BACKUP_MAX_AGE_HOURS not an integer")

    if _truthy(env, "READYZ_REQUIRE_RESTORE"):
        try:
            int(env.get("READYZ_RESTORE_MAX_AGE_HOURS", "192"))
        except ValueError:
            fatal.append("READYZ_RESTORE_MAX_AGE_HOURS not an integer")

    # B2 keys must not look like placeholders.
    if _truthy(env, "B2_ENABLED"):
        for k in ("B2_BUCKET", "B2_KEY_ID", "B2_APPLICATION_KEY"):
            v = env.get(k, "")
            if v.startswith("<") and v.endswith(">"):
                fatal.append(f"{k} still has placeholder pointy-brackets")

    # GPG recipient sanity.
    if _truthy(env, "GPG_ENABLED"):
        recipient = env.get("BACKUP_GPG_RECIPIENT", "")
        if recipient.startswith("<"):
            fatal.append("BACKUP_GPG_RECIPIENT still has placeholder")

    # Production fail-closed: ingest pipeline must NOT default to mock LLM.
    if prod:
        for var, step in (
            ("INGEST_LLM_EXTRACT_MODELS", "extract"),
            ("INGEST_LLM_CHOOSE_MODELS", "choose"),
        ):
            chain = [m.strip() for m in env.get(var, "").split(",") if m.strip()]
            if not chain:
                # Falls back to 'mock' default in code — fatal in production.
                fatal.append(f"{var} unset; defaults to mock at runtime "
                             f"(production must set real providers for the "
                             f"{step} step)")
                continue
            if all(m == "mock" for m in chain):
                fatal.append(f"{var}={','.join(chain)} is mock-only "
                             f"(production cannot run the {step} step on mock)")
        # If real models are configured, LITELLM_BASE_URL must be set.
        any_real = False
        for var in ("INGEST_LLM_EXTRACT_MODELS", "INGEST_LLM_CHOOSE_MODELS"):
            chain = [m.strip() for m in env.get(var, "").split(",") if m.strip()]
            if any(m != "mock" for m in chain):
                any_real = True
                break
        if any_real and not env.get("LITELLM_BASE_URL", "").strip():
            fatal.append("LITELLM_BASE_URL required when ingest LLM chains "
                         "include non-mock providers")

    # Production-extra warnings.
    if prod and not _truthy(env, "READYZ_REQUIRE_BACKUP"):
        warn.append("ENVIRONMENT=production but READYZ_REQUIRE_BACKUP is not enabled"
                    " — readiness will not catch a stalled backup loop")
    if prod and not _truthy(env, "GPG_ENABLED"):
        warn.append("ENVIRONMENT=production but GPG_ENABLED=false — backups leaving"
                    " home network (Spark #2 / B2) will be unencrypted")
    if prod and not _truthy(env, "B2_ENABLED"):
        warn.append("ENVIRONMENT=production but B2_ENABLED=false — only 2-of-3"
                    " backup copies (no offsite); chunk1.5 step 6 will land this")

    return Result(fatal=fatal, warn=warn)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", "-f",
                        help="Path to .env file (default: $OPSMEMORY_ENV_FILE or /opt/opsmemory/.env)")
    parser.add_argument("--use-environ", action="store_true",
                        help="Read os.environ instead of an .env file")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Only emit failures (no success message)")
    parser.add_argument("--strict", action="store_true",
                        help="Exit 2 on warnings (CI mode). Default: exit 0 on"
                             " warnings (still printed; systemd-friendly).")
    args = parser.parse_args(argv)

    if args.use_environ:
        env: dict[str, str] = {k: v for k, v in os.environ.items()}
        source = "<os.environ>"
    else:
        path_str = args.env_file or os.environ.get("OPSMEMORY_ENV_FILE", "/opt/opsmemory/.env")
        path = Path(path_str)
        if not path.exists() or not path.is_file():
            print(f"ERROR: env file not found: {path}", file=sys.stderr)
            return 3
        try:
            env = parse_env_file(path)
        except Exception as exc:
            print(f"ERROR: failed to parse {path}: {exc!r}", file=sys.stderr)
            return 3
        source = str(path)

    res = check(env)

    if res.fatal:
        print(f"FAIL ({source}): {len(res.fatal)} required check(s) failed", file=sys.stderr)
        for line in res.fatal:
            print(f"  - {line}", file=sys.stderr)
    if res.warn:
        out = sys.stderr if res.fatal else sys.stdout
        print(f"WARN ({source}): {len(res.warn)} non-fatal warning(s)", file=out)
        for line in res.warn:
            print(f"  - {line}", file=out)

    if res.fatal:
        return 1
    if res.warn and args.strict:
        return 2
    if not args.quiet:
        if res.warn:
            print(f"OK with {len(res.warn)} warning(s) ({source}): no fatal violations")
        else:
            print(f"OK ({source}): all required vars present, no production fail-closed violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
