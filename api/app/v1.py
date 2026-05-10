"""OpsMemory v1 read API.

Chunk 2 endpoints (read-only):
  GET /v1/businesses          - list businesses visible to caller
  GET /v1/tasks               - list tasks visible to caller (with filters)
  GET /v1/tasks/{task_id}     - single task with assignees, businesses, field versions
  GET /v1/users               - admin-only; list users

Authorization scoping is enforced at the SQL level (admins get
unrestricted queries; owners join through business_memberships ->
task_businesses). The model never returns a row the caller can't see.

Field-version vectors are baked into the task response from day one
(per Codex Chunk 2 plan): clients can use them as the base_field_versions
for offline outbox writes in Chunk 6.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .auth import Principal, require_principal
from .authz import require_admin, visible_business_ids

log = logging.getLogger("opsmemory.v1")

router = APIRouter(prefix="/v1")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fetch_field_versions(conn, task_id: str) -> dict[str, int]:
    rows = await conn.fetch(
        "SELECT field_name, version FROM task_field_versions WHERE task_id = $1::uuid",
        task_id,
    )
    return {r["field_name"]: r["version"] for r in rows}


async def _fetch_assignees(conn, task_id: str) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT u.id::text AS id, u.email::text AS email, u.display_name,
               u.role::text AS user_role, ta.role AS task_role,
               ta.assigned_at::text AS assigned_at
        FROM task_assignees ta
        JOIN users u ON u.id = ta.user_id
        WHERE ta.task_id = $1::uuid
        ORDER BY ta.assigned_at
        """,
        task_id,
    )
    return [dict(r) for r in rows]


async def _fetch_task_businesses(conn, task_id: str) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT b.id::text AS id, b.slug::text AS slug, b.name
        FROM task_businesses tb
        JOIN businesses b ON b.id = tb.business_id
        WHERE tb.task_id = $1::uuid
        ORDER BY b.name
        """,
        task_id,
    )
    return [dict(r) for r in rows]


def _serialize_task(row: dict) -> dict:
    return {
        "id": row["id"],
        "summary": row["summary"],
        "description": row.get("description"),
        "status": row["status"],
        "due_at": row.get("due_at"),
        "category": row.get("category"),
        "priority": row.get("priority"),
        "dependency_task_id": row.get("dependency_task_id"),
        "dependency_text": row.get("dependency_text"),
        "completed_at": row.get("completed_at"),
        "completed_by": row.get("completed_by"),
        "completion_note": row.get("completion_note"),
        "last_activity_at": row.get("last_activity_at"),
        "version": row["version"],
        "deletion_state": row["deletion_state"],
        "deleted_at": row.get("deleted_at"),
        "superseded_by_task_id": row.get("superseded_by_task_id"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/businesses")
async def list_businesses(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    pool = request.app.state.db
    visible = visible_business_ids(principal)

    async with pool.acquire() as conn:
        if visible is None:
            rows = await conn.fetch(
                """
                SELECT id::text AS id, slug::text AS slug, name,
                       deletion_state::text AS deletion_state,
                       created_at::text AS created_at
                FROM businesses
                WHERE deletion_state = 'active'
                ORDER BY name
                """
            )
        elif not visible:
            return {"businesses": []}
        else:
            rows = await conn.fetch(
                """
                SELECT id::text AS id, slug::text AS slug, name,
                       deletion_state::text AS deletion_state,
                       created_at::text AS created_at
                FROM businesses
                WHERE deletion_state = 'active' AND id::text = ANY($1::text[])
                ORDER BY name
                """,
                visible,
            )

    return {"businesses": [dict(r) for r in rows]}


@router.get("/businesses/{slug}/members")
async def list_business_members(
    slug: str,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Return active members of a business — used for the Quick Add
    assignee dropdown.

    Authz: any authenticated principal whose visible_business_ids
    includes this business slug. Owners scoped to their memberships;
    admins unrestricted; services 403 (Quick Add is human-only).
    """
    if principal.principal_type == "service":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="business members listing is human-only",
        )
    pool = request.app.state.db
    visible = visible_business_ids(principal)
    async with pool.acquire() as conn:
        biz = await conn.fetchrow(
            "SELECT id::text AS id, slug::text AS slug, name "
            "FROM businesses "
            "WHERE slug::text = $1 AND deletion_state = 'active'",
            slug,
        )
        if not biz:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "business_not_found", "slug": slug},
            )
        if visible is not None and biz["id"] not in visible:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "business_not_visible"},
            )
        # Codex B3-2 review: do NOT return u.email here. The endpoint
        # is callable by any business member (not just admin) and the
        # assignee dropdown only needs id + display_name + role.
        # Email is exposed via /v1/users (admin-only).
        rows = await conn.fetch(
            """
            SELECT u.id::text AS id, u.display_name,
                   bm.role::text AS role
              FROM users u
              JOIN business_memberships bm ON bm.user_id = u.id
             WHERE bm.business_id = $1::uuid
               AND bm.status = 'active'
               AND u.status = 'active'
             ORDER BY u.display_name
            """,
            biz["id"],
        )
    return {
        "business": {"slug": biz["slug"], "name": biz["name"]},
        "members": [dict(r) for r in rows],
    }


