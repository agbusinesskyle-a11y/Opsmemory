"""Deterministic file_drop resolver (Chunk 9 step 2).

Forces every candidate's businesses to the operator-mapped value from
ingest source_metadata.business_slug. Per Codex chunk-9-step1 STEP 2
PLAN: "Do not let LLM infer business from folder names; add
deterministic Drive folder to business mapping or require n8n to send
a configured business slug." n8n already sends business_slug; this
resolver enforces it post-extract / post-parse.

Matches the slack_resolve.py pattern: pipeline.process_event calls
resolve_file_drop_context() per candidate after normalize() and before
retrieve().
"""

from __future__ import annotations

import logging

log = logging.getLogger("opsmemory.reconciliation.file_drop_resolve")


def resolve_file_drop_context(candidate: dict, *, source_metadata: dict | None) -> dict:
    """Mutate candidate.businesses to the file_drop ingest's mapped slug.

    The ingest endpoint validated business_slug exists + is active and
    stored both slug + uuid in source_metadata. The resolver overwrites
    any LLM-emitted businesses_hint (the prompt instructs the model to
    leave it [], but we don't trust the LLM to follow that).

    A non-blocking conflict marker is added when the LLM produced a
    business hint that differs from the ingest mapping — same shape
    as slack_resolve's business_resolution_conflict.
    """
    md = source_metadata or {}
    slug = md.get("business_slug")
    if not slug:
        # Should be unreachable: file_drop ingest always populates this.
        return candidate

    existing = candidate.get("businesses") or []
    if existing and slug not in existing:
        candidate["business_resolution_conflict"] = {
            "text_businesses": list(existing),
            "ingest_mapped": slug,
            "drive_file_id": md.get("drive_file_id"),
        }
        log.info("file_drop_business_conflict", extra={
            "drive_file_id": md.get("drive_file_id"),
            "candidate_text": list(existing),
            "ingest_mapped": slug,
        })

    candidate["businesses"] = [slug]
    candidate.setdefault("business_resolution_source", "file_drop_ingest_metadata")
    return candidate
