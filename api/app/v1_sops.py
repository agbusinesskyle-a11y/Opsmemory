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
from datetime import datetime
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, Field

from .auth import Principal, require_principal
from .authz import require_admin

log = logging.getLogger("opsmemory.v1_sops")

router = APIRouter()


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# Mirror the schema enums (api/migrations/0009_sops.sql) so a typo in
# either side surfaces here, not at runtime.
SOP_STATUSES = frozenset({"active", "archived"})
ANCHOR_EVENT_STATES = frozenset({"scheduled", "fired", "cancelled", "failed"})


def _parse_iso_timestamp(raw: str | None, *, field: str) -> datetime | None:
    """Parse an ISO-8601 timestamp from a query string. None passes
    through. Normalizes 'Z' -> '+00:00' (Python 3.11+ accepts both,
    but older fromisoformat doesn't, and being explicit makes the
    error path predictable)."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_timestamp", "field": field, "got": raw},
        )
    # Reject naive timestamps — anchor scheduled_for is timestamptz
    # and a timezone-less filter would silently use the DB session
    # timezone, which produces nondeterministic results across
    # operator clients.
    if dt.tzinfo is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "naive_timestamp", "field": field,
                    "detail": "include a timezone offset (e.g. 'Z' or '+00:00')"},
        )
    return dt


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
        "template_seq_no": row.get("template_seq_no"),
        "template_summary": row.get("template_summary"),
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
    if sop_status is not None and sop_status not in SOP_STATUSES:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_status", "got": sop_status,
                    "allowed": sorted(SOP_STATUSES)},
        )

    pool = request.app.state.db
    async with pool.acquire() as conn:
        biz_id = await _resolve_business_id(conn, business_slug)

        # Separate where_params from list_params (limit/offset) per
        # Codex chunk-7-step2 review — the params[:-2] slice trick
        # in v1.py was already noted as brittle.
        where_params: list[Any] = []
        where: list[str] = []
        if biz_id:
            where_params.append(biz_id)
            where.append(f"s.business_id = ${len(where_params)}::uuid")
        if sop_status:
            where_params.append(sop_status)
            where.append(f"s.status::text = ${len(where_params)}")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        list_params = where_params + [limit, offset]
        limit_p = len(where_params) + 1
        offset_p = len(where_params) + 2

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
            ORDER BY s.business_id, s.name, s.id
            LIMIT ${limit_p} OFFSET ${offset_p}
            """,
            *list_params,
        )
        count_row = await conn.fetchrow(
            f"SELECT count(*) AS c FROM sops s {where_sql}",
            *where_params,
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
    version_no: Annotated[int, Path(ge=1)],
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    require_admin(principal)

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
    if state_filter is not None and state_filter not in ANCHOR_EVENT_STATES:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_state", "got": state_filter,
                    "allowed": sorted(ANCHOR_EVENT_STATES)},
        )

    # Parse timestamps in Python so a malformed value returns a clean
    # 400 instead of bubbling PG 22007 as a 500 (Codex chunk-7-step2
    # blocker). Also enforces tz-aware semantics so the filter is
    # deterministic across operator timezones.
    from_dt = _parse_iso_timestamp(from_ts, field="from")
    to_dt = _parse_iso_timestamp(to_ts, field="to")
    if from_dt is not None and to_dt is not None and from_dt > to_dt:
        raise HTTPException(
            status_code=400,
            detail={"code": "timestamp_range_invalid",
                    "detail": "'from' must be <= 'to'",
                    "from": from_ts, "to": to_ts},
        )

    pool = request.app.state.db
    async with pool.acquire() as conn:
        biz_id = await _resolve_business_id(conn, business_slug)

        where_params: list[Any] = []
        where: list[str] = []
        if biz_id:
            where_params.append(biz_id)
            where.append(f"a.business_id = ${len(where_params)}::uuid")
        if state_filter:
            where_params.append(state_filter)
            where.append(f"a.state::text = ${len(where_params)}")
        if kind:
            where_params.append(kind)
            where.append(f"a.kind = ${len(where_params)}")
        if from_dt is not None:
            where_params.append(from_dt)
            where.append(f"a.scheduled_for >= ${len(where_params)}::timestamptz")
        if to_dt is not None:
            where_params.append(to_dt)
            where.append(f"a.scheduled_for <= ${len(where_params)}::timestamptz")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        list_params = where_params + [limit, offset]
        limit_p = len(where_params) + 1
        offset_p = len(where_params) + 2

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
            ORDER BY a.scheduled_for DESC, a.id
            LIMIT ${limit_p} OFFSET ${offset_p}
            """,
            *list_params,
        )
        count_row = await conn.fetchrow(
            f"SELECT count(*) AS c FROM anchor_events a {where_sql}",
            *where_params,
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
            ORDER BY fired_at DESC, id
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
        # Junction rows joined to template + review_items + tasks for
        # the audit pane the PWA SOPs tab will render. Ordered by
        # template seq_no so the reviewer sees materialized tasks in
        # the SOP author's intended order even before they're
        # approved into real tasks (Codex chunk-7-step2 fix).
        gen_rows = await conn.fetch(
            """
            SELECT g.id::text                  AS id,
                   g.sop_instance_id::text     AS sop_instance_id,
                   g.sop_template_task_id::text AS sop_template_task_id,
                   st.seq_no                   AS template_seq_no,
                   st.summary                  AS template_summary,
                   g.review_item_id::text      AS review_item_id,
                   ri.status::text             AS review_item_status,
                   g.task_id::text             AS task_id,
                   t.status::text              AS task_status,
                   t.summary                   AS task_summary,
                   g.manually_overridden_fields AS manually_overridden_fields
            FROM sop_generated_tasks g
            JOIN sop_template_tasks st ON st.id = g.sop_template_task_id
            LEFT JOIN review_items ri  ON ri.id = g.review_item_id
            LEFT JOIN tasks t          ON t.id = g.task_id
            WHERE g.sop_instance_id = $1::uuid
            ORDER BY st.seq_no, g.created_at, g.id
            """,
            instance_row["id"],
        )

    return {
        "instance": _serialize_instance(instance_row),
        "generated_tasks": [_serialize_generated(g) for g in gen_rows],
    }


# ===========================================================================
# Write endpoints (Chunk 7 step 3b — admin-only)
# ===========================================================================
#
# Workflow:
#   POST /v1/sops                          create an SOP record (no version)
#   POST /v1/sops/{id}/versions            create a draft version (auto-allocates
#                                           version_no = max+1)
#   PATCH /v1/sops/{id}/versions/{vn}/templates
#                                           replace ALL templates of a draft
#                                           atomically (cleaner than per-row
#                                           CRUD for this use case)
#   POST /v1/sops/{id}/versions/{vn}/publish
#                                           atomic state='published' + bump
#                                           sops.latest_version_id +
#                                           supersede prior published version
#   POST /v1/anchor_events                  schedule an anchor
#
# Schema-side guardrails (migration 0010):
#   - sop_versions immutability trigger blocks state regression and
#     content edits post-publish.
#   - sop_template_tasks trigger blocks any mutation when parent != draft.
#   - sops.latest_version_id trigger requires state='published' + same sop.
#   - Partial unique indexes prevent two drafts or two published per sop.

# ---------------------------------------------------------------------------
# Pydantic bodies
# ---------------------------------------------------------------------------

class _CreateSopBody(BaseModel):
    model_config = {"extra": "forbid"}
    business_slug: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=8192)


class _CreateSopVersionBody(BaseModel):
    model_config = {"extra": "forbid"}
    change_log: str | None = Field(default=None, max_length=8192)


class _TemplateBody(BaseModel):
    model_config = {"extra": "forbid"}
    summary: str = Field(..., min_length=1, max_length=4096)
    description: str | None = Field(default=None, max_length=8192)
    due_offset_days: int | None = Field(default=None, ge=-3650, le=3650)
    dependency_text: str | None = Field(default=None, max_length=2048)
    category: str | None = Field(default=None, max_length=64)
    priority: str | None = Field(default=None, max_length=32)
    owner_role: str | None = Field(default=None, max_length=64)
    owner_user_id: uuid.UUID | None = None


class _ReplaceTemplatesBody(BaseModel):
    model_config = {"extra": "forbid"}
    templates: list[_TemplateBody] = Field(..., min_length=0, max_length=200)


class _PublishVersionBody(BaseModel):
    model_config = {"extra": "forbid"}
    change_log: str | None = Field(default=None, max_length=8192)


class _CreateAnchorBody(BaseModel):
    model_config = {"extra": "forbid"}
    business_slug: str = Field(..., min_length=1, max_length=64)
    kind: str = Field(..., min_length=1, max_length=64)
    sop_id: uuid.UUID
    scheduled_for: str = Field(..., min_length=1, max_length=64,
                                 description="ISO-8601 timestamp with timezone (Z or offset).")
    notes: str | None = Field(default=None, max_length=4096)


# ---------------------------------------------------------------------------
# POST /v1/sops
# ---------------------------------------------------------------------------

@router.post("/v1/sops", status_code=201)
async def create_sop(
    body: _CreateSopBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    require_admin(principal)
    pool = request.app.state.db
    actor_id = principal.id if principal.principal_type == "user" else None
    async with pool.acquire() as conn:
        biz_id = await _resolve_business_id(conn, body.business_slug)
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO sops (business_id, name, description, created_by, updated_by)
                VALUES ($1::uuid, $2, $3, $4::uuid, $4::uuid)
                RETURNING id::text AS id,
                          business_id::text AS business_id,
                          name AS name,
                          description AS description,
                          status::text AS status,
                          latest_version_id::text AS latest_version_id,
                          created_at::text AS created_at,
                          updated_at::text AS updated_at
                """,
                biz_id, body.name, body.description, actor_id,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail={"code": "sop_name_in_use",
                        "detail": (f"an active SOP named {body.name!r} already "
                                    f"exists in business {body.business_slug!r}")},
            )
    log.info("sop_created", extra={"sop_id": row["id"], "actor": actor_id})
    return {**_serialize_sop(row), "latest_version_no": None}