@router.get("/users")
async def list_users(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    require_admin(principal)
    pool = request.app.state.db
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.id::text AS id, u.email::text AS email, u.display_name,
                   u.role::text AS role, u.status::text AS status,
                   u.timezone, u.last_seen_at::text AS last_seen_at,
                   COALESCE(
                     json_agg(
                       json_build_object('id', b.id::text, 'slug', b.slug::text, 'name', b.name, 'role', bm.role::text)
                     ) FILTER (WHERE b.id IS NOT NULL),
                     '[]'::json
                   ) AS businesses
            FROM users u
            LEFT JOIN business_memberships bm ON bm.user_id = u.id AND bm.status = 'active'
            LEFT JOIN businesses b ON b.id = bm.business_id AND b.deletion_state = 'active'
            GROUP BY u.id
            ORDER BY u.display_name
            """
        )
    return {"users": [dict(r) for r in rows]}


@router.get("/dashboard/summary")
async def dashboard_summary(
    request: Request,
    principal: Principal = Depends(require_principal),
    business_slug: str | None = Query(default=None, max_length=64),
) -> dict:
    """Aggregate metrics for the operator dashboard tiles.

    Phase UI-3: scoped by visible_business_ids (admins unrestricted;
    owners scoped to their memberships). Returns enough to render
    three tiles + a sparkline without follow-up requests:
      - totals: open count, done in last 7d / 30d
      - open_aging: counts in today / 1-3d / 3-7d / 7d+ buckets,
        anchored on last_activity_at so freshly-touched tasks reset
      - by_business: open count per visible business (for the
        per-business bar)
      - spark_daily_done: 14-day daily series of completed-tasks
        count, zero-filled via generate_series so the sparkline has
        a stable x-axis even on quiet days.
    Optional ?business_slug filter scopes everything to one business.
    """
    pool = request.app.state.db
    visible = visible_business_ids(principal)

    # Resolve optional business filter -> intersect with visible.
    # Codex UI-3 R1 blocker: this MUST run before the no-visibility
    # short-circuit, otherwise a service principal (or owner with
    # zero memberships) would get a 200 with zeros instead of the
    # expected 404 (unknown slug) or 403 (slug not visible to them).
    biz_filter_ids: list[str] | None
    if business_slug:
        async with pool.acquire() as conn:
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
        if visible is not None and row["id"] not in visible:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "business_not_visible"},
            )
        biz_filter_ids = [row["id"]]
    else:
        biz_filter_ids = visible  # may be None for platform_admin

    # No-visibility short-circuit AFTER slug resolution.
    if visible is not None and not visible:
        return {
            "totals": {"open": 0, "done_7d": 0, "done_30d": 0},
            "open_aging": {"today": 0, "1_3d": 0, "3_7d": 0, "7d_plus": 0},
            "by_business": [],
            "spark_daily_done": [],
        }

    # When biz_filter_ids is None (platform_admin, no slug filter),
    # we want ALL active businesses. Materialize that explicitly so
    # the SQL below can always filter on a concrete list.
    async with pool.acquire() as conn:
        if biz_filter_ids is None:
            all_biz = await conn.fetch(
                "SELECT id::text AS id FROM businesses WHERE deletion_state = 'active'"
            )
            biz_filter_ids = [r["id"] for r in all_biz]

        # Totals + aging in one pass over the scoped task set.
        totals_row = await conn.fetchrow(
            """
            WITH scoped AS (
              SELECT DISTINCT t.id,
                     t.status::text AS status,
                     t.last_activity_at,
                     t.completed_at
                FROM tasks t
                JOIN task_businesses tb ON tb.task_id = t.id
               WHERE t.deletion_state = 'active'
                 AND tb.business_id::text = ANY($1::text[])
            )
            SELECT
              count(*) FILTER (WHERE status = 'open') AS open_total,
              count(*) FILTER (WHERE status = 'done'
                               AND completed_at >= now() - interval '7 days') AS done_7d,
              count(*) FILTER (WHERE status = 'done'
                               AND completed_at >= now() - interval '30 days') AS done_30d,
              count(*) FILTER (WHERE status = 'open'
                               AND last_activity_at >= now() - interval '24 hours') AS age_today,
              count(*) FILTER (WHERE status = 'open'
                               AND last_activity_at <  now() - interval '24 hours'
                               AND last_activity_at >= now() - interval '3 days') AS age_1_3d,
              count(*) FILTER (WHERE status = 'open'
                               AND last_activity_at <  now() - interval '3 days'
                               AND last_activity_at >= now() - interval '7 days') AS age_3_7d,
              count(*) FILTER (WHERE status = 'open'
                               AND last_activity_at <  now() - interval '7 days') AS age_7d_plus
            FROM scoped
            """,
            biz_filter_ids,
        )

        # Per-business open counts (only the businesses in scope).
        by_biz_rows = await conn.fetch(
            """
            SELECT b.slug::text AS slug, b.name AS name,
                   count(DISTINCT t.id)
                     FILTER (WHERE t.status = 'open') AS open_count
              FROM businesses b
              LEFT JOIN task_businesses tb ON tb.business_id = b.id
              LEFT JOIN tasks t ON t.id = tb.task_id
                                AND t.deletion_state = 'active'
             WHERE b.id::text = ANY($1::text[])
               AND b.deletion_state = 'active'
             GROUP BY b.id, b.slug, b.name
             ORDER BY b.name
            """,
            biz_filter_ids,
        )

        # Sparkline: completed tasks per day for the last 14 days,
        # zero-filled via generate_series. Day boundaries in UTC; the
        # PWA can re-bucket to local TZ if it cares (today's tile
        # uses the UTC day number which is close-enough for ops at
        # this scale).
        spark_rows = await conn.fetch(
            """
            WITH days AS (
              SELECT generate_series(
                       date_trunc('day', now() - interval '13 days'),
                       date_trunc('day', now()),
                       interval '1 day'
                     ) AS day
            ),
            done AS (
              SELECT date_trunc('day', t.completed_at) AS day,
                     count(DISTINCT t.id) AS c
                FROM tasks t
                JOIN task_businesses tb ON tb.task_id = t.id
               WHERE t.deletion_state = 'active'
                 AND t.status = 'done'
                 AND t.completed_at >= date_trunc('day', now() - interval '13 days')
                 AND tb.business_id::text = ANY($1::text[])
               GROUP BY 1
            )
            SELECT to_char(d.day, 'YYYY-MM-DD') AS day,
                   COALESCE(done.c, 0) AS count
              FROM days d
              LEFT JOIN done ON done.day = d.day
             ORDER BY d.day
            """,
            biz_filter_ids,
        )

    return {
        "totals": {
            "open": int(totals_row["open_total"] or 0),
            "done_7d": int(totals_row["done_7d"] or 0),
            "done_30d": int(totals_row["done_30d"] or 0),
        },
        "open_aging": {
            "today": int(totals_row["age_today"] or 0),
            "1_3d": int(totals_row["age_1_3d"] or 0),
            "3_7d": int(totals_row["age_3_7d"] or 0),
            "7d_plus": int(totals_row["age_7d_plus"] or 0),
        },
        "by_business": [
            {"slug": r["slug"], "name": r["name"],
             "open": int(r["open_count"] or 0)}
            for r in by_biz_rows
        ],
        "spark_daily_done": [
            {"day": r["day"], "count": int(r["count"])}
            for r in spark_rows
        ],
    }


@router.get("/tasks")
async def list_tasks(
    request: Request,
    principal: Principal = Depends(require_principal),
    status_filter: str | None = Query(default=None, alias="status",
                                       pattern="^(open|done)$"),
    business_slug: str | None = Query(default=None, max_length=64),
    assigned_to_user_id: UUID | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """List tasks visible to the caller, with optional filters."""
    # Admin-gate include_deleted (Codex chunk-2-close fix). Owners must not
    # see soft-deleted tasks even within their own businesses.
    # MT-2: include_deleted is platform-admin-only. No 'admin' alias.
    if include_deleted and not (principal.principal_type == "user"
                                and principal.role == "platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="include_deleted requires platform admin role",
        )

    pool = request.app.state.db
    visible = visible_business_ids(principal)
    if visible is not None and not visible:
        return {"tasks": [], "total": 0, "limit": limit, "offset": offset}

    where: list[str] = []
    where_params: list[Any] = []  # only the WHERE-clause binds; reused by count_sql
    pidx = 0

    def add_param(value: Any) -> str:
        nonlocal pidx
        pidx += 1
        where_params.append(value)
        return f"${pidx}"

    if not include_deleted:
        where.append("t.deletion_state = 'active'")

    if status_filter:
        where.append(f"t.status = {add_param(status_filter)}::task_lifecycle_state")

    if business_slug:
        where.append(
            "EXISTS (SELECT 1 FROM task_businesses tb JOIN businesses b ON b.id = tb.business_id "
            f"WHERE tb.task_id = t.id AND b.slug = {add_param(business_slug)}::citext)"
        )

    if assigned_to_user_id is not None:
        where.append(
            f"EXISTS (SELECT 1 FROM task_assignees ta WHERE ta.task_id = t.id "
            f"AND ta.user_id = {add_param(str(assigned_to_user_id))}::uuid)"
        )

    if visible is not None:
        # Owner scoping: task must be linked to at least one of the
        # caller's visible businesses.
        where.append(
            "EXISTS (SELECT 1 FROM task_businesses tb WHERE tb.task_id = t.id "
            f"AND tb.business_id::text = ANY({add_param(visible)}::text[]))"
        )

    where_clause = "WHERE " + " AND ".join(where) if where else ""

    # The list query has its own LIMIT/OFFSET binds; the count query uses
    # only the WHERE binds. Snapshot the WHERE binds BEFORE adding the
    # LIMIT/OFFSET binds so the count query references the right slice
    # (Codex chunk-2-close fix: previously used `params[:-2]` which was
    # brittle to future param-order changes).
    count_params = list(where_params)
    list_params = list(where_params)
    list_params.append(limit)
    limit_idx = pidx + 1
    list_params.append(offset)
    offset_idx = pidx + 2

    sql = f"""
        SELECT t.id::text AS id, t.summary, t.description,
               t.status::text AS status, t.due_at::text AS due_at,
               t.category, t.priority,
               t.dependency_task_id::text AS dependency_task_id,
               t.dependency_text,
               t.completed_at::text AS completed_at,
               t.completed_by::text AS completed_by,
               t.completion_note,
               t.last_activity_at::text AS last_activity_at,
               t.version,
               t.deletion_state::text AS deletion_state,
               t.deleted_at::text AS deleted_at,
               t.superseded_by_task_id::text AS superseded_by_task_id,
               t.created_at::text AS created_at,
               t.updated_at::text AS updated_at
        FROM tasks t
        {where_clause}
        ORDER BY t.last_activity_at DESC NULLS LAST
        LIMIT ${limit_idx} OFFSET ${offset_idx}
    """
    count_sql = f"SELECT count(*) FROM tasks t {where_clause}"

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *list_params)
        total = await conn.fetchval(count_sql, *count_params)

    tasks = [_serialize_task(dict(r)) for r in rows]
    return {"tasks": tasks, "total": total, "limit": limit, "offset": offset}


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
    include_deleted: bool = Query(default=False),
) -> dict:
    # Admin-gate include_deleted on detail too (Codex chunk-2-close fix).
    # MT-2: include_deleted is platform-admin-only. No 'admin' alias.
    if include_deleted and not (principal.principal_type == "user"
                                and principal.role == "platform_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="include_deleted requires platform admin role",
        )

    task_id_str = str(task_id)
    pool = request.app.state.db
    visible = visible_business_ids(principal)
    if visible is not None and not visible:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")

    # Active filter unless admin opts in — owner-visible tasks must hide
    # the trash to match list semantics.
    active_clause = "" if include_deleted else " AND t.deletion_state = 'active'"

    async with pool.acquire() as conn:
        if visible is None:
            row = await conn.fetchrow(
                f"""
                SELECT t.id::text AS id, t.summary, t.description,
                       t.status::text AS status, t.due_at::text AS due_at,
                       t.category, t.priority,
                       t.dependency_task_id::text AS dependency_task_id,
                       t.dependency_text,
                       t.completed_at::text AS completed_at,
                       t.completed_by::text AS completed_by,
                       t.completion_note,
                       t.last_activity_at::text AS last_activity_at,
                       t.version,
                       t.deletion_state::text AS deletion_state,
                       t.deleted_at::text AS deleted_at,
                       t.superseded_by_task_id::text AS superseded_by_task_id,
                       t.created_at::text AS created_at,
                       t.updated_at::text AS updated_at
                FROM tasks t
                WHERE t.id = $1::uuid{active_clause}
                """,
                task_id_str,
            )
        else:
            row = await conn.fetchrow(
                f"""
                SELECT t.id::text AS id, t.summary, t.description,
                       t.status::text AS status, t.due_at::text AS due_at,
                       t.category, t.priority,
                       t.dependency_task_id::text AS dependency_task_id,
                       t.dependency_text,
                       t.completed_at::text AS completed_at,
                       t.completed_by::text AS completed_by,
                       t.completion_note,
                       t.last_activity_at::text AS last_activity_at,
                       t.version,
                       t.deletion_state::text AS deletion_state,
                       t.deleted_at::text AS deleted_at,
                       t.superseded_by_task_id::text AS superseded_by_task_id,
                       t.created_at::text AS created_at,
                       t.updated_at::text AS updated_at
                FROM tasks t
                WHERE t.id = $1::uuid{active_clause}
                  AND EXISTS (
                    SELECT 1 FROM task_businesses tb
                    WHERE tb.task_id = t.id
                      AND tb.business_id::text = ANY($2::text[])
                  )
                """,
                task_id_str,
                visible,
            )

        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")

        task = _serialize_task(dict(row))
        task["assignees"] = await _fetch_assignees(conn, task_id_str)
        task["businesses"] = await _fetch_task_businesses(conn, task_id_str)
        task["field_versions"] = await _fetch_field_versions(conn, task_id_str)

    return {"task": task}
