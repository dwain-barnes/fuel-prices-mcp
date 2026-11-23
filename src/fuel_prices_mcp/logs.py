"""Logging to stderr only (stdout is reserved for the stdio transport's JSON-RPC stream).

JSON lines by default: one object per record, so log injection via CR/LF in
user-supplied values is impossible and audit events are machine-parseable.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

AUDIT_LOGGER = "fuel_prices_mcp.audit"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if isinstance(event, dict):
            payload.update(event)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        event = getattr(record, "event", None)
        if isinstance(event, dict):
            text += " " + json.dumps(event, ensure_ascii=False, default=str)
        return text.replace("\r", "\\r").replace("\n", "\\n") if not record.exc_info else text


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    # The SDK and HTTP client log request-level detail (including upstream URLs) at INFO;
    # keep them quieter unless debugging.
    if level != "DEBUG":
        for name in ("mcp", "httpx", "httpcore", "httpx2", "httpcore2", "sse_starlette"):
            logging.getLogger(name).setLevel(logging.WARNING)
    # uvicorn installs its own handlers unless told otherwise; route through root.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


def audit(message: str, **fields: Any) -> None:
    logging.getLogger(AUDIT_LOGGER).info(message, extra={"event": fields})
