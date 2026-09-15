"""Structured logging (structlog) for every MeshForge process (ARCHITECTURE §25).

Usage::

    from meshforge.common.logging import configure_logging, get_logger, log_context

    configure_logging("INFO", fmt="json", component="agent")
    log = get_logger(__name__)
    with log_context(job_id=job_id, attempt_id=attempt_id):
        log.info("attempt.started", runtime="docker")

Design choices:

* **One pipeline for everything.** structlog renders through a stdlib
  ``logging.Handler`` (``structlog.stdlib.ProcessorFormatter``), so records from
  third-party libraries (grpc, uvicorn, sqlalchemy, …) come out in the same JSON
  shape, with the same timestamps, correlation fields and redaction.
* **Correlation fields** (``job_id``, ``attempt_id``, ``node_id``, ``session_id``;
  see :data:`CORRELATION_KEYS`) live in :mod:`contextvars`. Each asyncio task
  gets a copy of the context at creation, so bindings in one task never leak
  into another; bind them at the start of a task / request handler.
* **Redaction is a processor, not a convention.** Two layers run on every record:

  1. *Key-based*: fields whose key looks sensitive (password, token, secret, api
     key, authorization, cookie, credential, private key, dsn, database url,
     connection string, otp, jwt, pin, join/pairing code, session key, refresh
     token, …) and any ``SecretStr``/``SecretBytes`` value — at any nesting depth,
     including inside pydantic models, dataclasses and namedtuples — become
     ``"[REDACTED]"``. Key matching works on word parts (``auth_token`` and
     ``apiKey`` are redacted, ``num_tokens``, ``idempotency_key`` and
     ``pin_memory`` are not), trading a little recall for readable telemetry keys.
  2. *Value scrubbing* (:func:`scrub_text`) of every string — field values, the
     ``event`` message, exception messages and rendered tracebacks (JSON and
     console): URL credentials (``scheme://user:pass@`` → ``scheme://user:[REDACTED]@``),
     ``Bearer …``/``Basic …`` tokens anywhere in the text, and ``key=value`` /
     ``key: value`` pairs whose key is sensitive (``token=abc`` → ``token=[REDACTED]``).
     Linear-time; strings over ``MAX_SCRUB_CHARS`` (64 KiB) are cut and end in
     ``[TRUNCATED <n> bytes]``. Implementation and exact value rules:
     :mod:`meshforge.common.redaction` (names re-exported here).

  **Limitation:** free text has no keys, so layer 2 only catches the *known
  secret shapes* above. A bare secret interpolated into a message
  (``f"using {token}"``) cannot be recognised — pass secrets as fields (or better,
  not at all), never inside the message text.

  Tracebacks never include local variables (JSON and console). Nesting deeper
  than ``_MAX_DEPTH`` is replaced by ``"[TRUNCATED]"`` rather than emitted raw.
* Timestamps are ISO-8601 UTC. Output goes to stderr by default so CLI stdout
  stays clean for command output.
* ``fmt="console"`` gives coloured human output for local development.

Note: this module shadows the stdlib ``logging`` name only inside this package;
absolute imports (``import logging``) still get the stdlib module.
"""

from __future__ import annotations

import logging
import sys
import traceback
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from datetime import date, datetime
from enum import Enum
from pathlib import PurePath
from typing import Any, Final, Literal, TextIO

import structlog
from structlog.tracebacks import ExceptionDictTransformer
from structlog.typing import EventDict, Processor, WrappedLogger

from meshforge.common.errors import MeshForgeError
from meshforge.common.redaction import (
    MAX_SCRUB_CHARS,
    REDACTED,
    TRUNCATED,
    is_sensitive_key,
    redact,
    scrub_text,
)

__all__ = [
    "CORRELATION_KEYS",
    "MAX_SCRUB_CHARS",
    "REDACTED",
    "TRUNCATED",
    "LogFormat",
    "bind_context",
    "clear_context",
    "configure_logging",
    "get_context",
    "get_logger",
    "is_sensitive_key",
    "log_context",
    "redact",
    "redact_processor",
    "scrub_text",
    "unbind_context",
]

type LogFormat = Literal["json", "console"]

CORRELATION_KEYS: Final = ("job_id", "attempt_id", "node_id", "session_id")
"""Standard correlation fields; other keys (e.g. ``chunk_id``) may be bound too."""

# Keys added by ProcessorFormatter for foreign records; internal, never rendered.
_INTERNAL_KEYS: Final = frozenset({"_record", "_from_structlog"})

_HANDLER_MARK: Final = "_meshforge_handler"


