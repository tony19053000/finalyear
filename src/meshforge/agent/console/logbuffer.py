"""The agent's own recent log, kept in memory for the console's Logs page.

:class:`LogBuffer` is a stdlib :class:`logging.Handler` added to the root logger
next to the stderr handler of :func:`meshforge.common.logging.configure_logging`.
It renders each record with a **JSON** :class:`structlog.stdlib.ProcessorFormatter`
built from the same processors, so every line has already been through the
redaction processor (sensitive keys, URL credentials, bearer tokens, ``key=value``
secrets) - the console never sees what the journal would not. It keeps the last
``capacity`` lines (a :class:`collections.deque`; ``emit`` runs under the
handler's lock, so threads may log too).

Only the agent's own process log is here; job output (stdout/stderr of a job)
goes to the controller, never to this buffer.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, Final

import structlog
from structlog.tracebacks import ExceptionDictTransformer

from meshforge.common.logging import redact_processor

__all__ = ["LogBuffer", "LogLine", "install_log_buffer"]

_MAX_LINE_CHARS: Final = 8192
_KNOWN: Final = frozenset(
    {"timestamp", "level", "event", "logger", "component", "color_message"}
)
"""Shown as columns - or, ``color_message`` (uvicorn), dropped: ANSI codes."""


@dataclass(frozen=True, slots=True)
class LogLine:
    seq: int
    timestamp: str
    level: str
    event: str
    logger: str
    fields: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "level": self.level,
            "event": self.event,
            "logger": self.logger,
            "fields": self.fields,
        }


def _formatter() -> logging.Formatter:
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        redact_processor,
    ]
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.ExceptionRenderer(
                ExceptionDictTransformer(show_locals=False, use_rich=False)
            ),
            redact_processor,
            structlog.processors.JSONRenderer(default=str),
        ],
    )


class LogBuffer(logging.Handler):
    """See module docstring."""

    def __init__(self, capacity: int, *, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.setFormatter(_formatter())
        self._lines: deque[LogLine] = deque(maxlen=capacity)
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
            data = json.loads(text)
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)
            return
        if not isinstance(data, dict):
            return
        self._seq += 1
        fields = {k: v for k, v in data.items() if k not in _KNOWN}
        encoded = json.dumps(fields, default=str)
        if len(encoded) > _MAX_LINE_CHARS:
            fields = {"truncated": f"{len(encoded)} characters of fields"}
        self._lines.append(
            LogLine(
                seq=self._seq,
                timestamp=str(data.get("timestamp", "")),
                level=str(data.get("level", record.levelname.lower())),
                event=str(data.get("event", ""))[:_MAX_LINE_CHARS],
                logger=str(data.get("logger", record.name)),
                fields=fields,
            )
        )

    def tail(self, limit: int, *, after: int = 0) -> list[LogLine]:
        """The newest ``limit`` lines with ``seq > after``, oldest first."""
        with self.lock if self.lock is not None else contextlib.nullcontext():
            lines = [line for line in self._lines if line.seq > after]
        return lines[-limit:] if limit > 0 else []


def install_log_buffer(capacity: int) -> LogBuffer:
    """Add a :class:`LogBuffer` to the root logger (replacing an earlier one)."""
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, LogBuffer):
            root.removeHandler(existing)
    buffer = LogBuffer(capacity)
    root.addHandler(buffer)
    return buffer
