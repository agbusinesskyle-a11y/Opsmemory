"""OpsMemory v1 ingest API.

Entry points where external sources (meeting recaps, future Slack, email,
file drops) hand raw input to the reconciliation pipeline.

Chunk 3 surface (this file): just the meeting-recap entry point.
  POST /v1/ingest/meeting_recap

Records an `ingest_events` row, dedupes by content hash + per-source
external id, returns the event_id. Pipeline workers (extract -> normalize
-> retrieve -> choose -> validate -> queue review) run separately and
populate `review_items` / `llm_calls` rows linked to this event.

Auth:
  - User principals (admin or owner) may POST a recap (e.g., paste from
    the dashboard).
  - Service principals must have the `ingest:write` scope.

Idempotency:
  - meeting_recap: normalized_hash is unique within the source via a
    partial UNIQUE index (migration 0006). Re-posting identical recap
    text returns the existing event_id with deduped=true.
  - source_external_id (when supplied) is unique per source. Posting the
    same Slack message ts twice returns the existing event.
  - Slack does NOT participate in hash uniqueness (Codex chunk-5-step1
    review: short repeated messages — "ok", "+1", "lgtm" — across
    channels are legitimately distinct events). Slack idempotency is
    by source_external_id alone.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .auth import Principal, require_principal
from .authz import SCOPE_INGEST_WRITE, require_scope

log = logging.getLogger("opsmemory.v1_ingest")

router = APIRouter(prefix="/v1/ingest")


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class MeetingRecapIngest(BaseModel):
    model_config = {"extra": "forbid"}

    content: str = Field(..., min_length=10, max_length=200_000,
                         description="Raw meeting recap text. UTF-8.")
    source_external_id: str | None = Field(
        default=None, max_length=256,
        description="Provider-given id for idempotency (e.g., note-taker session id)."
    )
    source_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form context (note-taker name, meeting date, attendees)."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WS_RUN = re.compile(r"[ \t]+")


def canonicalize(content: str) -> str:
    """Normalize line endings + collapse runs of horizontal whitespace.

    Conservative — keeps the original semantics (paragraphs, bullets,
    numbers) but produces a stable bytes-form for SHA-256.
    """
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse trailing whitespace per line (don't change line counts)
    text = "\n".join(_WS_RUN.sub(" ", line.rstrip()) for line in text.split("\n"))
    return text.strip("\n")


def content_hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/meeting_recap")
async def ingest_meeting_recap(
    body: MeetingRecapIngest,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Record a meeting recap as an ingest_event. Idempotent.

    Pipeline workers process pending events asynchronously; this endpoint
    just persists the input + returns the event id.
    """
    # User principals (admin or owner) may post directly. Service
    # principals must carry ingest:write scope.
    if principal.principal_type == "service":
        require_scope(principal, SCOPE_INGEST_WRITE)
    elif principal.principal_type != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ingest requires user or service principal",
        )

    canonical = canonicalize(body.content)
    if len(canonical) < 10:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="content too short after canonicalization",
        )
    hash_hex = content_hash(canonical)

    if principal.principal_type == "user":
        actor_type = "user"
        actor_user_id = principal.id
        actor_service_id = None
    else:
        actor_type = "service"
        actor_user_id = None
        actor_service_id = principal.id

    request_id = getattr(request.state, "request_id", None)

    pool = request.app.state.db
    async with pool.acquire() as conn:
        # First check: dedup by (source, content_hash). Migration 0005
        # made the unique index source-scoped per Codex chunk-4-close
        # review (Slack messages can legitimately repeat across
        # channels, so a global hash UNIQUE was wrong).
        existing = await conn.fetchrow(
            "SELECT id::text AS id, status::text AS status FROM ingest_events "
            "WHERE source = 'meeting_recap' AND normalized_hash = $1",
            hash_hex,
        )
        if existing:
            return {
                "event_id": existing["id"],
                "status": existing["status"],
                "deduped": True,
                "dedup_key": "content_hash",
            }

        # Second check: dedup by (source, source_external_id) when set.
        if body.source_external_id:
            existing_ext = await conn.fetchrow(
                "SELECT id::text AS id, status::text AS status FROM ingest_events "
                "WHERE source = 'meeting_recap' AND source_external_id = $1",
                body.source_external_id,
            )
            if existing_ext:
                return {
                    "event_id": existing_ext["id"],
                    "status": existing_ext["status"],
                    "deduped": True,
                    "dedup_key": "source_external_id",
                }

        # Insert new event. Pipeline picks it up later by status='received'.
        # Race: another concurrent request may insert the same content_hash
        # or (source, source_external_id) between our pre-checks above and
        # this INSERT. Catch the unique violation and re-select the
        # winning row instead of returning a 500.
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO ingest_events
                  (source, source_external_id, raw_content, normalized_hash,
                   source_metadata, status,
                   actor_type, actor_user_id, actor_service_account_id,
                   request_id)
                VALUES
                  ('meeting_recap', $1, $2, $3, $4::jsonb, 'received',
                   $5, $6::uuid, $7::uuid, $8)
                RETURNING id::text AS id, status::text AS status
                """,
                body.source_external_id,
                canonical,
                hash_hex,
                body.source_metadata,
                actor_type,
                actor_user_id,
                actor_service_id,
                request_id,
            )
        except asyncpg.UniqueViolationError:
            existing = await conn.fetchrow(
                "SELECT id::text AS id, status::text AS status, "
                "       (normalized_hash = $1) AS by_hash "
                "FROM ingest_events "
                "WHERE source = 'meeting_recap' "
                "  AND (normalized_hash = $1 "
                "       OR (source_external_id IS NOT NULL "
                "           AND source_external_id = $2)) "
                "LIMIT 1",
                hash_hex,
                body.source_external_id,
            )
            if not existing:
                raise
            return {
                "event_id": existing["id"],
                "status": existing["status"],
                "deduped": True,
                "dedup_key": "content_hash" if existing["by_hash"] else "source_external_id",
            }

    log.info(
        "ingest_meeting_recap_received",
        extra={
            "event_id": row["id"],
            "actor_type": actor_type,
            "principal_id": principal.id,
            "content_bytes": len(canonical),
            "request_id": request_id,
        },
    )

    return {
        "event_id": row["id"],
        "status": row["status"],
        "deduped": False,
    }


# ---------------------------------------------------------------------------
# Slack ingest (Chunk 5 step 1)
# ---------------------------------------------------------------------------

# Slack ts is "<seconds>.<microseconds>" — use as the per-message
# external id along with team + channel for full uniqueness across
# workspaces.
# Per Codex chunk-5-close: each Slack id type has a stable prefix.
# Field-specific patterns reject mistaken cross-field forwards from
# n8n (a channel id in the user_id field, etc.).
_SLACK_TEAM_ID_PATTERN = r"^T[A-Z0-9]{2,30}$"
_SLACK_CHANNEL_ID_PATTERN = r"^[CGD][A-Z0-9]{2,30}$"
_SLACK_USER_ID_PATTERN = r"^[UW][A-Z0-9]{2,30}$"
_SLACK_ENTERPRISE_ID_PATTERN = r"^E[A-Z0-9]{2,30}$"
_SLACK_TS_PATTERN = r"^\d{8,12}\.\d{6}$"


class SlackIngest(BaseModel):
    model_config = {"extra": "forbid"}

    team_id: str = Field(..., min_length=3, max_length=32, pattern=_SLACK_TEAM_ID_PATTERN,
                          description="Slack workspace id (T-prefixed).")
    channel_id: str = Field(..., min_length=3, max_length=32, pattern=_SLACK_CHANNEL_ID_PATTERN,
                              description="Slack channel id (C public, G private, D DM).")
    ts: str = Field(..., min_length=8, max_length=32, pattern=_SLACK_TS_PATTERN,
                     description="Slack message timestamp (idempotency key).")
    text: str = Field(..., min_length=1, max_length=50_000,
                       description="Raw message text. UTF-8. Slack caps at 40k.")
    user_id: str | None = Field(default=None, max_length=32, pattern=_SLACK_USER_ID_PATTERN,
                                  description="Slack user id of the poster (U or W prefix).")
    thread_ts: str | None = Field(default=None, max_length=32, pattern=_SLACK_TS_PATTERN,
                                    description="Parent ts if this is a thread reply.")
    channel_name: str | None = Field(default=None, max_length=128)
    user_name: str | None = Field(default=None, max_length=128)
    team_domain: str | None = Field(default=None, max_length=128,
                                       description="Workspace subdomain, e.g. 'kyleconway'.")
    workspace_name: str | None = Field(default=None, max_length=128,
                                          description="Display name of the workspace.")
    enterprise_id: str | None = Field(default=None, max_length=32, pattern=_SLACK_ENTERPRISE_ID_PATTERN,
                                         description="Slack Enterprise Grid id, when applicable.")
    extra: dict[str, Any] = Field(default_factory=dict,
                                    description="Free-form context from n8n forward.")


@router.post("/slack")
async def ingest_slack_message(
    body: SlackIngest,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Service-only Slack message ingest.

    Called by n8n after Slack signing verification + URL handshake +
    retry de-dupe at the edge. The OpsMemory API trusts the n8n
    forward; we still require a service principal carrying
    `ingest:write` so a leaked admin session can't post here.

    Idempotency:
      - source_external_id = '{team_id}:{channel_id}:{ts}' is unique
        per source via the partial UNIQUE index from migration 0003.
        This is the canonical Slack idempotency key; Slack retries the
        webhook with the same ts on 5xx.
      - Slack does NOT participate in normalized_hash uniqueness — short
        repeated messages ("ok", "+1", "lgtm") across channels are
        legitimately distinct. The (source, normalized_hash) UNIQUE
        from 0005 was reverted in 0006 in favor of a meeting_recap-only
        partial UNIQUE.

    Pipeline behavior in this commit:
      - The event lands as 'received'. The reconciliation worker
        currently skips non-'meeting_recap' events (pipeline.py:127).
        A follow-up commit generalizes the prompt + step routing to
        actually run extraction on Slack messages.
    """
    # Service-only — admin user shouldn't post here directly. Per
    # Codex chunk-3-close rec: machine endpoints require service
    # principal + scope (admin bypass would be a defense-in-depth
    # hole if an admin browser session ever made it here).
    if principal.principal_type != "service":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="slack ingest requires a service principal",
        )
    require_scope(principal, SCOPE_INGEST_WRITE)

    canonical = canonicalize(body.text)
    if len(canonical) < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="text empty after canonicalization",
        )
    hash_hex = content_hash(canonical)

    source_external_id = f"{body.team_id}:{body.channel_id}:{body.ts}"
    metadata = {
        "team_id": body.team_id,
        "channel_id": body.channel_id,
        "ts": body.ts,
        "thread_ts": body.thread_ts,
        "user_id": body.user_id,
        "channel_name": body.channel_name,
        "user_name": body.user_name,
        "team_domain": body.team_domain,
        "workspace_name": body.workspace_name,
        "enterprise_id": body.enterprise_id,
        "extra": body.extra,
    }

    request_id = getattr(request.state, "request_id", None)

    pool = request.app.state.db
    async with pool.acquire() as conn:
        # Slack idempotency is by source_external_id ONLY (the
        # canonical message ts). NO content-hash dedup — short
        # repeated messages across channels are legitimately distinct
        # events, and 0006 dropped the source-scoped hash UNIQUE for
        # exactly this reason.
        existing = await conn.fetchrow(
            "SELECT id::text AS id, status::text AS status FROM ingest_events "
            "WHERE source = 'slack_message' AND source_external_id = $1",
            source_external_id,
        )
        if existing:
            return {
                "event_id": existing["id"],
                "status": existing["status"],
                "deduped": True,
                "dedup_key": "source_external_id",
            }

        try:
            row = await conn.fetchrow(
                """
                INSERT INTO ingest_events
                  (source, source_external_id, raw_content, normalized_hash,
                   source_metadata, status,
                   actor_type, actor_user_id, actor_service_account_id,
                   request_id)
                VALUES
                  ('slack_message', $1, $2, $3, $4::jsonb, 'received',
                   'service', NULL, $5::uuid, $6)
                RETURNING id::text AS id, status::text AS status
                """,
                source_external_id,
                canonical,
                hash_hex,
                metadata,
                principal.id,
                request_id,
            )
        except asyncpg.UniqueViolationError:
            # Only source_external_id is UNIQUE for Slack now.
            existing = await conn.fetchrow(
                "SELECT id::text AS id, status::text AS status FROM ingest_events "
                "WHERE source = 'slack_message' AND source_external_id = $1",
                source_external_id,
            )
            if not existing:
                raise
            return {
                "event_id": existing["id"],
                "status": existing["status"],
                "deduped": True,
                "dedup_key": "source_external_id",
            }

    log.info(
        "ingest_slack_message_received",
        extra={
            "event_id": row["id"],
            "team_id": body.team_id,
            "channel_id": body.channel_id,
            "ts": body.ts,
            "service_id": principal.id,
            "content_bytes": len(canonical),
            "request_id": request_id,
        },
    )

    return {
        "event_id": row["id"],
        "status": row["status"],
        "deduped": False,
        "source": "slack_message",
        "source_external_id": source_external_id,
    }
