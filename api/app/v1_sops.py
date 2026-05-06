"""OpsMemory v1 SOPs read API (Chunk 7 step 2).

Endpoints (admin-only first pass):

  GET /v1/sops                              list SOPs (filters)
  GET /v1/sops/{id}                         detail with versions[]
  GET /v1/sops/{id}/versions/{version_no}   one version + ordered templates
  GET /v1/anchor_events                     list anchors (filters)
  GET /v1/anchor_events/{id}                anchor detail
  GET /v1/anchor_events/{id}/instances      fires of one anchor
  GET /v1/sop_instances/{id}                instance with junction rows joined
                                            to review_items + tasks

Authz model (per Codex chunk-7-step1 review): admin-only initially.
require_admin like the review queue endpoints. Owner-scoped reads via
visible_business_ids are doable but expand the test matrix and expose
operational playbooks before write/authz semantics are settled. Admit
owner reads in a later commit if/when the SOP browse UX needs them.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .auth import Principal, require_principal
from .authz import require_admin

log = logging.getLogger("opsmemory.v1_sops")

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_business_id(conn, business_slug: str | None) -> str | None:
    if not business_slug:
        return None
    row = await conn.fetchrow(
        "SELECT id::text AS id FROM businesses "
        "WHERE slug::text = $1 AND deletion_state = 'active'",
        business_slug,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "business_not_found", "slug": business_slug},
        )
    return row["id"]


def _serialize_sop(row: Any) -> dict:
    return {
        "id": row["id"],
        "business_id": row["business_id"],
        "name": row["name"],
        "description": row.get("description"),
        "status": row["status"],
        "latest_version_id": row.get("latest_version_id"),
        "latest_version_no": row.get("latest_version_no"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _serialize_version(row: Any) -> dict:
    return {
        "id": row["id"],
        "sop_id": row["sop_id"],
        "version_no": row["version_no"],
        "state": row["state"],
        "change_log": row.get("change_log"),
        "created_at": row.get("created_at"),
        "published_at": row.get("published_at"),
        "published_by": row.get("published_by"),
    }


def _serialize_template(row: Any) -> dict:
    return {
        "id": row["id"],
        "seq_no": row["seq_no"],
        "summary": row["summary"],
        "description": row.get("description"),
        "due_offset_days": row.get("due_offset_days"),
        "dependency_text": row.get("dependency_text"),
        "category": row.get("category"),
        "priority": row.get("priority"),
        "owner_role": row.get("owner_role"),
        "owner_user_id": row.get("owner_user_id"),
    }


def _serialize_anchor(row: Any) -> dict:
    return {
        "id": row["id"],
        "business_id": row["business_id"],
        "kind": row["kind"],
        "sop_id": row["sop_id"],
        "scheduled_for": row["scheduled_for"],
        "state": row["state"],
        "fired_at": row.get("fired_at"),
        "fired_by": row.get("fired_by"),
        "cancelled_at": row.get("cancelled_at"),
        "cancelled_by": row.get("cancelled_by"),
        "notes": row.get("notes"),
        "created_at": row.get("created_at"),
    }


def _serialize_instance(row: Any) -> dict:
    return {
        "id": row["id"],
        "anchor_event_id": row["anchor_event_id"],
        "sop_version_id": row["sop_version_id"],
        "ingest_event_id": row.get("ingest_event_id"),
        "fired_at": row["fired_at"],
        "fired_by": row.get("fired_by"),
    }


def _serialize_generated(row: Any) -> dict:
    return {
        "id": row["id"],
        "sop_instance_id": row["sop_instance_id"],
        "sop_template_task_id": row["sop_template_task_id"],
        "review_item_id": row.get("review_item_id"),
        "review_item_status": row.get("review_item_status"),
        "task_id": row.get("task_id"),
        "task_status": row.get("task_status"),
        "task_summary": row.get("task_summary"),
        "manually_overridden_fields": row.get("manually_overridden_fields") or [],
    }


# ---------------------------------------------------------------------------
# GET /v1/sops
# ---------------------------------------------------------------------------

@router.get("/v1/sops")
async def list_sops(
    request: Request,
    principal: Principal = Depends(require_principal),
    business_slug: str | None = Query(default=None, max_length=64),
    sop_status: str | None = Query(default=None, alias="status",
                                     description="active | archived"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    require_admin(principal)
    if sop_status is not None and sop_status not in ("active", "archived"):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_status", "got": sop_status},
        )

    pool = request.app.state.db
    async with pool.acquire() as conn:
        biz_id = await _resolve_business_id(conn, business_slug)

        params: list[Any] = []
        where: list[str] = []
        if biz_id:
            params.append(biz_id)
            where.append(f"s.business_id = ${len(params)}::uuid")
        if sop_status:
            params.append(sop_status)
            where.append(f"s.status::text = ${len(params)}")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        params.append(limit)
        limit_p = len(params)
        params.append(offset)
        offset_p = len(params)

        rows = await conn.fetch(
            f"""
            SELECT s.id::text                AS id,
                   s.business_id::text       AS business_id,
                   s.name                    AS name,
                   s.description             AS description,
                   s.status::text            AS status,
                   s.latest_version_id::text AS latest_version_id,
                   v.version_no              AS latest_version_no,
                   s.created_at::text        AS created_at,
                   s.updated_at::text        AS updated_at
            FROM sops s
            LEFT JOIN sop_versions v ON v.id = s.latest_version_id
            {where_sql}
            ORDER BY s.business_id, s.name
            LIMIT ${limit_p} OFFSET ${offset_p}
            """,
            *params,
        )
        # Use the same WHERE for the count.
        count_params = params[: len(params) - 2]
        count_row = await conn.fetchrow(
            f"SELECT count(*) AS c FROM sops s {where_sql}",
            *count_params,
        )

    return {
        "items": [_serialize_sop(r) for r in rows],
        "limit": limit,
        "offset": offset,
        "total": int(count_row["c"]),
    }


# ---------------------------------------------------------------------------
# GET /v1/sops/{id}
# ---------------------------------------------------------------------------

@router.get("/v1/sops/{sop_id}")
async def get_sop(
    sop_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    require_admin(principal)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        sop_row = await conn.fetchrow(
            """
            SELECT s.id::text                AS id,
                   s.business_id::text       AS business_id,
                   s.name                    AS name,
                   s.description             AS description,
                   s.status::text            AS status,
                   s.latest_version_id::text AS latest_version_id,
                   v.version_no              AS latest_version_no,
                   s.created_at::text        AS created_at,
                   s.updated_at::text        AS updated_at
            FROM sops s
            LEFT JOIN sop_versions v ON v.id = s.latest_version_id
            WHERE s.id = $1::uuid
            """,
            str(sop_id),
        )
        if not sop_row:
            raise HTTPException(
                status_code=404,
                detail={"code": "sop_not_found", "id": str(sop_id)},
            )
        version_rows = await conn.fetch(
            """
            SELECT id::text          AS id,
                   sop_id::text      AS sop_id,
                   version_no        AS version_no,
                   state::text       AS state,
                   change_log        AS change_log,
                   created_at::text  AS created_at,
                   published_at::text AS published_at,
                   published_by::text AS published_by
            FROM sop_versions
            WHERE sop_id = $1::uuid
            ORDER BY version_no DESC
            """,
            str(sop_id),
        )

    return {
        "sop": _serialize_sop(sop_row),
        "versions": [_serialize_version(v) for v in version_rows],
    }


# ---------------------------------------------------------------------------
# GET /v1/sops/{id}/versions/{version_no}
# ---------------------------------------------------------------------------

@router.get("/v1/sops/{sop_id}/versions/{version_no}")
async def get_sop_version(
    sop_id: uuid.UUID,
    version_no: int,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    require_admin(principal)
    if version_no < 1:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_version_no", "got": version_no},
        )

    pool = request.app.state.db
    async with pool.acquire() as conn:
        version_row = await conn.fetchrow(
            """
            SELECT id::text          AS id,
                   sop_id::text      AS sop_id,
                   version_no        AS version_no,
                   state::text       AS state,
                   change_log        AS change_log,
                   created_at::text  AS created_at,
                   published_at::text AS published_at,
                   published_by::text AS published_by
            FROM sop_versions
            WHERE sop_id = $1::uuid AND version_no = $2
            """,
            str(sop_id), version_no,
        )
        if not version_row:
            raise HTTPException(
                status_code=404,
                detail={"code": "sop_version_not_found",
                        "sop_id": str(sop_id),
                        "version_no": version_no},
            )
        template_rows = await conn.fetch(
            """
            SELECT id::text             AS id,
                   seq_no               AS seq_no,
                   summary              AS summary,
                   description          AS description,
                   due_offset_days      AS due_offset_days,
                   dependency_text      AS dependency_text,
                   category             AS category,
                   priority             AS priority,
                   owner_role           AS owner_role,
                   owner_user_id::text  AS owner_user_id
            FROM sop_template_tasks
            WHERE sop_version_id = $1::uuid
            ORDER BY seq_no
            """,
            version_row["id"],
        )

    return {
        "version": _serialize_version(version_row),
        "template_tasks": [_serialize_template(t) for t in template_rows],
    }


# ---------------------------------------------------------------------------
# GET /v1/anchor_events
# ---------------------------------------------------------------------------

@router.get("/v1/anchor_events")
async def list_anchor_events(
    request: Request,
    principal: Principal = Depends(require_principal),
    business_slug: str | None = Query(default=None, max_length=64),
    state_filter: str | None = Query(default=None, alias="state",
                                       description="scheduled | fired | cancelled | failed"),
    kind: str | None = Query(default=None, max_length=64),
    from_ts: str | None = Query(default=None, alias="from",
                                  description="ISO timestamp lower bound on scheduled_for"),
    to_ts: str | None = Query(default=None, alias="to",
                                description="ISO timestamp upper bound on scheduled_for"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    require_admin(principal)
    if state_filter is not None and state_filter not in (
        "scheduled", "fired", "cancelled", "failed",
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_state", "got": state_filter},
        )

    pool = request.app.state.db
    async with pool.acquire() as conn:
        biz_id = await _resolve_business_id(conn, business_slug)

        params: list[Any] = []
        where: list[str] = []
        if biz_id:
            params.append(biz_id)
            where.append(f"a.business_id = ${len(params)}::uuid")
        if state_filter:
            params.append(state_filter)
            where.append(f"a.state::text = ${len(params)}")
        if kind:
            params.append(kind)
            where.append(f"a.kind = ${len(params)}")
        if from_ts:
            params.append(from_ts)
            where.append(f"a.scheduled_for >= ${len(params)}::timestamptz")
        if to_ts:
            params.append(to_ts)
            where.append(f"a.scheduled_for <= ${len(params)}::timestamptz")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        params.append(limit)
        limit_p = len(params)
        params.append(offset)
        offset_p = len(params)

        rows = await conn.fetch(
            f"""
            SELECT a.id::text                AS id,
                   a.business_id::text       AS business_id,
                   a.kind                    AS kind,
                   a.sop_id::text            AS sop_id,
                   a.scheduled_for::text     AS scheduled_for,
                   a.state::text             AS state,
                   a.fired_at::text          AS fired_at,
                   a.fired_by::text          AS fired_by,
                   a.cancelled_at::text      AS cancelled_at,
                   a.cancelled_by::text      AS cancelled_by,
                   a.notes                   AS notes,
                   a.created_at::text        AS created_at
            FROM anchor_events a
            {where_sql}
            ORDER BY a.scheduled_for DESC
            LIMIT ${limit_p} OFFSET ${offset_p}
            """,
            *params,
        )
        count_params = params[: len(params) - 2]
        count_row = await conn.fetchrow(
            f"SELECT count(*) AS c FROM anchor_events a {where_sql}",
            *count_params,
        )

    return {
        "items": [_serialize_anchor(r) for r in rows],
        "limit": limit,
        "offset": offset,
        "total": int(count_row["c"]),
    }


# ---------------------------------------------------------------------------
# GET /v1/anchor_events/{id}
# ---------------------------------------------------------------------------

@router.get("/v1/anchor_events/{anchor_id}")
async def get_anchor_event(
    anchor_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    require_admin(principal)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id::text                AS id,
                   business_id::text       AS business_id,
                   kind                    AS kind,
                   sop_id::text            AS sop_id,
                   scheduled_for::text     AS scheduled_for,
                   state::text             AS state,
                   fired_at::text          AS fired_at,
                   fired_by::text          AS fired_by,
                   cancelled_at::text      AS cancelled_at,
                   cancelled_by::text      AS cancelled_by,
                   notes                   AS notes,
                   created_at::text        AS created_at
            FROM anchor_events
            WHERE id = $1::uuid
            """,
            str(anchor_id),
        )
    if not row:
        raise HTTPException(
            status_code=404,
            detail={"code": "anchor_event_not_found", "id": str(anchor_id)},
        )
    return {"anchor_event": _serialize_anchor(row)}