# ---------------------------------------------------------------------------
# POST /v1/sops/{id}/versions
# ---------------------------------------------------------------------------

@router.post("/v1/sops/{sop_id}/versions", status_code=201)
async def create_sop_version(
    sop_id: uuid.UUID,
    body: _CreateSopVersionBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Create a new draft version. Server allocates version_no = max+1.

    A SOP may have at most one outstanding draft (enforced by the
    partial unique index from migration 0010). A second concurrent
    create returns 409 sop_draft_exists.
    """
    require_admin(principal)
    pool = request.app.state.db
    actor_id = principal.id if principal.principal_type == "user" else None
    rid = str(sop_id)

    async with pool.acquire() as conn:
        async with conn.transaction():
            sop = await conn.fetchrow(
                "SELECT id::text AS id, status::text AS status "
                "FROM sops WHERE id = $1::uuid FOR UPDATE",
                rid,
            )
            if not sop:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "sop_not_found", "id": rid},
                )
            if sop["status"] != "active":
                raise HTTPException(
                    status_code=409,
                    detail={"code": "sop_archived", "id": rid,
                            "detail": "cannot add versions to an archived SOP"},
                )
            next_no_row = await conn.fetchrow(
                "SELECT COALESCE(MAX(version_no), 0) + 1 AS n "
                "FROM sop_versions WHERE sop_id = $1::uuid",
                rid,
            )
            next_no = int(next_no_row["n"])
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO sop_versions
                      (sop_id, version_no, state, change_log, created_by, updated_by)
                    VALUES ($1::uuid, $2, 'draft', $3, $4::uuid, $4::uuid)
                    RETURNING id::text AS id,
                              sop_id::text AS sop_id,
                              version_no AS version_no,
                              state::text AS state,
                              change_log AS change_log,
                              created_at::text AS created_at,
                              published_at::text AS published_at,
                              published_by::text AS published_by
                    """,
                    rid, next_no, body.change_log, actor_id,
                )
            except asyncpg.UniqueViolationError:
                # Either (sop_id, version_no) collided (impossible — we
                # just allocated max+1 inside the row lock) OR the
                # one-draft-per-sop partial unique tripped.
                raise HTTPException(
                    status_code=409,
                    detail={"code": "sop_draft_exists",
                            "detail": ("this SOP already has an outstanding "
                                       "draft; publish or delete it before "
                                       "creating another")},
                )
    log.info("sop_version_created", extra={
        "sop_id": rid, "version_no": next_no, "actor": actor_id,
    })
    return _serialize_version(row)


