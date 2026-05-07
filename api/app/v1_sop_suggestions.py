"""OpsMemory SOP suggestions admin API (Chunk 13).

Admin-only. Lists pending suggestions, lets the operator accept
(promote to a draft sops + sop_versions + sop_template_tasks)
or dismiss.

Per Codex chunk-13 plan-review: accept transactionally creates
fresh sops + draft sop_versions + sop_template_tasks. The
existing chunk-7 publish path owns activation; this module
NEVER auto-publishes.

Endpoints (all admin-only; service principals 403):

  GET    /v1/sop_suggestions?business_slug=X&status=pending
  GET    /v1/sop_suggestions/{id}
  POST   /v1/sop_suggestions/{id}/accept
  POST   /v1/sop_suggestions/{id}/dismiss
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from .auth import Principal, require_principal
from .authz import require_admin


log = logging.getLogger("opsmemory.v1_sop_suggestions")

router = APIRouter(prefix="/v1/sop_suggestions")


_VALID_STATUS = frozenset({"pending", "accepted", "dismissed"})


class AcceptBody(BaseModel):
    """Optional operator-edited fields. Anything missing keeps the
    suggestion's proposed_* value.
    """
    model_config = {"extra": "forbid"}
    edited_name: str | None = Field(default=None, min_length=1, max_length=256)
    edited_description: str | None = Field(default=None, max_length=4096)
    edited_template: list[dict] | None = Field(default=None)


class DismissBody(BaseModel):
    model_config = {"extra": "forbid"}
    reason: str | None = Field(default=None, max_length=512)


def _serialize_suggestion(row: Any, *, include_template: bool = False) -> dict:
    out = {
        "id": row["id"],
        "business_id": row["business_id"],
        "business_slug": row.get("business_slug"),
        "business_name": row.get("business_name"),
        "proposed_name": row["proposed_name"],
        "proposed_description": row.get("proposed_description"),
        "seed_task_ids": list(row["seed_task_ids"]) if row["seed_task_ids"] else [],
        "status": row["status"],
        "cluster_signature": row["cluster_signature"],
        "promoted_sop_id": row.get("promoted_sop_id"),
        "suggestion_run_id": row.get("suggestion_run_id"),
        "rationale": row.get("rationale") or {},
        "dismissed_reason": row.get("dismissed_reason"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    if include_template:
        out["proposed_template"] = row.get("proposed_template") or []
    return out


# ---------------------------------------------------------------------------
# GET /v1/sop_suggestions
# ---------------------------------------------------------------------------

@router.get("")
async def list_suggestions(
    request: Request,
    principal: Principal = Depends(require_principal),
    business_slug: str | None = Query(default=None),
    suggestion_status: str | None = Query(default="pending", alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    require_admin(principal)
    where = []
    params: list[Any] = []
    if business_slug is not None:
        params.append(business_slug)
        where.append(f"b.slug = ${len(params)}")
    if suggestion_status is not None:
        if suggestion_status not in _VALID_STATUS and suggestion_status != "all":
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_status",
                        "got": suggestion_status,
                        "allowed": sorted(_VALID_STATUS) + ["all"]},
            )
        if suggestion_status != "all":
            params.append(suggestion_status)
            where.append(f"s.status = ${len(params)}")
    params.append(limit)
    limit_param = f"${len(params)}::int"
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT s.id::text                AS id,
               s.business_id::text       AS business_id,
               b.slug::text              AS business_slug,
               b.name                    AS business_name,
               s.proposed_name           AS proposed_name,
               s.proposed_description    AS proposed_description,
               s.seed_task_ids           AS seed_task_ids,
               s.status::text            AS status,
               s.cluster_signature       AS cluster_signature,
               s.promoted_sop_id::text   AS promoted_sop_id,
               s.suggestion_run_id::text AS suggestion_run_id,
               s.rationale               AS rationale,
               s.dismissed_reason        AS dismissed_reason,
               s.created_at::text        AS created_at,
               s.updated_at::text        AS updated_at
          FROM sop_suggestions s
          JOIN businesses b ON b.id = s.business_id
          {where_sql}
         ORDER BY s.created_at DESC
         LIMIT {limit_param}
    """
    pool = request.app.state.db
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {"items": [_serialize_suggestion(r) for r in rows]}


# ---------------------------------------------------------------------------
# GET /v1/sop_suggestions/{id}
# ---------------------------------------------------------------------------

