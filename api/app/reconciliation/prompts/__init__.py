"""Prompt templates for the reconciliation pipeline.

Stored as Markdown so version control diffs are readable and the prompt
itself can be reviewed without code-formatter noise.

Version suffix in the filename (`.v1.md`, `.v2.md`, ...) is the canonical
prompt version recorded in llm_calls.prompt_template. Bump on any change
that meaningfully alters extraction/choice behavior — even small wording
tweaks. Old prompt versions stay in the repo so historical llm_calls
rows can be matched back to the exact text they ran against.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> tuple[str, str, str]:
    """Return (template_id, body, sha256_hex) for a prompt file.

    `name` includes the version suffix, e.g. 'meeting_recap_extract.v1'.
    Raises FileNotFoundError if the prompt isn't in the repo.
    """
    path = PROMPT_DIR / f"{name}.md"
    body = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return name, body, digest
