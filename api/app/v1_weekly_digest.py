"""OpsMemory v1 weekly Gmail digest admin API (Chunk 11).

Admin-only CRUD for weekly_digest_allowlist + read-only access to
weekly_digest_runs. Service principals are 403; non-admin user
principals are 403.

Per docs/01-design.md locked decision: drafts only, recipient
allowlist, never auto-sends. The API does NOT expose a "trigger
a run now" endpoint — the cron is the only writer to
weekly_digest_runs.

Endpoints:

  GET    /v1/weekly_digest/allowlist?business_slug=X
  POST   /v1/weekly_digest/allowlist
  DELETE /v1/weekly_digest/allowlist/{id}
  GET    /v1/weekly_digest/runs?business_slug=X[&limit=N]
  GET    /v1/weekly_digest/runs/{id}
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from .auth import Principal, require_principal


log = logging.getLogger("opsmemory.v1_weekly_digest")

router = APIRouter(prefix="/v1/weekly_digest")


_VALID_ROLES = frozenset({"to", "cc", "bcc"})


def _require_admin(principal: Principal) -> None:
    if principal.principal_type != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="weekly digest admin requires a user principal",
        )
    if principal.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="weekly digest admin requires admin role",
        )


class AllowlistAddBody(BaseModel):
    model_config = {"extra": "forbid"}
    business_slug: str = Field(..., min_length=1, max_length=64)
    recipient_email: str = Field(..., min_length=3, max_length=320)
    role: str = Field(...)
    notes: str | None = Field(default=None, max_length=512)


def _serialize_allowlist_row(row: Any) -> dict:
    return {
        "id": row["id"],
        "business_id": row["business_id"],
        "business_slug": row.get("business_slug"),
        "business_name": row.get("business_name"),
        "recipient_email": row["recipient_email"],
        "role": row["role"],
        "notes": row.get("notes"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _serialize_run_row(row: Any, *, include_payload: bool = False) -> dict:
    out = {
        "id": row["id"],
        "business_id": row["business_id"],
        "business_slug": row.get("business_slug"),
        "business_name": row.get("business_name"),
        "week_start_iso": row["week_start_iso"],
        "week_end_iso": row["week_end_iso"],
        "status": row["status"],
        "idempotency_key": row["idempotency_key"],
        "draft_id": row.get("draft_id"),
        "scheduled_for": row.get("scheduled_for"),
        "attempted_at": row.get("attempted_at"),
        "sent_at": row.get("sent_at"),
        "failed_at": row.get("failed_at"),
        "error": row["error"] or {},
        "created_at": row.get("created_at"),
    }
    if include_payload:
        out["payload"] = row["payload"] or {}
    else:
        # Lighter list shape: just the counts for grep'ing audits.
        payload = row["payload"] or {}
        out["counts"] = payload.get("counts") or {}
    return out


# ---------------------------------------------------------------------------
# GET /v1/weekly_digest/allowlist
# ---------------------------------------------------------------------------

@router.get("/allowlist")
async def list_allowlist(
    request: Request,
    principal: Principal = Depends(require_principal),
    business_slug: str | None = Query(default=None),
) -> dict:
    _require_admin(principal)
    pool = request.app.state.db
    where = []
    params: list[Any] = []
    if business_slug is not None:
        params.append(business_slug)
        where.append(f"b.slug = ${len(params)}")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT a.id::text                AS id,
               a.business_id::text       AS business_id,
               b.slug::text              AS business_slug,
               b.name                    AS business_name,
               a.recipient_email::text   AS recipient_email,
               a.role                    AS role,
               a.notes                   AS notes,
               a.created_at::text        AS created_at,
               a.updated_at::text        AS updated_at
          FROM weekly_digest_allowlist a
          JOIN businesses b ON b.id = a.business_id
          {where_sql}
         ORDER BY b.slug, a.role, a.recipient_email
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {"items": [_serialize_allowlist_row(r) for r in rows]}


# ---------------------------------------------------------------------------
# POST /v1/weekly_digest/allowlist
# ---------------------------------------------------------------------------

@router.post("/allowlist", status_code=201)
async def add_allowlist(
    body: AllowlistAddBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    _require_admin(principal)
    if body.role not in _VALID_ROLES:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_role",
                    "got": body.role,
                    "allowed": sorted(_VALID_ROLES)},
        )
    if "@" not in body.recipient_email:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_email",
                    "got": body.recipient_email},
        )

    pool = request.app.state.db
    async with pool.acquire() as conn:
        biz = await conn.fetchrow(
            """
            SELECT id::text AS id
              FROM businesses
             WHERE slug = $1
               AND deletion_state = 'active'
            """,
            body.business_slug,
        )
        if biz is None:
            raise HTTPException(
                status_code=422,
                detail={"code": "business_not_found",
                        "slug": body.business_slug},
            )
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO weekly_digest_allowlist
                  (business_id, recipient_email, role, notes)
                VALUES
                  ($1::uuid, $2::citext, $3::text, $4::text)
                RETURNING id::text          AS id,
                          business_id::text AS business_id,
                          recipient_email::text AS recipient_email,
                          role              AS role,
                          notes             AS notes,
                          created_at::text  AS created_at,
                          updated_at::text  AS updated_at
                """,
                biz["id"], body.recipient_email,
                body.role, body.notes,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail={"code": "recipient_already_listed",
                        "business_slug": body.business_slug,
                        "recipient_email": body.recipient_email},
            )

    log.info("weekly_digest_allowlist_added", extra={
        "actor_user_id": principal.id,
        "business_slug": body.business_slug,
        "allowlist_id": row["id"],
    })
    out = _serialize_allowlist_row(row)
    out["business_slug"] = body.business_slug
    return out


