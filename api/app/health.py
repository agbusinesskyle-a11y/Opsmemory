"""Health and identity endpoints.

- ``/healthz`` — liveness, no DB dependency.
- ``/readyz`` — DB ping + migration check + optional backup/restore-freshness checks.
  Returns HTTP 503 (not 200) when not ready, so Docker/Cloudflare/monitors treat it correctly.
- ``/whoami`` — authenticated principal.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .auth import Principal, require_principal
from .db import verify_migration

log = logging.getLogger("opsmemory.health")

router = APIRouter()


def _not_ready(reason: str, **extra) -> JSONResponse:
    body = {"ok": False, "reason": reason, **extra}
    return JSONResponse(content=body, status_code=503)


def _check_status_file(path_env: str, default_path: str, max_age_env: str,
                       default_max_age_h: int) -> tuple[str | None, dict]:
    """Returns (failure_reason, extras). failure_reason None means OK."""
    path = Path(os.environ.get(path_env, default_path))
    max_age_h = int(os.environ.get(max_age_env, str(default_max_age_h)))
    if not path.exists():
        return "missing", {"path": str(path)}
    try:
        payload = json.loads(path.read_text())
    except Exception as exc:
        log.warning("status_file_unreadable", extra={"path": str(path), "err": repr(exc)})
        return "unreadable", {"path": str(path)}
    completed_at_str = payload.get("completed_at")
    if not completed_at_str:
        return "no_completed_at", {"path": str(path)}
    try:
        completed_at = datetime.fromisoformat(completed_at_str.replace("Z", "+00:00"))
    except Exception:
        return "bad_completed_at", {"path": str(path), "value": completed_at_str}
    age_hours = (datetime.now(timezone.utc) - completed_at).total_seconds() / 3600
    if age_hours > max_age_h:
        return "stale", {"age_hours": round(age_hours, 2), "max_age_hours": max_age_h}
    return None, {"age_hours": round(age_hours, 2)}


@router.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "service": "opsmemory-api",
        "version": os.environ.get("APP_VERSION", "chunk1"),
        "time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/readyz")
async def readyz(request: Request):
    pool = request.app.state.db

    # 1. DB ping.
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as exc:
        log.warning("readyz_db_unreachable", extra={"err": repr(exc)})
        return _not_ready("db_unreachable")

    # 2. Migration applied.
    if not await verify_migration("0001_initial"):
        return _not_ready("migration_missing", version="0001_initial")

    # 3. Backup status freshness (optional).
    require_backup = os.environ.get("READYZ_REQUIRE_BACKUP", "false").lower() == "true"
    backup_age = None
    if require_backup:
        reason, extras = _check_status_file(
            "BACKUP_STATUS_FILE",
            "/var/lib/opsmemory/backup/status.json",
            "READYZ_BACKUP_MAX_AGE_HOURS",
            36,
        )
        if reason:
            return _not_ready(f"backup_{reason}", **extras)
        backup_age = extras.get("age_hours")

    # 4. Restore status freshness.
    #
    # Two modes per Chunk 1.5 step 8:
    #
    #  - READYZ_REQUIRE_RESTORE=true  (production): missing/malformed/stale
    #    restore status fails readiness. Use after the weekly restore-check
    #    timer is enabled and producing fresh status JSON.
    #
    #  - READYZ_REQUIRE_RESTORE=false (default, backwards-compat): a present
    #    restore_status.json is checked for staleness; missing/malformed is
    #    tolerated. Lets the server boot even before the restore-check
    #    timer has fired.
    require_restore = os.environ.get("READYZ_REQUIRE_RESTORE", "false").lower() == "true"
    restore_age = None
    if require_restore:
        reason, extras = _check_status_file(
            "RESTORE_STATUS_FILE",
            "/var/lib/opsmemory/backup/restore_status.json",
            "READYZ_RESTORE_MAX_AGE_HOURS",
            192,
        )
        if reason:
            return _not_ready(f"restore_{reason}", **extras)
        restore_age = extras.get("age_hours")
    else:
        restore_status_path = os.environ.get("RESTORE_STATUS_FILE")
        if restore_status_path and Path(restore_status_path).exists():
            max_age_h = int(os.environ.get("READYZ_RESTORE_MAX_AGE_HOURS", "192"))
            try:
                payload = json.loads(Path(restore_status_path).read_text())
                completed_at = datetime.fromisoformat(
                    str(payload.get("completed_at", "")).replace("Z", "+00:00")
                )
                age = (datetime.now(timezone.utc) - completed_at).total_seconds() / 3600
                if age > max_age_h:
                    return _not_ready("restore_stale", age_hours=round(age, 2),
                                      max_age_hours=max_age_h)
                restore_age = round(age, 2)
            except Exception as exc:
                log.warning("readyz_restore_unreadable", extra={"err": repr(exc)})
                # Soft mode: don't fail readiness on a malformed restore file.

    # 5. XLSX-decode availability (Chunk 9 step 3).
    # Soft check: import xlsx_decode (which lazy-imports openpyxl +
    # defusedxml on first call). If openpyxl/defusedxml aren't installed
    # — or openpyxl doesn't have DEFUSEDXML hardening — the import
    # raises ImportError. /readyz reports the failure but doesn't 503,
    # because the rest of the API works. n8n's /v1/ingest/file_drop
    # XLSX path returns 503 itself when this happens. Codex
    # chunk-9-step3 (g): "missing openpyxl/defusedxml should show up
    # before n8n discovers it through retries."
    xlsx_decode_status: str
    try:
        from . import xlsx_decode  # noqa: F401
        xlsx_decode_status = "ok"
    except ImportError as exc:
        xlsx_decode_status = f"unavailable: {exc!r}"
        log.warning("readyz_xlsx_decode_unavailable", extra={"err": repr(exc)})

    return {
        "ok": True,
        "service": "opsmemory-api",
        "migration": "0001_initial",
        "backup_check": "enabled" if require_backup else "skipped",
        "backup_age_hours": backup_age,
        "restore_check": "required" if require_restore else "soft",
        "restore_age_hours": restore_age,
        "xlsx_decode": xlsx_decode_status,
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
