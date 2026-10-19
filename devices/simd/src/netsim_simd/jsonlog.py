"""Structured JSON logging (one JSON object per line on stdout).

Every record carries ``ts``, ``level``, ``host``, ``logger``, ``event`` and ``msg``; state-change records add
fields such as ``service_id``, ``role``, ``old``, ``new`` and ``reason``::

    {"ts": "2026-10-19T10:00:00.123Z", "level": "info", "host": "spoke-1", "logger": "simd",
     "event": "oper-change", "msg": "SVC-0001:primary up -> down (remote-not-configured)",
     "service_id": "SVC-0001", "role": "primary", "old": "up", "new": "down", "reason": "remote-not-configured"}
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_HOST = "-"


def iso_ts(epoch: float) -> str:
    """``2026-10-19T10:00:00.123Z`` (UTC, milliseconds)."""
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": iso_ts(record.created),
            "level": record.levelname.lower(),
            "host": _HOST,
            "logger": record.name,
        }
        fields = getattr(record, "fields", None)
        payload["event"] = (fields or {}).get("event", "log")
        payload["msg"] = record.getMessage()
        if fields:
            payload.update({k: v for k, v in fields.items() if k != "event"})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(hostname: str, level: str = "INFO") -> None:
    """Route the root logger (and uvicorn's) to JSON lines on stdout."""
    global _HOST
    _HOST = hostname
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def log_event(logger: logging.Logger, event: str, msg: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Log a structured event: ``fields`` become top-level JSON members."""
    logger.log(level, msg, extra={"fields": {"event": event, **fields}})
