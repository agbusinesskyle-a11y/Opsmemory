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
from pydantic import BaseModel, Field, model_validator

from .auth import Principal, require_principal
from .authz import SCOPE_INGEST_WRITE, require_scope

# xlsx_decode imports openpyxl + defusedxml. Lazy-import inside the
# endpoint so the API still boots when those deps aren't installed
# (e.g. a partial deploy where requirements.txt didn't update before
# the API restarted).

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

    NOTE: This collapses internal whitespace (e.g. "a    b" -> "a b"),
    which is correct for free-form text (meeting recaps, Slack messages)
    but WRONG for CSV / structured data where cell contents may carry
    meaningful whitespace runs. Use canonicalize_file_drop() for those.
    """
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse trailing whitespace per line (don't change line counts)
    text = "\n".join(_WS_RUN.sub(" ", line.rstrip()) for line in text.split("\n"))
    return text.strip("\n")


def canonicalize_file_drop(content: str) -> str:
    """Canonicalizer for file_drop content. Codex chunk-9-step1 close-fix:
    canonicalize() collapses horizontal whitespace, which would mutate
    CSV cells like ``"a,b   c,d"`` -> ``"a,b c,d"`` BEFORE the parser
    sees them. Preserve internal whitespace; normalize only line endings
    + strip outer blank lines so the SHA-256 is deterministic.
    """
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip("\n")


def content_hash(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_drive_modified_time(raw: str) -> str:
    """Parse Drive's RFC3339 modifiedTime to a canonical UTC ISO string.

    Codex chunk-9-step1 close-fix: modified_time is part of the
    source_external_id ('drive:{file_id}:{modified_time}'). Accepting
    arbitrary text would let n8n misconfiguration persist bad
    idempotency keys forever. Require tz-aware ISO; canonicalize to
    UTC so '2026-05-06T08:00:00-07:00' and '2026-05-06T15:00:00Z'
    don't produce two different keys for the same instant.

    Raises HTTPException(400, ...) on failure.
    """
    from datetime import datetime as _dt
    s = (raw or "").strip()
    if not s:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "modified_time_required",
                    "field": "modified_time"},
        )
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.fromisoformat(s)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "modified_time_invalid",
                    "field": "modified_time", "got": raw},
        )
    if dt.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "modified_time_naive",
                    "field": "modified_time",
                    "detail": "include a timezone offset"},
        )
    # Canonicalize to UTC and strip microseconds so the external id
    # is deterministic regardless of how Drive serializes precision.
    return dt.astimezone(__import__("datetime").timezone.utc).replace(microsecond=0).isoformat()


def _looks_like_zip_or_xlsx(content: str) -> bool:
    """Cheap binary-bytes-as-text sniff. XLSX is a ZIP container
    starting with 'PK\\x03\\x04'. n8n is supposed to convert XLSX to
    CSV before posting; if raw bytes leak through, we'd canonicalize
    garbage and queue an unparseable event. 415 instead.
    """
    if not content:
        return False
    head = content[:8]
    return head.startswith("PK\x03\x04") or head.startswith("PK\x05\x06") or head.startswith("PK\x07\x08")


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
        # Hard channel gate: refuse messages from channels that aren't
        # in slack_channel_mappings with status='active'. Per Codex
        # Phase-C-plan review: AMBIGUOUS review_items are fine for
        # unresolved owners, NOT for unknown channels. n8n already
        # filters via its own allowlist as a first-line check; this is
        # the authoritative server-side gate so a misconfigured n8n
        # forward (or future direct service-account caller) can't
        # bypass it. Distinguish 'not_mapped' from 'paused' so the
        # operator can fix either case without a stack trace.
        mapping = await conn.fetchrow(
            "SELECT status::text AS status, business_id::text AS business_id "
            "FROM slack_channel_mappings "
            "WHERE team_id = $1 AND channel_id = $2",
            body.team_id, body.channel_id,
        )
        if not mapping:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "channel_not_mapped",
                        "team_id": body.team_id,
                        "channel_id": body.channel_id,
                        "detail": "no slack_channel_mappings row; add one "
                                  "with status='active' to enable ingest"},
            )
        if mapping["status"] != "active":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "channel_paused",
                        "team_id": body.team_id,
                        "channel_id": body.channel_id,
                        "status": mapping["status"],
                        "detail": "slack_channel_mappings.status is "
                                  "not 'active'; flip to 'active' to "
                                  "resume or remove the n8n forward"},
            )

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


# ---------------------------------------------------------------------------
# File-drop ingest (Chunk 9 step 1)
# ---------------------------------------------------------------------------
#
# n8n watches a Drive folder, downloads the file, and POSTs here with
# the file's text representation + Drive metadata. OpsMemory canonicalizes
# the text into raw_content (storing the byte SHA-256 + Drive id +
# modified_time + filename + mime + folder ids in source_metadata) and
# queues an ingest_events row.
#
# Per Codex chunk-8-close STEP 9 PLAN, this commit ships the endpoint
# only — the parser + extract prompt + source registry entry land in
# step 2. Until then, file_drop events sit at status='received' and the
# worker ignores them (file_drop is not in reconciliation.SOURCES, so
# the claim filter excludes them).
#
# Idempotency: source_external_id = 'drive:{file_id}:{modified_time}'
# matches Codex's recommendation. The (source, source_external_id)
# partial UNIQUE from migration 0003 dedupes re-uploads of the same
# Drive file at the same modified_time. Different modified_time values
# legitimately represent edited files — distinct events.
#
# Codex specifically flagged that content-hash uniqueness for file_drop
# is wrong (legitimate duplicate spreadsheets across folders), so we
# don't add a hash UNIQUE — only the partial meeting_recap-only one
# from migration 0006 stands.

# Drive file id: 30-50 chars typical, alphanumeric + dash/underscore.
_DRIVE_FILE_ID_PATTERN = r"^[A-Za-z0-9_-]{10,128}$"


class FileDropIngest(BaseModel):
    model_config = {"extra": "forbid"}

    file_id: str = Field(..., min_length=10, max_length=128,
                          pattern=_DRIVE_FILE_ID_PATTERN,
                          description="Google Drive file id.")
    modified_time: str = Field(..., min_length=1, max_length=64,
                                description="ISO-8601 timestamp of the Drive file's last modification.")
    mime_type: str = Field(..., min_length=1, max_length=128,
                            description="MIME type as reported by Drive (e.g. 'text/csv', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet').")
    filename: str = Field(..., min_length=1, max_length=512)
    file_content: str | None = Field(
        default=None, min_length=1, max_length=200_000,
        description="Text representation of the file (CSV / plain text). "
                    "Mutually exclusive with xlsx_base64.")
    xlsx_base64: str | None = Field(
        default=None, min_length=1, max_length=8_000_000,
        description="Base64-encoded XLSX bytes (Chunk 9 step 3). "
                    "OpsMemory decodes server-side via openpyxl, "
                    "selects a sheet ('Tasks' if present, else the "
                    "first visible sheet with a recognizable task "
                    "header), converts to CSV, then runs the existing "
                    "CSV parser path. Mutually exclusive with file_content. "
                    "Pydantic cap is 8MB base64 (~6MB raw); the "
                    "authoritative size cap is 5 MiB raw bytes inside "
                    "xlsx_decode.decode_xlsx_to_csv (xlsx_too_large 422 "
                    "if exceeded).")
    business_slug: str = Field(..., min_length=1, max_length=64,
                                 description="Business this file is associated with. n8n "
                                             "maps Drive folder -> business slug; "
                                             "OpsMemory does not infer business from "
                                             "folder paths to keep the mapping auditable.")
    folder_ids: list[str] = Field(default_factory=list, max_length=16,
                                    description="Drive folder ids in the file's path. "
                                                "Stored for audit; not used for routing.")
    web_link: str | None = Field(default=None, max_length=1024,
                                   description="Drive 'Open' URL (https://drive.google.com/...).")
    drive_owner_email: str | None = Field(default=None, max_length=256,
                                            description="Drive file owner email, for audit.")
    extra: dict[str, Any] = Field(default_factory=dict,
                                    description="Free-form context from n8n forward.")

    @model_validator(mode="after")
    def _exactly_one_body(self):
        # Codex chunk-9-step2 STEP 3 PLAN: require exactly one of
        # file_content / xlsx_base64. n8n picks based on Drive's MIME.
        has_text = self.file_content is not None
        has_xlsx = self.xlsx_base64 is not None
        if has_text and has_xlsx:
            raise ValueError(
                "exactly one of file_content / xlsx_base64 may be set"
            )
        if not has_text and not has_xlsx:
            raise ValueError(
                "one of file_content / xlsx_base64 must be set"
            )
        return self


@router.post("/file_drop")
async def ingest_file_drop(
    body: FileDropIngest,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """Service-only Drive file-drop ingest.

    Called by n8n after a Drive folder change event. n8n is responsible
    for verifying Drive's webhook + downloading the file + converting
    binary formats (XLSX) to text (CSV). OpsMemory trusts the n8n
    forward; service-key auth + ingest:write scope authenticates the
    bridge.

    Idempotency:
      - source_external_id = 'drive:{file_id}:{modified_time}'.
        Re-posting the same (file_id, modified_time) tuple returns the
        prior event_id with deduped=true. Edits to the same file (new
        modified_time) produce distinct events — that's intentional;
        the operator may want each version reviewed.
      - File content SHA-256 is stored in source_metadata.byte_sha256
        for forensic comparison but does NOT participate in any UNIQUE.

    Pipeline behavior in this commit:
      - The event lands at status='received'. The worker ignores
        file_drop because it isn't in reconciliation.sources.SOURCES.
        Step 2 adds the parser + source registry entry; until then,
        events accumulate as audit/provenance only.
    """
    if principal.principal_type != "service":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="file_drop ingest requires a service principal",
        )
    require_scope(principal, SCOPE_INGEST_WRITE)

    # Codex chunk-9-step1 close-fix: parse + canonicalize modified_time
    # so source_external_id is deterministic across timezones / Drive
    # serialization variants.
    canonical_modified_time = _parse_drive_modified_time(body.modified_time)

    # Server-side XLSX path (Chunk 9 step 3). When the body carries
    # xlsx_base64, decode -> pick sheet -> CSV. Reuses the existing
    # CSV parser path downstream. Decode failures map to a meaningful
    # 4xx with a code field instead of a 500.
    xlsx_decode_metadata: dict[str, Any] | None = None
    if body.xlsx_base64 is not None:
        try:
            from .xlsx_decode import XlsxDecodeError, decode_xlsx_to_csv
        except ImportError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "xlsx_decode_unavailable",
                        "detail": "openpyxl/defusedxml not installed on this "
                                  "API instance; reinstall requirements.txt "
                                  "and restart"},
            )
        try:
            csv_text, xlsx_decode_metadata = decode_xlsx_to_csv(body.xlsx_base64)
        except XlsxDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": exc.code, "detail": exc.detail},
            )
        canonical = canonicalize_file_drop(csv_text)
    else:
        # Codex chunk-9-step1 close-fix: reject bytes that look like a
        # ZIP / XLSX container BEFORE canonicalization. With xlsx_base64
        # now the proper channel for binary, file_content is required
        # to be already-text.
        if _looks_like_zip_or_xlsx(body.file_content or ""):
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail={"code": "binary_content_unsupported",
                        "detail": "file_content looks like a ZIP/XLSX binary; "
                                  "post the bytes via xlsx_base64 instead"},
            )
        # Codex chunk-9-step1 close-fix: file_drop content gets a
        # CSV-safe normalizer (line-ending only) instead of canonicalize()
        # which would collapse meaningful whitespace inside CSV cells.
        canonical = canonicalize_file_drop(body.file_content or "")

    if len(canonical) < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="file content empty after canonicalization",
        )
    hash_hex = content_hash(canonical)

    source_external_id = f"drive:{body.file_id}:{canonical_modified_time}"

    pool = request.app.state.db
    request_id = getattr(request.state, "request_id", None)

    async with pool.acquire() as conn:
        # Codex chunk-9-step1 close-fix: dedupe pre-check FIRST so a
        # retry of an already-ingested file still dedupes after the
        # business is later soft-deleted. Business validation only
        # matters for NEW events.
        existing = await conn.fetchrow(
            "SELECT id::text AS id, status::text AS status FROM ingest_events "
            "WHERE source = 'file_drop' AND source_external_id = $1",
            source_external_id,
        )
        if existing:
            return {
                "event_id": existing["id"],
                "status": existing["status"],
                "deduped": True,
                "dedup_key": "source_external_id",
            }

        # Validate business_slug exists and is active. n8n's
        # folder->business mapping config could go stale; surfacing
        # this as 422 tells the operator to fix the config rather
        # than silently creating orphan ingest events.
        # Codex chunk-9-step1 close-fix: distinguish "missing" from
        # "soft-deleted" via separate error codes; the schema uses
        # deletion_state, not a 'status' column on businesses.
        biz = await conn.fetchrow(
            "SELECT id::text AS id, slug::text AS slug, "
            "       deletion_state::text AS deletion_state "
            "FROM businesses "
            "WHERE slug::text = $1",
            body.business_slug,
        )
        if not biz:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "business_not_found",
                        "business_slug": body.business_slug,
                        "detail": "n8n folder->business mapping is stale"},
            )
        if biz["deletion_state"] != "active":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "business_inactive",
                        "business_slug": body.business_slug,
                        "deletion_state": biz["deletion_state"],
                        "detail": "business has been soft-deleted; "
                                  "update the n8n folder mapping"},
            )

        metadata = {
            "drive_file_id": body.file_id,
            "modified_time": canonical_modified_time,
            "modified_time_raw": body.modified_time,
            "mime_type": body.mime_type,
            "filename": body.filename,
            "folder_ids": body.folder_ids,
            "web_link": body.web_link,
            "drive_owner_email": body.drive_owner_email,
            "byte_sha256": hash_hex,
            "business_slug": biz["slug"],
            "business_id": biz["id"],
            "extra": body.extra,
        }
        if xlsx_decode_metadata is not None:
            # Chunk 9 step 3: surface XLSX provenance so the audit pane
            # can show "from sheet 'Tasks' of permits.xlsx".
            metadata["xlsx_decode"] = xlsx_decode_metadata

        try:
            row = await conn.fetchrow(
                """
                INSERT INTO ingest_events
                  (source, source_external_id, raw_content, normalized_hash,
                   source_metadata, status,
                   actor_type, actor_user_id, actor_service_account_id,
                   request_id)
                VALUES
                  ('file_drop', $1, $2, $3, $4::jsonb, 'received',
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
            # Race with a concurrent post of the same (file_id, modified_time).
            existing = await conn.fetchrow(
                "SELECT id::text AS id, status::text AS status FROM ingest_events "
                "WHERE source = 'file_drop' AND source_external_id = $1",
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
        "ingest_file_drop_received",
        extra={
            "event_id": row["id"],
            "drive_file_id": body.file_id,
            "modified_time": body.modified_time,
            "filename": body.filename,
            "mime_type": body.mime_type,
            "business_slug": biz["slug"],
            "service_id": principal.id,
            "content_bytes": len(canonical),
            "request_id": request_id,
        },
    )

    return {
        "event_id": row["id"],
        "status": row["status"],
        "deduped": False,
        "source": "file_drop",
        "source_external_id": source_external_id,
    }
