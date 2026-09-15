"""Task 0.1.6: structlog JSON logging, correlation context, redaction."""

import asyncio
import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog
from pydantic import SecretStr

from meshforge.common.errors import ConfigError
from meshforge.common.logging import (
    REDACTED,
    bind_context,
    clear_context,
    configure_logging,
    get_context,
    get_logger,
    is_sensitive_key,
    log_context,
    redact,
)

pytestmark = pytest.mark.unit


def _our_handlers() -> list[logging.Handler]:
    return [
        h
        for h in logging.getLogger().handlers
        if getattr(h, "_meshforge_handler", False)
    ]


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    root = logging.getLogger()
    old_level = root.level
    clear_context()
    configure_logging("INFO", stream=buf)
    yield buf
    for handler in _our_handlers():
        root.removeHandler(handler)
    root.setLevel(old_level)
    clear_context()
    structlog.reset_defaults()


def _records(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_json_output_shape(stream: io.StringIO) -> None:
    get_logger("meshforge.test").info("thing.happened", count=3)
    (rec,) = _records(stream)
    assert rec["event"] == "thing.happened"
    assert rec["level"] == "info"
    assert rec["logger"] == "meshforge.test"
    assert rec["count"] == 3
    ts = datetime.fromisoformat(rec["timestamp"])
    assert ts.utcoffset() == UTC.utcoffset(None)
    assert abs((datetime.now(UTC) - ts).total_seconds()) < 5


def test_level_filtering(stream: io.StringIO) -> None:
    log = get_logger("x")
    log.debug("hidden")
    log.warning("shown")
    assert [r["event"] for r in _records(stream)] == ["shown"]


def test_correlation_context_and_restore(stream: io.StringIO) -> None:
    log = get_logger("x")
    bind_context(node_id="node_A", session_id=None)
    with log_context(job_id="job_1", attempt_id="att_1"):
        log.info("inside")
        with log_context(attempt_id="att_2"):
            log.info("nested")
        log.info("after-nested")
    log.info("outside")
    inside, nested, after, outside = _records(stream)
    assert (inside["job_id"], inside["attempt_id"], inside["node_id"]) == (
        "job_1",
        "att_1",
        "node_A",
    )
    assert "session_id" not in inside  # None values are not bound
    assert nested["attempt_id"] == "att_2"
    assert after["attempt_id"] == "att_1"
    assert "job_id" not in outside and outside["node_id"] == "node_A"
    assert get_context() == {"node_id": "node_A"}


def test_stdlib_third_party_logs_are_json_with_context_and_redaction(
    stream: io.StringIO,
) -> None:
    with log_context(job_id="job_9"):
        logging.getLogger("thirdparty.lib").warning(
            "connected to %s", "db", extra={"password": "hunter2"}
        )
    (rec,) = _records(stream)
    assert rec["event"] == "connected to db"
    assert rec["logger"] == "thirdparty.lib"
    assert rec["level"] == "warning"
    assert rec["job_id"] == "job_9"
    assert rec["password"] == REDACTED
    assert "hunter2" not in stream.getvalue()


def test_redaction_of_keys_values_and_nesting(stream: io.StringIO) -> None:
    get_logger("x").info(
        "req",
        password="p1-secret-value",
        api_key="p2-secret-value",
        apiKey="p3-secret-value",
        auth_token="p4-secret-value",
        headers={"Authorization": "p5-secret-value", "Accept": "json"},
        items=[{"client_secret": "p6-secret-value"}, ("ok",)],
        sec=SecretStr("p7-secret-value"),
        note="Bearer p8-secret-value",
        num_tokens=12,
        idempotency_key="ik-1",
    )
    text = stream.getvalue()
    for i in range(1, 9):
        assert f"p{i}-secret-value" not in text
    (rec,) = _records(stream)
    assert rec["headers"] == {"Authorization": REDACTED, "Accept": "json"}
    assert rec["items"][0] == {"client_secret": REDACTED}
    assert rec["sec"] == REDACTED
    assert rec["note"] == REDACTED
    assert rec["num_tokens"] == 12
    assert rec["idempotency_key"] == "ik-1"


@pytest.mark.parametrize(
    ("key", "sensitive"),
    [
        ("password", True),
        ("db_password", True),
        ("Authorization", True),
        ("join_token", True),
        ("accessToken", True),
        ("private_key", True),
        ("privateKey", True),
        ("x-api-key", True),
        ("key", True),
        ("set-cookie", True),
        ("credentials", True),
        ("num_tokens", False),
        ("tokens_per_sec", False),
        ("idempotency_key", False),
        ("cache_key", False),
        ("job_id", False),
        ("", False),
        (3, False),
    ],
)
def test_is_sensitive_key(key: object, sensitive: bool) -> None:
    assert is_sensitive_key(key) is sensitive


def test_redact_preserves_container_types() -> None:
    value = {"a": ("x", SecretStr("s")), "b": {"token": "t"}, "c": [1]}
    assert redact(value) == {"a": ("x", REDACTED), "b": {"token": REDACTED}, "c": [1]}


def test_asyncio_tasks_have_isolated_context(stream: io.StringIO) -> None:
    log = get_logger("x")

    async def attempt(job: str) -> None:
        bind_context(job_id=job)
        for step in range(3):
            await asyncio.sleep(0)
            log.info("step", step=step)

    async def main() -> None:
        await asyncio.gather(attempt("job_a"), attempt("job_b"))

    asyncio.run(main())
    recs = _records(stream)
    assert len(recs) == 6
    by_job: dict[str, list[int]] = {}
    for r in recs:
        by_job.setdefault(r["job_id"], []).append(r["step"])
    assert by_job == {"job_a": [0, 1, 2], "job_b": [0, 1, 2]}
    assert "job_id" not in get_context()  # tasks' bindings did not leak out


def test_exception_logging_is_structured_without_locals(stream: io.StringIO) -> None:
    def load() -> None:
        local_secret = "topsecret-local"  # noqa: F841 - must not appear in the log
        raise ConfigError("bad config")

    try:
        load()
    except ConfigError:
        get_logger("x").exception("config.failed")
    text = stream.getvalue()
    assert "topsecret-local" not in text
    (rec,) = _records(stream)
    assert rec["failure_type"] == "config_error"
    assert rec["retryable"] is False
    assert rec["level"] == "error"
    assert isinstance(rec["exception"], list)
    assert rec["exception"][0]["exc_type"] == "ConfigError"


def test_component_field_and_idempotent_reconfigure() -> None:
    buf = io.StringIO()
    try:
        configure_logging("INFO", stream=io.StringIO())
        configure_logging("DEBUG", stream=buf, component="agent")
        assert len(_our_handlers()) == 1
        get_logger("x").debug("dbg")
        (rec,) = _records(buf)
        assert rec["component"] == "agent"
    finally:
        for handler in _our_handlers():
            logging.getLogger().removeHandler(handler)
        structlog.reset_defaults()


def test_console_format_is_human_readable() -> None:
    buf = io.StringIO()
    try:
        configure_logging("INFO", fmt="console", stream=buf)
        get_logger("x").info("hello.console", token="tok-secret-value")
        out = buf.getvalue()
        assert "hello.console" in out
        assert "tok-secret-value" not in out
    finally:
        for handler in _our_handlers():
            logging.getLogger().removeHandler(handler)
        structlog.reset_defaults()


def test_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        configure_logging("LOUD")
    with pytest.raises(ValueError):
        configure_logging("INFO", fmt="xml")  # type: ignore[arg-type]
    assert _our_handlers() == []
