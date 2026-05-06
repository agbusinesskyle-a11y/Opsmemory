"""OpsMemory reconciliation pipeline.

Codex-designed 7-step deterministic flow with LLM at extract + choose only.
Chunk 3 implements steps 1-6 (extract -> normalize -> retrieve -> choose
-> validate -> queue review). Step 7 (apply with conflict re-check) lives
in Chunk 4 alongside the review approval UI.

Module layout:
    extract.py     LLM call: raw text -> candidate task-shaped facts
    normalize.py   Deterministic: alias resolution, business inference,
                   date parsing, dedup keys
    retrieve.py    Deterministic: candidate task lookup by business +
                   owner + time + lexical match
    choose.py      LLM call: given candidate + retrieved tasks, pick
                   CREATE / UPDATE / COMPLETE / IGNORE / AMBIGUOUS
    validate.py    Deterministic: schema enforcement + authz checks
    pipeline.py    Orchestrator. process_event() runs the six steps
                   for one ingest_event.
    llm_client.py  OpenAI-compatible client (talks to litellm proxy).
                   Logs every call to llm_calls.
    prompts/       Markdown templates with explicit version suffixes.
                   meeting_recap_extract.v1.md
                   meeting_recap_choose.v1.md

Each step is independently testable. LLM calls are mockable via the
INGEST_LLM_EXTRACT_MODELS=mock env override (deterministic stub).
"""
