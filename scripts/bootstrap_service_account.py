#!/usr/bin/env python3
"""Generate a new service-account API key and persist it to action_tracker.

Usage:
    python3 scripts/bootstrap_service_account.py \
        --name "n8n-opsmemory-writer" \
        [--scopes write:tasks,read:owners] \
        [--description "n8n calls OpsMemory to ingest meeting recaps"] \
        [--env live|test] \
        [--expires-days 365]

Prints the generated key ONCE to stdout. The key is also recorded in the
service_accounts table with key_prefix + key_hash columns. The raw secret
half is NOT stored — anywhere — only the HMAC hash. Lost keys cannot be
recovered; revoke + rotate.

Key format:
    opsmem_<env>_<16-char-kid>_<43-char-secret>

  env     "live" or "test" — purely a label visible to the operator
  kid     URL-safe random ID (becomes service_accounts.key_prefix)
  secret  URL-safe random secret (HMAC'd with the active pepper)

The full displayed key is HMAC'd with SERVICE_KEY_PEPPER_<ACTIVE_VERSION>
(or legacy SERVICE_KEY_PEPPER) and stored as `key_hash`. The pepper version
used is recorded in `metadata.pepper_version` so multiple peppers can
coexist during rotation.

Environment:
    POSTGRES_CONTAINER       default: postgres
    ACTION_TRACKER_DB_ROLE   default: opsmemory_owner (DDL role can INSERT)
    ACTION_TRACKER_DB_NAME   default: action_tracker
    SERVICE_KEY_PEPPER_ACTIVE_VERSION  default: (legacy SERVICE_KEY_PEPPER, no version)
    SERVICE_KEY_PEPPER_<VERSION>       e.g. SERVICE_KEY_PEPPER_V1
    SERVICE_KEY_PEPPER       legacy chunk1 pepper, treated as version "" (none)

Exit codes:
    0  success
    1  config error (pepper missing, name conflict, etc.)
    2  validation error
    3  DB error
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import string
import subprocess
import sys
from datetime import datetime, timedelta, timezone


URL_SAFE_ALPHABET = string.ascii_letters + string.digits


def random_token(length: int) -> str:
    return "".join(secrets.choice(URL_SAFE_ALPHABET) for _ in range(length))


def resolve_pepper() -> tuple[str, str]:
    """Return (pepper_value, pepper_version_label).

    Version label is empty string for legacy SERVICE_KEY_PEPPER (chunk1
    backward compat). Version label is e.g. "V1" when using
    SERVICE_KEY_PEPPER_ACTIVE_VERSION + SERVICE_KEY_PEPPER_V1.
    """
    active = os.environ.get("SERVICE_KEY_PEPPER_ACTIVE_VERSION", "").strip()
    if active:
        env_name = f"SERVICE_KEY_PEPPER_{active.upper()}"
        pepper = os.environ.get(env_name, "")
        if not pepper:
            raise SystemExit(
                f"ERROR: SERVICE_KEY_PEPPER_ACTIVE_VERSION={active} but {env_name} is unset"
            )
        return pepper, active.upper()
    legacy = os.environ.get("SERVICE_KEY_PEPPER", "")
    if not legacy:
        raise SystemExit(
            "ERROR: no pepper found. Set SERVICE_KEY_PEPPER_ACTIVE_VERSION + "
            "SERVICE_KEY_PEPPER_<VERSION>, or the legacy SERVICE_KEY_PEPPER."
        )
    return legacy, ""


def hmac_key(pepper: str, raw: str) -> str:
    return hmac.new(pepper.encode(), raw.encode(), hashlib.sha256).hexdigest()


def insert_service_account(
    name: str,
    description: str,
    kid: str,
    key_hash: str,
    scopes: list[str],
    expires_at: datetime | None,
    pepper_version: str,
) -> None:
    """Insert via psql -v parameterized variables.

    `:'varname'` in the SQL body is replaced by psql with a properly
    quoted SQL string literal (single quotes doubled for embedded
    quotes). This prevents SQL injection from operator-controlled
    --description text. We pass each value as -v key=value; psql does
    the escaping.

    IMPORTANT: psql's :'varname' substitution does NOT work with
    `-c "...sql..."` — it only fires in script mode (stdin or
    `-f file`). Original chunk-1 implementation used `-c sql` and
    psql passed `:'name'` through to postgres verbatim, which
    syntax-errored. Fix: feed SQL via stdin so psql processes it
    in script mode and substitutes correctly.
    """
    container = os.environ.get("POSTGRES_CONTAINER", "postgres")
    role = os.environ.get("ACTION_TRACKER_DB_ROLE", "opsmemory_owner")
    db = os.environ.get("ACTION_TRACKER_DB_NAME", "action_tracker")

    metadata = json.dumps({"pepper_version": pepper_version})
    scopes_csv = ",".join(scopes)  # empty string when no scopes
    expires_iso = expires_at.isoformat() if expires_at else ""

    # service_accounts.key_prefix is the 16-char kid alone — auth.py
    # _load_service queries WHERE key_prefix = parsed["kid"] (the 16-char
    # random token from _parse_service_key). An earlier "fix" wrote the
    # full opsmem_<env>_<kid> string here, which made every service key
    # fail validation with "invalid service key" because auth.py was
    # looking for the bare kid. Reverted 2026-05-08; both chunk-1
    # generation (this script) and chunk-1 validation (auth.py) agree
    # that kid IS the key_prefix.

    sql = (
        "INSERT INTO service_accounts "
        "(name, description, role, status, key_prefix, key_hash, scopes, expires_at, metadata) "
        "VALUES ("
        ":'name', "
        ":'description', "
        "'service', 'active', "
        ":'kid', "
        ":'key_hash', "
        "string_to_array(NULLIF(:'scopes_csv', ''), ',')::text[], "
        "NULLIF(:'expires_iso', '')::timestamptz, "
        ":'metadata_json'::jsonb"
        ");"
    )

    proc = subprocess.run(
        [
            "docker", "exec", "-i", container, "psql",
            "-U", role, "-d", db,
            "-v", "ON_ERROR_STOP=1",
            "-v", f"name={name}",
            "-v", f"description={description}",
            "-v", f"kid={kid}",
            "-v", f"key_hash={key_hash}",
            "-v", f"scopes_csv={scopes_csv}",
            "-v", f"expires_iso={expires_iso}",
            "-v", f"metadata_json={metadata}",
        ],
        input=sql,        # SQL via stdin (script mode) — required
                          # for :'name' substitution to fire. -c does
                          # not perform substitution in psql.
        text=True, capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"ERROR: insert failed (psql exit {proc.returncode})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True,
                        help="Service-account display name (e.g. 'n8n-opsmemory-writer'). Must be unique.")
    parser.add_argument("--description", default="",
                        help="Free-form description shown in audit logs.")
    parser.add_argument("--scopes", default="",
                        help="Comma-separated scope list (e.g. 'write:tasks,read:owners'). May be empty.")
    parser.add_argument("--env", choices=["live", "test"], default="live",
                        help="Key environment label baked into the key string. Default: live.")
    parser.add_argument("--expires-days", type=int, default=0,
                        help="Days until the key expires. 0 = no expiry (rotate manually).")
    args = parser.parse_args()

    name_re = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,63}$")
    if not name_re.match(args.name):
        raise SystemExit("ERROR: --name must match ^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,63}$")

    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    for s in scopes:
        if not re.match(r"^[a-z][a-z0-9_:.-]{1,63}$", s):
            raise SystemExit(f"ERROR: invalid scope {s!r}")

    pepper, pepper_version = resolve_pepper()

    kid = random_token(16)
    secret = random_token(43)
    raw_key = f"opsmem_{args.env}_{kid}_{secret}"
    key_hash = hmac_key(pepper, raw_key)

    expires_at: datetime | None = None
    if args.expires_days > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(days=args.expires_days)

    insert_service_account(
        name=args.name,
        description=args.description,
        kid=kid,
        key_hash=key_hash,
        scopes=scopes,
        expires_at=expires_at,
        pepper_version=pepper_version,
    )

    print()
    print("=" * 64)
    print("SERVICE ACCOUNT CREATED")
    print("=" * 64)
    print(f"  name         : {args.name}")
    print(f"  description  : {args.description}")
    print(f"  scopes       : {','.join(scopes) if scopes else '(none)'}")
    print(f"  env          : {args.env}")
    print(f"  pepper       : version={pepper_version or '(legacy)'}")
    print(f"  kid          : {kid}")
    if expires_at:
        print(f"  expires_at   : {expires_at.isoformat()}")
    else:
        print(f"  expires_at   : never (rotate manually)")
    print()
    print("KEY (shown ONCE — copy now into the consumer's secret store):")
    print()
    print(f"    {raw_key}")
    print()
    print("Send via: X-OpsMemory-Service-Key header on requests to OpsMemory.")
    print("If lost: revoke (set status='disabled' in service_accounts) + rerun this script.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