def redact_processor(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    """structlog processor applying :func:`redact` to every field.

    The ``event`` message is free text, so only :func:`scrub_text` applies to it.
    """
    for key in list(event_dict):
        if key in _INTERNAL_KEYS:
            continue
        if key == "event":
            event_dict[key] = redact(event_dict[key])  # str -> scrub_text
        elif is_sensitive_key(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = redact(event_dict[key])
    return event_dict


def _add_failure_info(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    """Surface ``failure_type``/``retryable`` of a logged :class:`MeshForgeError`."""
    exc_info = event_dict.get("exc_info")
    exc: BaseException | None = None
    if isinstance(exc_info, BaseException):
        exc = exc_info
    elif isinstance(exc_info, tuple) and len(exc_info) == 3:
        exc = exc_info[1]
    elif exc_info is True:
        exc = sys.exc_info()[1]
    if isinstance(exc, MeshForgeError):
        event_dict.setdefault("failure_type", exc.failure_type.value)
        event_dict.setdefault("retryable", exc.retryable)
    return event_dict


def _exc_info_tuple(
    exc_info: object,
) -> tuple[type[BaseException], BaseException, Any] | None:
    if isinstance(exc_info, BaseException):
        return type(exc_info), exc_info, exc_info.__traceback__
    if isinstance(exc_info, tuple) and len(exc_info) == 3 and exc_info[1] is not None:
        return exc_info[0], exc_info[1], exc_info[2]
    if exc_info is True:
        current = sys.exc_info()
        if current[1] is not None:
            return current[0], current[1], current[2]
    return None


def _scrub_exception(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    """Scrub the structured exception (after ``ExceptionRenderer``) for JSON output."""
    if "exception" in event_dict:
        event_dict["exception"] = redact(event_dict["exception"])
    return event_dict


def _console_exception(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    """Render ``exc_info`` as a plain, locals-free, scrubbed traceback string.

    ``ConsoleRenderer`` prints a string ``exception`` field as-is, so its own (rich,
    locals-showing) traceback formatter is never reached.
    """
    exc = _exc_info_tuple(event_dict.pop("exc_info", None))
    if exc is not None:
        event_dict["exception"] = scrub_text("".join(traceback.format_exception(*exc)))
    elif isinstance(event_dict.get("exception"), str):
        event_dict["exception"] = scrub_text(event_dict["exception"])
    return event_dict


def _json_default(obj: object) -> object:
    """``json.dumps`` fallback: readable forms for common types; scrubbed ``repr`` else."""
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, bytes):
        return f"<{len(obj)} bytes>"
    return scrub_text(repr(obj))


def _component_adder(component: str) -> Processor:
    def add_component(
        _logger: WrappedLogger, _method: str, event_dict: EventDict
    ) -> EventDict:
        event_dict.setdefault("component", component)
        return event_dict

    return add_component


def _shared_processors(component: str | None) -> list[Processor]:
    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
    ]
    if component:
        processors.append(_component_adder(component))
    processors += [_add_failure_info, redact_processor]
    return processors


def configure_logging(
    level: str | int = "INFO",
    *,
    fmt: LogFormat = "json",
    component: str | None = None,
    stream: TextIO | None = None,
) -> None:
    """Configure structlog and the stdlib root logger. Safe to call repeatedly.

    Args:
        level: minimum level name (``"DEBUG"``…) or number, applied to the root logger.
        fmt: ``"json"`` (one JSON object per line) or ``"console"`` (dev).
        component: optional process role (``"controller"``, ``"agent"``…) added to
            every record as ``component``.
        stream: output stream; defaults to ``sys.stderr``.
    """
    numeric_level = (
        logging.getLevelNamesMapping().get(level.upper())
        if isinstance(level, str)
        else level
    )
    if numeric_level is None:
        raise ValueError(f"unknown log level: {level!r}")

    shared = _shared_processors(component)
    renderer: Processor
    final_chain: list[Processor] = [
        structlog.stdlib.ProcessorFormatter.remove_processors_meta
    ]
    if fmt == "json":
        final_chain.append(
            structlog.processors.ExceptionRenderer(
                ExceptionDictTransformer(show_locals=False, use_rich=False)
            )
        )
        final_chain.append(_scrub_exception)
        renderer = structlog.processors.JSONRenderer(default=_json_default)
    elif fmt == "console":
        final_chain.append(_console_exception)
        renderer = structlog.dev.ConsoleRenderer(
            colors=_isatty(stream or sys.stderr),
            # Fallback only; _console_exception normally pre-renders the traceback.
            exception_formatter=structlog.dev.plain_traceback,
        )
    else:
        raise ValueError(f"unknown log format: {fmt!r}")
    final_chain.append(renderer)

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=final_chain,
    )
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_MARK, True)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARK, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric_level)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # Not cached, so reconfiguring (tests, CLI --log-level) affects existing loggers.
        cache_logger_on_first_use=False,
    )


def _isatty(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def get_logger(name: str | None = None, **initial: Any) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger (stdlib-backed) named ``name``."""
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name, **initial)
    return logger


def bind_context(**fields: Any) -> None:
    """Bind correlation fields for the current context (task/thread). ``None`` is skipped.

    The bindings stay until unbound/cleared — they are *not* scoped. Prefer
    :func:`log_context`, which restores the previous values on exit.

    Warning — thread pools: a ``ThreadPoolExecutor`` worker thread keeps its own
    context across the unrelated jobs it runs, so ``bind_context`` inside pooled
    work leaks the fields into the next job on that thread. Inside pooled work use
    ``with log_context(...)``. Context *propagation* differs too:
    ``asyncio.to_thread`` copies the caller's context into the worker, while
    ``loop.run_in_executor`` / ``executor.submit`` do not (wrap the callable with
    ``contextvars.copy_context().run`` if the caller's fields are needed).
    Asyncio tasks are safe: each task works on its own copy of the context.
    """
    structlog.contextvars.bind_contextvars(
        **{k: v for k, v in fields.items() if v is not None}
    )


def unbind_context(*keys: str) -> None:
    structlog.contextvars.unbind_contextvars(*keys)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


def get_context() -> MutableMapping[str, Any]:
    """Currently bound context fields (a copy)."""
    return dict(structlog.contextvars.get_contextvars())


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Bind fields for the duration of a ``with`` block, restoring previous values after."""
    with structlog.contextvars.bound_contextvars(
        **{k: v for k, v in fields.items() if v is not None}
    ):
        yield
