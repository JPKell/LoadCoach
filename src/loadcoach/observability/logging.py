"""loadcoach.observability.logging — structured logging, per Observability Standards.

Two formatters (text for a TTY, JSON Lines otherwise), one logger per module, and a
``contextvars``-based correlation context so every log record produced while handling a request
carries that request's ``request_id`` without threading it through every function signature. A
redaction filter removes anything shaped like a secret before it reaches either formatter.

Not in the Phase 1 file list verbatim, but required by it: the Work item names "logging" alongside
settings, request IDs and health as first-phase requirements, mirroring FreeWeight's own
``observability.logging`` module at the same point in its own sequence.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final

from baseaicore.timeutil import to_rfc3339, utc_now

from loadcoach.__about__ import __version__

__all__ = ["bind_context", "configure_logging", "current_context"]

_REDACT_PATTERN: Final = re.compile(r"(?i)(token|key|secret|password|authorization|cookie)")
_REDACTED_VALUE: Final = "********"

_context_var: contextvars.ContextVar[Mapping[str, Any] | None] = contextvars.ContextVar(
    "loadcoach_log_context", default=None
)

_STANDARD_RECORD_ATTRS: Final = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime"}


@contextmanager
def bind_context(**fields: Any) -> Iterator[None]:
    """Add correlation fields to every log record produced within this block.

    Nested calls compose: an inner ``bind_context`` sees and extends the outer one's fields, and
    restores exactly the outer state on exit.
    """
    current = dict(_context_var.get() or {})
    current.update(fields)
    token = _context_var.set(current)
    try:
        yield
    finally:
        _context_var.reset(token)


def current_context() -> dict[str, Any]:
    """Return a copy of the correlation fields bound in the current context."""
    return dict(_context_var.get() or {})


def _redact(value: Any) -> Any:
    """Recursively replace any value whose key looks secret-shaped with a fixed placeholder."""
    if isinstance(value, dict):
        return {
            key: (_REDACTED_VALUE if _REDACT_PATTERN.search(str(key)) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class _JsonFormatter(logging.Formatter):
    """Renders each record as a single JSON line, redacted."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as a single JSON line, redacted."""
        payload: dict[str, Any] = {
            "timestamp": to_rfc3339(utc_now()),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "loadcoach_version": __version__,
            **current_context(),
            **{
                key: value
                for key, value in record.__dict__.items()
                if key not in _STANDARD_RECORD_ATTRS
            },
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(_redact(payload), default=str)


class _TextFormatter(logging.Formatter):
    """Renders each record as an aligned, human-readable line, redacted."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as an aligned, human-readable line, redacted."""
        base = f"{record.levelname:<8} {record.name}: {record.getMessage()}"
        fields = {
            **current_context(),
            **{
                key: value
                for key, value in record.__dict__.items()
                if key not in _STANDARD_RECORD_ATTRS
            },
        }
        redacted = _redact(fields)
        if redacted:
            base += " " + " ".join(f"{key}={value}" for key, value in redacted.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(*, level: str = "INFO", log_format: str = "auto") -> None:
    """Configure the root logger with the standard formatter and level.

    Args:
        level: A standard logging level name (``"DEBUG"``, ``"INFO"``, ...).
        log_format: ``"text"``, ``"json"``, or ``"auto"`` (text on a TTY, JSON Lines otherwise).
    """
    resolved_format = log_format
    if resolved_format == "auto":
        resolved_format = "text" if sys.stderr.isatty() else "json"

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_TextFormatter() if resolved_format == "text" else _JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # SQLAlchemy's own INFO-level engine logging duplicates what DEBUG-level `weightsdb.*`
    # logging already covers under our own formatter; left at WARNING unless a caller sets a more
    # verbose level via LOADCOACH_LOG_LEVEL for a specific investigation.
    logging.getLogger("sqlalchemy.engine").setLevel(
        os.environ.get("LOADCOACH_SQLALCHEMY_LOG_LEVEL", "WARNING")
    )