# ---------------------------------------------------------------------------
# PATCH /v1/sops/{id}/versions/{vn}/templates
# ---------------------------------------------------------------------------

@router.patch("/v1/sops/{sop_id}/versions/{version_no}/templates")
async def replace_sop_templates(
    sop_id: uuid.UUID,
    version_no: Annotated[int, Path(ge=1)],
    body: _ReplaceTemplatesBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Replace ALL templates of a draft version atomically.

    Per-row CRUD would multiply round trips and complicate ordering.
    The reorder + add + remove cases all collapse into "send the new
    full list". seq_no is the array index.

    Schema trigger blocks all mutations when parent != draft, so
    publishing freezes templates automatically.
    """
    require_admin(principal)
    pool = request.app.state.db
    rid = str(sop_id)

    async with pool.acquire() as conn:
        async with conn.transaction():
            version = await conn.fetchrow(
                "SELECT id::text AS id, sop_id::text AS sop_id, "
                "       state::text AS state "
                "FROM sop_versions "
                "WHERE sop_id = $1::uuid AND version_no = $2 FOR UPDATE",
                rid, version_no,
            )
            if not version:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "sop_version_not_found",
                            "sop_id": rid, "version_no": version_no},
                )
            if version["state"] != "draft":
                raise HTTPException(
                    status_code=409,
                    detail={"code": "sop_version_not_draft",
                            "state": version["state"],
                            "detail": "templates can only be edited while the version is draft"},
                )

            # Delete-all + reinsert. Cheaper than diffing for a list
            # bounded at 200 templates. The schema trigger
            # sop_template_tasks_draft_only_check passes because we're
            # still in 'draft' state.
            await conn.execute(
                "DELETE FROM sop_template_tasks WHERE sop_version_id = $1::uuid",
                version["id"],
            )
            for idx, t in enumerate(body.templates):
                owner_uid = str(t.owner_user_id) if t.owner_user_id else None
                await conn.execute(
                    """
                    INSERT INTO sop_template_tasks
                      (sop_version_id, seq_no, summary, description,
                       due_offset_days, dependency_text, category, priority,
                       owner_role, owner_user_id)
                    VALUES
                      ($1::uuid, $2, $3, $4,
                       $5, $6, $7, $8,
                       $9, $10::uuid)
                    """,
                    version["id"], idx, t.summary, t.description,
                    t.due_offset_days, t.dependency_text, t.category, t.priority,
                    t.owner_role, owner_uid,
                )

            # Touch the version row so its updated_at reflects this edit.
            actor_id = principal.id if principal.principal_type == "user" else None
            await conn.execute(
                "UPDATE sop_versions SET updated_by = $2::uuid "
                "WHERE id = $1::uuid",
                version["id"], actor_id,
            )

    log.info("sop_templates_replaced", extra={
        "sop_id": rid, "version_no": version_no,
        "template_count": len(body.templates),
    })
    return {
        "sop_id": rid,
        "version_no": version_no,
        "template_count": len(body.templates),
    }


# ---------------------------------------------------------------------------
# POST /v1/sops/{id}/versions/{vn}/publish
# ---------------------------------------------------------------------------

@router.post("/v1/sops/{sop_id}/versions/{version_no}/publish")
async def publish_sop_version(
    sop_id: uuid.UUID,
    version_no: Annotated[int, Path(ge=1)],
    body: _PublishVersionBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Atomic publish of a draft version.

    Single transaction:
      1. Lock the sop FOR UPDATE.
      2. Lock target version FOR UPDATE; require state='draft' AND
         the SOP has at least one template (an empty SOP wouldn't
         materialize anything useful on fire — refuse).
      3. If there's an existing 'published' version, supersede it
         (state='superseded'). The schema trigger guards content
         immutability during this transition.
      4. Set target version state='published' with published_at +
         published_by + (optional) change_log override.
      5. UPDATE sops.latest_version_id to the newly published version.

    Schema triggers from migration 0010 enforce all the invariants
    the application code can't see — once published, version content
    is immutable; only one published per sop; latest_version_id must
    reference a published version of THIS sop.
    """
    require_admin(principal)
    pool = request.app.state.db
    actor_id = principal.id if principal.principal_type == "user" else None
    rid = str(sop_id)

    async with pool.acquire() as conn:
        async with conn.transaction():
            sop = await conn.fetchrow(
                "SELECT id::text AS id, status::text AS status "
                "FROM sops WHERE id = $1::uuid FOR UPDATE",
                rid,
            )
            if not sop:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "sop_not_found", "id": rid},
                )
            if sop["status"] != "active":
                raise HTTPException(
                    status_code=409,
                    detail={"code": "sop_archived", "id": rid},
                )

            target = await conn.fetchrow(
                """
                SELECT id::text AS id, state::text AS state, change_log
                FROM sop_versions
                WHERE sop_id = $1::uuid AND version_no = $2
                FOR UPDATE
                """,
                rid, version_no,
            )
            if not target:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "sop_version_not_found",
                            "sop_id": rid, "version_no": version_no},
                )
            if target["state"] != "draft":
                raise HTTPException(
                    status_code=409,
                    detail={"code": "sop_version_not_draft",
                            "state": target["state"],
                            "detail": "only draft versions can be published"},
                )

            template_count_row = await conn.fetchrow(
                "SELECT count(*) AS c FROM sop_template_tasks "
                "WHERE sop_version_id = $1::uuid",
                target["id"],
            )
            if int(template_count_row["c"]) == 0:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "sop_version_empty",
                            "detail": "cannot publish a version with no templates"},
                )

            # Supersede any prior published version. The schema
            # trigger ensures only state + updated_at/by change.
            await conn.execute(
                """
                UPDATE sop_versions
                   SET state = 'superseded', updated_by = $2::uuid
                 WHERE sop_id = $1::uuid AND state = 'published'
                """,
                rid, actor_id,
            )

            # Publish target. Use the body's change_log if provided,
            # else keep the draft's existing change_log.
            new_change_log = body.change_log if body.change_log is not None else target["change_log"]
            published = await conn.fetchrow(
                """
                UPDATE sop_versions
                   SET state         = 'published',
                       published_at  = now(),
                       published_by  = $2::uuid,
                       change_log    = $3,
                       updated_by    = $2::uuid
                 WHERE id = $1::uuid
                RETURNING id::text AS id,
                          sop_id::text AS sop_id,
                          version_no AS version_no,
                          state::text AS state,
                          change_log AS change_log,
                          created_at::text AS created_at,
                          published_at::text AS published_at,
                          published_by::text AS published_by
                """,
                target["id"], actor_id, new_change_log,
            )

            # Bump sops.latest_version_id. Trigger requires
            # state='published' for this column; we just satisfied it.
            await conn.execute(
                "UPDATE sops SET latest_version_id = $2::uuid, updated_by = $3::uuid "
                "WHERE id = $1::uuid",
                rid, target["id"], actor_id,
            )

    log.info("sop_version_published", extra={
        "sop_id": rid, "version_no": version_no, "actor": actor_id,
    })
    return _serialize_version(published)


