"""Deterministic CSV parser for source='file_drop' events (Chunk 9 step 2).

CSV files dropped into Drive are parsed without an LLM call: each row
becomes a candidate task. Header aliases let operators name their
columns naturally — `summary`, `task`, `title`, `description`, `due`,
`due_at`, `owner`, `assignee`, `category`, etc.

When the file isn't CSV-shaped (no recognizable header row), the
caller falls back to the LLM extract path with file_drop_extract.v1.

Per Codex chunk-9-step1 STEP 2 PLAN:
  - First pass uses hard-coded header aliases only. Operator-defined
    per-folder mappings need config storage/versioning and are
    deferred.
  - candidates[*].businesses_hint is always [] from this parser; the
    pipeline's deterministic resolver forces businesses from
    source_metadata.business_slug.
  - candidate_facts carries parser_kind='csv', row_number, filename,
    raw_due, raw_owner so the review UI can show provenance.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

log = logging.getLogger("opsmemory.reconciliation.file_drop_parser")


# Header aliases — case-insensitive substring match. First-match wins
# in the order each list appears below.
_SUMMARY_ALIASES = ("summary", "task", "title", "action", "todo", "to-do", "to do")
_DESCRIPTION_ALIASES = ("description", "details", "notes")
_DUE_ALIASES = ("due", "due_at", "due date", "due-date", "deadline", "by", "when")
_OWNER_ALIASES = ("owner", "assignee", "responsible", "assigned to", "assigned_to", "who")
_CATEGORY_ALIASES = ("category", "type", "kind", "area")
_PRIORITY_ALIASES = ("priority", "p", "rank", "importance")
_DEPENDENCY_ALIASES = ("dependency", "depends on", "blocker", "blocked by", "waiting on")


def _norm_header(h: str) -> str:
    return (h or "").strip().lower()


def _match_first(row_keys: list[str], aliases: tuple[str, ...]) -> str | None:
    """Return the original header key that matches one of the aliases,
    or None. Match is case-insensitive substring (e.g. 'Due Date'
    matches alias 'due').
    """
    for alias in aliases:
        a = alias.lower()
        for key in row_keys:
            if a in _norm_header(key):
                return key
    return None


def looks_like_csv(content: str) -> bool:
    """Heuristic: does this file have a CSV-shaped header row?

    Tested via csv.Sniffer (which infers delimiter + presence of
    header). Returns False on any sniff exception (binary/free-form/
    one-column files).
    """
    head = content[:4096]
    if not head.strip():
        return False
    if "\n" not in head and "\r" not in head:
        return False
    try:
        sniffer = csv.Sniffer()
        # has_header throws on degenerate input; treat as not-CSV.
        sniffer.sniff(head)
        if not sniffer.has_header(head):
            return False
    except csv.Error:
        return False
    # Require at least one comma in the first non-empty line — rules
    # out single-column markdown lists that csv.Sniffer occasionally
    # claims as CSV.
    first_line = head.splitlines()[0]
    return "," in first_line


def parse_csv_candidates(content: str, *, max_rows: int = 200) -> list[dict]:
    """Parse a CSV file body into a list of candidate dicts.

    Each candidate matches the LLM extract's output shape so the
    rest of the pipeline (normalize, retrieve, choose, validate)
    consumes them uniformly. parser_kind, row_number, raw fields
    are stamped into candidate_facts via extra keys.

    `max_rows` caps how many rows are emitted as candidates. Beyond
    that we stop reading; an XL spreadsheet can have thousands of
    rows but a single import shouldn't queue thousands of review
    items in one event.
    """
    candidates: list[dict] = []
    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        return candidates
    headers = list(reader.fieldnames)
    summary_key = _match_first(headers, _SUMMARY_ALIASES)
    if summary_key is None:
        # No recognizable summary column. Treat as not-parseable —
        # caller falls back to LLM extract.
        return []

    description_key = _match_first(headers, _DESCRIPTION_ALIASES)
    due_key = _match_first(headers, _DUE_ALIASES)
    owner_key = _match_first(headers, _OWNER_ALIASES)
    category_key = _match_first(headers, _CATEGORY_ALIASES)
    priority_key = _match_first(headers, _PRIORITY_ALIASES)
    dependency_key = _match_first(headers, _DEPENDENCY_ALIASES)

    for row_no, row in enumerate(reader, start=2):  # row 1 = header
        if row_no > max_rows + 1:
            log.info("file_drop_parser_truncated", extra={
                "max_rows": max_rows, "stopped_at": row_no,
            })
            break
        summary_raw = (row.get(summary_key) or "").strip()
        if not summary_raw:
            # Skip rows whose summary cell is blank.
            continue
        # Limit summary length to schema CHECK (4096).
        summary = summary_raw[:4096]

        candidate = {
            "summary": summary,
            "owner_hint": (row.get(owner_key) or "").strip() if owner_key else None,
            "businesses_hint": [],  # forced by file_drop_resolve from metadata
            "due_hint": (row.get(due_key) or "").strip() if due_key else None,
            "dependency_hint": (row.get(dependency_key) or "").strip() if dependency_key else None,
            "category_hint": (row.get(category_key) or "").strip() if category_key else None,
            # The prompt schema has source_quote; for CSV we surface
            # the row's data as a one-liner so the reviewer can see
            # the original cell values.
            "source_quote": _row_quote(row, headers),
            "source_timestamp": None,
            # Provenance tucked alongside the candidate. normalize.py
            # passes these through; review UI can render them.
            "parser_kind": "csv",
            "row_number": row_no,
            "raw_owner": (row.get(owner_key) or "") if owner_key else None,
            "raw_due": (row.get(due_key) or "") if due_key else None,
            "raw_priority": (row.get(priority_key) or "").strip() if priority_key else None,
        }
        # Empty-string -> None for hint-shaped fields so normalize's
        # heuristics (e.g. "today"/"tomorrow") don't trip on "".
        for k in ("owner_hint", "due_hint", "dependency_hint", "category_hint",
                   "raw_priority"):
            if candidate.get(k) == "":
                candidate[k] = None
        candidates.append(candidate)
    return candidates


def _row_quote(row: dict, headers: list[str]) -> str:
    """Produce an operator-readable source_quote from a CSV row."""
    parts = []
    for h in headers:
        v = row.get(h)
        if v is None or v == "":
            continue
        parts.append(f"{h}={v}")
    return " | ".join(parts)[:2048]
