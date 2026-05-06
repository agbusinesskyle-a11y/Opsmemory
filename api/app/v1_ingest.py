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
  - normalized_hash is unique. Re-posting identical content returns the
    existing event_id with deduped=true and no new row.
  - source_external_id (when supplied) is unique per source. Posting the
    same Slack message twice returns the existing event.
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
        # First check: dedup by content hash. If we've seen this exact
        # canonical content before, return the existing row.
        existing = await conn.fetchrow(
            "SELECT id::text AS id, status::text AS status FROM ingest_events "
            "WHERE normalized_hash = $1",
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
                __import__("json").dumps(body.source_metadata),
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
                "WHERE normalized_hash = $1 "
                "   OR (source = 'meeting_recap' AND source_external_id IS NOT NULL "
                "       AND source_external_id = $2) "
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