@router.get("/{suggestion_id}")
async def get_suggestion(
    suggestion_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    require_admin(principal)
    sid = str(suggestion_id)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT s.id::text                AS id,
                   s.business_id::text       AS business_id,
                   b.slug::text              AS business_slug,
                   b.name                    AS business_name,
                   s.proposed_name           AS proposed_name,
                   s.proposed_description    AS proposed_description,
                   s.seed_task_ids           AS seed_task_ids,
                   s.proposed_template       AS proposed_template,
                   s.status::text            AS status,
                   s.cluster_signature       AS cluster_signature,
                   s.promoted_sop_id::text   AS promoted_sop_id,
                   s.suggestion_run_id::text AS suggestion_run_id,
                   s.rationale               AS rationale,
                   s.dismissed_reason        AS dismissed_reason,
                   s.created_at::text        AS created_at,
                   s.updated_at::text        AS updated_at
              FROM sop_suggestions s
              JOIN businesses b ON b.id = s.business_id
             WHERE s.id = $1::uuid
            """,
            sid,
        )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "suggestion_not_found", "id": sid},
            )
    return _serialize_suggestion(row, include_template=True)


# ---------------------------------------------------------------------------
# POST /v1/sop_suggestions/{id}/accept
# Codex chunk-13 plan-review (4): transactionally create fresh
# sops + sop_versions(state='draft') + sop_template_tasks. Existing
# chunk-7 publish path owns activation.
# ---------------------------------------------------------------------------

@router.post("/{suggestion_id}/accept")
async def accept_suggestion(
    suggestion_id: uuid.UUID,
    body: AcceptBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    require_admin(principal)
    sid = str(suggestion_id)
    pool = request.app.state.db

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id::text             AS id,
                       business_id::text    AS business_id,
                       proposed_name        AS proposed_name,
                       proposed_description AS proposed_description,
                       proposed_template    AS proposed_template,
                       status::text         AS status
                  FROM sop_suggestions
                 WHERE id = $1::uuid
                 FOR UPDATE
                """,
                sid,
            )
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "suggestion_not_found", "id": sid},
                )
            if row["status"] != "pending":
                raise HTTPException(
                    status_code=409,
                    detail={"code": "suggestion_not_pending",
                            "got_status": row["status"]},
                )

            name = (body.edited_name or row["proposed_name"]).strip()
            if not name:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "name_required"},
                )
            description = (
                body.edited_description
                if body.edited_description is not None
                else row["proposed_description"]
            )
            template = (
                body.edited_template
                if body.edited_template is not None
                else (row["proposed_template"] or [])
            )
            if not isinstance(template, list) or len(template) < 1:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "template_required",
                            "detail": "at least one template task"},
                )

            # 1. Create the fresh sops row (status='active', no
            #    latest_version_id yet — set after sop_version
            #    insert + the chunk-9 trigger requires the
            #    latest version belong to this sop).
            sop = await conn.fetchrow(
                """
                INSERT INTO sops (business_id, name, description,
                                   status, created_by)
                VALUES ($1::uuid, $2, $3, 'active', $4::uuid)
                RETURNING id::text AS id
                """,
                row["business_id"], name, description,
                principal.id,
            )
            sop_id = sop["id"]

            # 2. Create version 1 in 'draft' state.
            version = await conn.fetchrow(
                """
                INSERT INTO sop_versions (sop_id, version_no, state,
                                           change_log, created_by)
                VALUES ($1::uuid, 1, 'draft',
                        'Auto-promoted from sop_suggestion',
                        $2::uuid)
                RETURNING id::text AS id
                """,
                sop_id, principal.id,
            )
            version_id = version["id"]

            # 3. Insert template tasks. Carry across summary +
            #    description + due_offset_days + dependency_text +
            #    category + owner_role from each entry. Defaults
            #    are NULL for missing optional fields.
            for idx, t in enumerate(template):
                if not isinstance(t, dict):
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "template_entry_not_object",
                                "index": idx},
                    )
                summary = (t.get("summary") or "").strip()
                if not summary:
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "template_summary_required",
                                "index": idx},
                    )
                await conn.execute(
                    """
                    INSERT INTO sop_template_tasks
                      (sop_version_id, position, summary,
                       description, due_offset_days,
                       dependency_text, category, owner_role)
                    VALUES
                      ($1::uuid, $2::int, $3, $4, $5, $6, $7, $8)
                    """,
                    version_id, idx, summary,
                    t.get("description"),
                    t.get("due_offset_days"),
                    t.get("dependency_text"),
                    t.get("category"),
                    t.get("owner_role"),
                )

            # 4. Mark the suggestion accepted; link to the new sop.
            await conn.execute(
                """
                UPDATE sop_suggestions
                   SET status = 'accepted',
                       promoted_sop_id = $2::uuid,
                       proposed_template = $3::jsonb
                 WHERE id = $1::uuid
                """,
                sid, sop_id, template,
            )

    log.info("sop_suggestion_accepted", extra={
        "actor_user_id": principal.id,
        "suggestion_id": sid,
        "promoted_sop_id": sop_id,
        "template_count": len(template),
    })
    return {
        "id": sid,
        "status": "accepted",
        "promoted_sop_id": sop_id,
        "draft_sop_version_id": version_id,
    }


# ---------------------------------------------------------------------------
# POST /v1/sop_suggestions/{id}/dismiss
# ---------------------------------------------------------------------------

@router.post("/{suggestion_id}/dismiss")
async def dismiss_suggestion(
    suggestion_id: uuid.UUID,
    body: DismissBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    require_admin(principal)
    sid = str(suggestion_id)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE sop_suggestions
               SET status = 'dismissed',
                   dismissed_reason = $2::text
             WHERE id = $1::uuid
               AND status = 'pending'
            """,
            sid, body.reason,
        )
        if result.endswith(" 0"):
            row = await conn.fetchrow(
                "SELECT status::text AS status FROM sop_suggestions WHERE id = $1::uuid",
                sid,
            )
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "suggestion_not_found", "id": sid},
                )
            raise HTTPException(
                status_code=409,
                detail={"code": "suggestion_not_pending",
                        "got_status": row["status"]},
            )
    log.info("sop_suggestion_dismissed", extra={
        "actor_user_id": principal.id,
        "suggestion_id": sid,
        "reason": body.reason,
    })
    return {"id": sid, "status": "dismissed"}
