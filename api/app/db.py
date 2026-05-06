"""Postgres connection pool for OpsMemory API.

Uses asyncpg with per-connection statement_timeout and
idle_in_transaction_session_timeout configured on each new connection.

Registers a JSONB codec so jsonb columns return as Python dicts/lists
instead of raw strings. Without this, code that reads `last_apply_error`
or `proposed_patch` from the DB has to json.loads() each value
defensively. The codec runs once per pool connection at setup time.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import asyncpg

log = logging.getLogger("opsmemory.db")

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> asyncpg.Pool:
    """Create the connection pool. Idempotent."""
    global _pool
    if _pool is not None:
        return _pool

    dsn = os.environ["DATABASE_URL"]
    min_size = int(os.environ.get("DB_POOL_MIN", "1"))
    max_size = int(os.environ.get("DB_POOL_MAX", "10"))
    statement_timeout_ms = int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "30000"))
    idle_in_tx_ms = int(os.environ.get("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", "30000"))

    async def _setup_connection(conn: asyncpg.Connection) -> None:
        await conn.execute(f"SET statement_timeout = {statement_timeout_ms}")
        await conn.execute(f"SET idle_in_transaction_session_timeout = {idle_in_tx_ms}")
        # Auto-decode jsonb / json so callers see dicts/lists, not strings.
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )
        await conn.set_type_codec(
            "json",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    _pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        setup=_setup_connection,
    )
    log.info(f"db_pool_initialized min={min_size} max={max_size}")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("db_pool_closed")


def pool() -> asyncpg.Pool:
    """Return the active pool. Raises if not initialized."""
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    return _pool


async def verify_migration(version: str) -> bool:
    """True if the named migration row exists and is not dirty."""
    if _pool is None:
        return False
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM schema_migrations WHERE version = $1 AND dirty = false",
            version,
        )
        return row is not None
