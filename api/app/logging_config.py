"""Structured JSON logging for OpsMemory.

Replaces the format-string-based JSON logger that breaks on quotes/newlines
in messages. All fields are properly JSON-escaped via ``json.dumps``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Emits one JSON object per log record.

    Fields: ts (ISO-8601 UTC), level, logger, msg, plus any extras the
    caller attached via ``logger.info(..., extra={...})``.
    Sensitive headers are never logged because we never log raw headers
    in our middleware — only request_id and path.
    """

    RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in self.RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = repr(value)
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """Install JsonFormatter on root + uvicorn loggers, idempotent."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    # Remove pre-existing handlers (basicConfig may have installed one).
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(level)

    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        ulog = logging.getLogger(logger_name)
        for h in list(ulog.handlers):
            ulog.removeHandler(h)
        ulog.addHandler(handler)
        ulog.setLevel(level)
        ulog.propagate = False
