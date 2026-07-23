"""M11 — the structured-logging spine (PORT of the reference `ops/observability.py`).

One JSON record per line at every boundary; configured ONCE at server start onto the `"hive"`
root logger. The in-memory metrics registry is DROPPED for v-min (Diagnostics →
health only). Secrets are NEVER logged — callers pass field names/counts/paths, never values.
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

_DEFAULT_LEVEL = logging.INFO
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 5
_ROOT = "hive"


class JSONFormatter(logging.Formatter):
    """Structured JSON output, one record per line. Standard fields ts/level/logger/message
    plus any JSON-serializable record extras (e.g. trace_id); non-serializable extras are
    repr'd, never dropped."""

    BASE_FIELDS = {
        "name",
        "levelname",
        "msg",
        "args",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "pathname",
        "filename",
        "module",
        "levelno",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if k not in self.BASE_FIELDS and not k.startswith("_"):
                try:
                    json.dumps(v)
                    payload[k] = v
                except TypeError:
                    payload[k] = repr(v)
        return json.dumps(payload, default=str)


def configure_json_logging(
    level: int = _DEFAULT_LEVEL,
    file: Optional[str] = None,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backup_count: int = _DEFAULT_BACKUP_COUNT,
    stream: bool = True,
) -> None:
    """Wire a JSON handler to the `"hive"` root logger. Idempotent: removes any existing hive
    handlers before adding (so repeated calls do not pile handlers up). `stream=True` also
    emits to stderr (so an MCP subprocess's stderr surfaces in the parent agent); `file`
    adds a rotating file handler. // O(1)."""
    import os

    root = logging.getLogger(_ROOT)
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = JSONFormatter()
    if stream:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    if file:
        os.makedirs(os.path.dirname(os.path.abspath(file)) or ".", exist_ok=True)
        rfh = RotatingFileHandler(
            filename=file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        rfh.setFormatter(fmt)
        root.addHandler(rfh)
    root.propagate = False
