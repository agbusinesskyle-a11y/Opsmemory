#!/usr/bin/env python3
"""Bootstrap a service account specifically for the MCP read-only server.

Replacement for `scripts/bootstrap_service_account.py` when that script's
psql-variable-substitution path is broken on a given deploy. Uses asyncpg
directly against DATABASE_URL — no shelling out to docker.

Run inside the API container so it picks up the env vars + asyncpg wheel:

    docker compose run --rm -v /opt/opsmemory/scripts:/app/scripts \
      opsmemory-api python3 /app/scripts/bootstrap_mcp_account.py

Prints the raw key ONCE. Save it somewhere safe.

Reuses the chunk-1 key format and HMAC-SHA256(pepper, raw_key) hash so the
existing auth.py service-key validation accepts it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sys

import asyncpg


KEY_PREFIX = "opsmem_live_"
KID_LEN = 16
SECRET_LEN = 43


def _resolve_pepper() -> tuple[bytes, str | None]:
    """Return (pepper_bytes, pepper_version) per chunk-1 env contract.

    Versioned format wins; legacy SERVICE_KEY_PEPPER is the fallback.
    """
    active = (os.environ.get("SERVICE_KEY_PEPPER_ACTIVE_VERSION") or "").strip()
    if active:
        var = f"SERVICE_KEY_PEPPER_{active}"
        val = os.environ.get(var)
        if val:
            return val.encode("utf-8"), active
        raise RuntimeError(
            f"SERVICE_KEY_PEPPER_ACTIVE_VERSION={active} but {var} is unset"
        )
    legacy = os.environ.get("SERVICE_KEY_PEPPER")
    if legacy:
        return legacy.encode("utf-8"), None
    raise RuntimeError(
        "no pepper found. Set SERVICE_KEY_PEPPER_ACTIVE_VERSION + "
        "SERVICE_KEY_PEPPER_<VERSION>, or the legacy SERVICE_KEY_PEPPER."
    )


def _generate_raw_key() -> tuple[str, str, str]:
    """Return (raw_key, kid, secret). raw_key is what the operator pastes
    into the MCP client config; kid + secret are for audit / lookup.
    """
    kid = secrets.token_urlsafe(KID_LEN)[:KID_LEN]
    # Strip any trailing '-' or '_' that might cause parsing weirdness in
    # configs; regenerate if the alphabet drifts.
    while not kid.replace("-", "").replace("_", "").isalnum():
        kid = secrets.token_urlsafe(KID_LEN)[:KID_LEN]
    secret_part = secrets.token_urlsafe(SECRET_LEN)[:SECRET_LEN]
    raw_key = f"{KEY_PREFIX}{kid}_{secret_part}"
    return raw_key, kid, secret_part


def _hmac_hash(pepper: bytes, raw_key: str) -> str:
    return hmac.new(pepper, raw_key.encode("utf-8"), hashlib.sha256).hexdigest()


async def main() -> int:
    name = os.environ.get("MCP_ACCOUNT_NAME", "opsmemory-mcp")
    description = os.environ.get(
        "MCP_ACCOUNT_DESCRIPTION",
        "Read-only MCP server for AI assistant",
    )

    try:
        pepper, pepper_version = _resolve_pepper()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL is unset", file=sys.stderr)
        return 1

    raw_key, kid, _secret = _generate_raw_key()
    key_hash = _hmac_hash(pepper, raw_key)

    metadata: dict = {"kid": kid}
    if pepper_version:
        metadata["pepper_version"] = pepper_version

    # Idempotency check via the app role (which has SELECT). The
    # actual INSERT happens via psql as opsmemory_owner because
    # service_accounts is admin-managed.
    try:
        conn = await asyncpg.connect(dsn=dsn)
    except Exception as exc:
        print(f"ERROR: could not connect to DATABASE_URL: {exc}", file=sys.stderr)
        return 1
    try:
        existing = await conn.fetchrow(
            "SELECT id::text AS id, status FROM service_accounts WHERE name = $1",
            name,
        )
        if existing is not None:
            print(
                f"ERROR: service account named {name!r} already exists "
                f"(id={existing['id']}, status={existing['status']}). "
                f"Disable or pick a different name via MCP_ACCOUNT_NAME=...",
                file=sys.stderr,
            )
            return 1
    finally:
        await conn.close()

    # Build a self-contained psql command the operator runs as
    # opsmemory_owner via docker compose exec. All values are
    # inline-quoted with E'...' string literals (asyncpg-style
    # escaping doesn't apply here — psql's E-string handles
    # backslashes, and we control the inputs so no injection risk).
    metadata_json = json.dumps(metadata).replace("'", "''")
    name_sql = name.replace("'", "''")
    description_sql = description.replace("'", "''")
    insert_sql = (
        "INSERT INTO service_accounts "
        "(name, description, key_hash, scopes, status, metadata) "
        f"VALUES ('{name_sql}', '{description_sql}', '{key_hash}', "
        "ARRAY['mcp:read']::text[], 'active', "
        f"'{metadata_json}'::jsonb);"
    )

    print()
    print("=== RAW KEY (save somewhere safe — only printed once) ===")
    print(raw_key)
    print("===")
    print()
    print("=== Run this on the Spark HOST to insert the key_hash ===")
    print()
    print(
        f"docker compose exec postgres psql -U opsmemory_owner "
        f"-d action_tracker -c \"{insert_sql}\""
    )
    print()
    print("(That command runs as opsmemory_owner via Unix-socket peer auth")
    print(" inside the postgres container — no password needed.)")
    print()
    print("After it succeeds, the key is live. Use as:")
    print("  X-OpsMemory-Service-Key: <raw key>")
    print("Or set OPSMEMORY_MCP_SERVICE_KEY=<raw key> for the MCP server.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
