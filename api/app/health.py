"""Health and identity endpoints.

- ``/healthz`` — liveness, no DB dependency.
- ``/readyz`` — DB ping + migration check + optional backup-freshness check.
- ``/whoami`` — authenticated principal.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from .auth import Principal, require_principal
from .db import verify_migration

log = logging.getLogger("opsmemory.health")

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "service": "opsmemory-api",
        "version": os.environ.get("APP_VERSION", "chunk1"),
        "time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/readyz")
async def readyz(request: Request) -> dict:
    pool = request.app.state.db

    # 1. DB ping.
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as exc:
        log.warning(f"readyz_db_unreachable err={exc!r}")
        return {"ok": False, "reason": "db_unreachable"}

    # 2. Migration applied.
    if not await verify_migration("0001_initial"):
        return {"ok": False, "reason": "migration_missing", "version": "0001_initial"}

    # 3. Backup status check (only if required).
    require_backup = os.environ.get("READYZ_REQUIRE_BACKUP", "false").lower() == "true"
    if require_backup:
        backup_path = Path(
            os.environ.get(
                "BACKUP_STATUS_FILE",
                "/var/lib/opsmemory/backup/status.json",
            )
        )
        max_age_h = int(os.environ.get("READYZ_BACKUP_MAX_AGE_HOURS", "36"))

        if not backup_path.exists():
            return {"ok": False, "reason": "backup_status_missing"}

        try:
            payload = json.loads(backup_path.read_text())
            completed_at_str = payload.get("completed_at")
            if not completed_at_str:
                return {"ok": False, "reason": "backup_no_completed_at"}
            completed_at = datetime.fromisoformat(
                completed_at_str.replace("Z", "+00:00")
            )
            age_hours = (
                datetime.now(timezone.utc) - completed_at
            ).total_seconds() / 3600
            if age_hours > max_age_h:
                return {
                    "ok": False,
                    "reason": "backup_stale",
                    "age_hours": round(age_hours, 2),
                    "max_age_hours": max_age_h,
                }
        except Exception as exc:
            log.warning(f"readyz_backup_status_unreadable err={exc!r}")
            return {"ok": False, "reason": "backup_status_unreadable"}

    return {
        "ok": True,
        "service": "opsmemory-api",
        "migration": "0001_initial",
        "backup_check": "enabled" if require_backup else "skipped",
    }


@router.get("/whoami")
async def whoami(principal: Principal = Depends(require_principal)) -> dict:
    return {
        "principal_type": principal.principal_type,
        "id": principal.id,
        "email": principal.email,
        "display_name": principal.display_name,
        "role": principal.role,
        "businesses": principal.businesses,
        "permissions": principal.permissions,
        "auth_method": principal.auth_method,
    }


# Forward-compatible v1 alias.
@router.get("/v1/whoami")
async def whoami_v1(principal: Principal = Depends(require_principal)) -> dict:
    return await whoami(principal)