# ---------------------------------------------------------------------------
# DELETE /v1/weekly_digest/allowlist/{id}
# ---------------------------------------------------------------------------

@router.delete("/allowlist/{allowlist_id}")
async def remove_allowlist(
    allowlist_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    _require_admin(principal)
    aid = str(allowlist_id)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM weekly_digest_allowlist WHERE id = $1::uuid",
            aid,
        )
        if result.endswith(" 0"):
            raise HTTPException(
                status_code=404,
                detail={"code": "allowlist_entry_not_found", "id": aid},
            )
    log.info("weekly_digest_allowlist_removed", extra={
        "actor_user_id": principal.id,
        "allowlist_id": aid,
    })
    return {"id": aid, "deleted": True}


# ---------------------------------------------------------------------------
# GET /v1/weekly_digest/runs
# ---------------------------------------------------------------------------

@router.get("/runs")
async def list_runs(
    request: Request,
    principal: Principal = Depends(require_principal),
    business_slug: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    _require_admin(principal)
    pool = request.app.state.db
    where = []
    params: list[Any] = []
    if business_slug is not None:
        params.append(business_slug)
        where.append(f"b.slug = ${len(params)}")
    params.append(limit)
    limit_param = f"${len(params)}::int"
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT r.id::text                  AS id,
               r.business_id::text         AS business_id,
               b.slug::text                AS business_slug,
               b.name                      AS business_name,
               r.week_start_iso::text      AS week_start_iso,
               r.week_end_iso::text        AS week_end_iso,
               r.status::text              AS status,
               r.idempotency_key           AS idempotency_key,
               r.payload                   AS payload,
               r.error                     AS error,
               r.draft_id                  AS draft_id,
               r.scheduled_for::text       AS scheduled_for,
               r.attempted_at::text        AS attempted_at,
               r.sent_at::text             AS sent_at,
               r.failed_at::text           AS failed_at,
               r.created_at::text          AS created_at
          FROM weekly_digest_runs r
          JOIN businesses b ON b.id = r.business_id
          {where_sql}
         ORDER BY r.created_at DESC
         LIMIT {limit_param}
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {"items": [_serialize_run_row(r) for r in rows]}


# ---------------------------------------------------------------------------
# GET /v1/weekly_digest/runs/{id}
# ---------------------------------------------------------------------------

@router.get("/runs/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    _require_admin(principal)
    rid = str(run_id)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT r.id::text                  AS id,
                   r.business_id::text         AS business_id,
                   b.slug::text                AS business_slug,
                   b.name                      AS business_name,
                   r.week_start_iso::text      AS week_start_iso,
                   r.week_end_iso::text        AS week_end_iso,
                   r.status::text              AS status,
                   r.idempotency_key           AS idempotency_key,
                   r.payload                   AS payload,
                   r.error                     AS error,
                   r.draft_id                  AS draft_id,
                   r.scheduled_for::text       AS scheduled_for,
                   r.attempted_at::text        AS attempted_at,
                   r.sent_at::text             AS sent_at,
                   r.failed_at::text           AS failed_at,
                   r.created_at::text          AS created_at
              FROM weekly_digest_runs r
              JOIN businesses b ON b.id = r.business_id
             WHERE r.id = $1::uuid
            """,
            rid,
        )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "run_not_found", "id": rid},
            )
    return _serialize_run_row(row, include_payload=True)