# ---------------------------------------------------------------------------
# GET /v1/anchor_events/{id}/instances
# ---------------------------------------------------------------------------

@router.get("/v1/anchor_events/{anchor_id}/instances")
async def list_anchor_instances(
    anchor_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    require_admin(principal)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text              AS id,
                   anchor_event_id::text AS anchor_event_id,
                   sop_version_id::text  AS sop_version_id,
                   ingest_event_id::text AS ingest_event_id,
                   fired_at::text        AS fired_at,
                   fired_by::text        AS fired_by
            FROM sop_instances
            WHERE anchor_event_id = $1::uuid
            ORDER BY fired_at DESC
            LIMIT $2 OFFSET $3
            """,
            str(anchor_id), limit, offset,
        )
        count_row = await conn.fetchrow(
            "SELECT count(*) AS c FROM sop_instances "
            "WHERE anchor_event_id = $1::uuid",
            str(anchor_id),
        )
    return {
        "items": [_serialize_instance(r) for r in rows],
        "limit": limit,
        "offset": offset,
        "total": int(count_row["c"]),
    }


# ---------------------------------------------------------------------------
# GET /v1/sop_instances/{id}
# ---------------------------------------------------------------------------

@router.get("/v1/sop_instances/{instance_id}")
async def get_sop_instance(
    instance_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    require_admin(principal)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        instance_row = await conn.fetchrow(
            """
            SELECT id::text              AS id,
                   anchor_event_id::text AS anchor_event_id,
                   sop_version_id::text  AS sop_version_id,
                   ingest_event_id::text AS ingest_event_id,
                   fired_at::text        AS fired_at,
                   fired_by::text        AS fired_by
            FROM sop_instances
            WHERE id = $1::uuid
            """,
            str(instance_id),
        )
        if not instance_row:
            raise HTTPException(
                status_code=404,
                detail={"code": "sop_instance_not_found", "id": str(instance_id)},
            )
        # Junction rows joined to review_items + tasks for the audit
        # pane the PWA SOPs tab will render.
        gen_rows = await conn.fetch(
            """
            SELECT g.id::text                  AS id,
                   g.sop_instance_id::text     AS sop_instance_id,
                   g.sop_template_task_id::text AS sop_template_task_id,
                   g.review_item_id::text      AS review_item_id,
                   ri.status::text             AS review_item_status,
                   g.task_id::text             AS task_id,
                   t.status::text              AS task_status,
                   t.summary                   AS task_summary,
                   g.manually_overridden_fields AS manually_overridden_fields
            FROM sop_generated_tasks g
            LEFT JOIN review_items ri ON ri.id = g.review_item_id
            LEFT JOIN tasks t        ON t.id = g.task_id
            WHERE g.sop_instance_id = $1::uuid
            ORDER BY g.id
            """,
            instance_row["id"],
        )

    return {
        "instance": _serialize_instance(instance_row),
        "generated_tasks": [_serialize_generated(g) for g in gen_rows],
    }