# ---------------------------------------------------------------------------
# POST /v1/anchor_events
# ---------------------------------------------------------------------------

@router.post("/v1/anchor_events", status_code=201)
async def create_anchor_event(
    body: _CreateAnchorBody,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Schedule an anchor event for a SOP.

    The schema trigger anchor_events_business_match_check enforces
    that the anchor's business_id matches the SOP's business_id, so
    a typo or hostile body can't pair business A with business B's
    SOP.
    """
    require_admin(principal)

    scheduled_for_dt = _parse_iso_timestamp(body.scheduled_for, field="scheduled_for")
    if scheduled_for_dt is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "scheduled_for_required",
                    "detail": "scheduled_for must be an ISO timestamp"},
        )

    pool = request.app.state.db
    actor_id = principal.id if principal.principal_type == "user" else None

    async with pool.acquire() as conn:
        biz_id = await _resolve_business_id(conn, body.business_slug)
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO anchor_events
                  (business_id, kind, sop_id, scheduled_for, notes,
                   created_by, updated_by)
                VALUES
                  ($1::uuid, $2, $3::uuid, $4::timestamptz, $5,
                   $6::uuid, $6::uuid)
                RETURNING id::text                AS id,
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
                """,
                biz_id, body.kind, str(body.sop_id), scheduled_for_dt,
                body.notes, actor_id,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail={"code": "anchor_in_use",
                        "detail": (f"anchor for ({body.business_slug}, "
                                    f"{body.kind!r}, {body.scheduled_for}) already "
                                    f"exists; create a distinct kind or schedule")},
            )
        except asyncpg.ForeignKeyViolationError as e:
            # Trigger raises foreign_key_violation when business <> sop.business.
            raise HTTPException(
                status_code=409,
                detail={"code": "anchor_business_mismatch",
                        "detail": str(e)},
            )

    log.info("anchor_event_created", extra={"id": row["id"], "actor": actor_id})
    return _serialize_anchor(row)

