"""Adversarial tests for task 0.1.6 logging: secret-leak scenarios (ARCHITECTURE §16, §25).

These tests assert the *safe* behaviour a "no secrets in logs" guarantee implies.
Where the current implementation does not meet that bar, the test fails and is
left failing (not weakened) so the gap is visible; see the tester's report for
severity judgement on each.
"""

from __future__ import annotations

import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from meshforge.common.logging import (
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
    is_sensitive_key,
    log_context,
    redact,
)

pytestmark = pytest.mark.unit


def _lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


@pytest.fixture
def stream() -> io.StringIO:
    buf = io.StringIO()
    configure_logging("INFO", fmt="json", stream=buf)
    return buf


# --------------------------------------------------------------------- odd-case keys


@pytest.mark.parametrize(
    "key",
    [
        "Authorization",
        "X-Auth-Token",
        "PASSWORD",
        "passwd",
        "pwd",
        "private_key_pem",
        "credentials",
        "cookie",
        "set-cookie",
        "Set-Cookie",
    ],
)
def test_odd_cased_sensitive_keys_are_redacted(stream: io.StringIO, key: str) -> None:
    log = get_logger("t")
    log.info("event", **{key: "super-secret-value"})
    (record,) = _lines(stream)
    assert record[key] == "[REDACTED]", f"{key} leaked: {record[key]!r}"


@pytest.mark.parametrize("key", ["dsn", "DSN", "database_url", "db_url"])
def test_dsn_and_database_url_keys_are_redacted(stream: io.StringIO, key: str) -> None:
    """DEFECT: a DSN/database URL commonly embeds `user:pass@host` credentials;
    the key is not in the sensitive word list, so the whole connection string
    (including the password) is logged verbatim. High risk if any component ever
    logs its database URL for diagnostics.
    """
    log = get_logger("t")
    secret_dsn = "postgresql://appuser:hunter2SECRET@db.internal:5432/meshforge"
    log.info("connecting", **{key: secret_dsn})
    (record,) = _lines(stream)
    assert record[key] == "[REDACTED]", (
        f"{key} leaked a full DSN with credentials: {record[key]!r}"
    )


# --------------------------------------------------------------- free-text message


def test_secret_embedded_in_fstring_message_is_not_leaked(stream: io.StringIO) -> None:
    """DEFECT: the `event` key is explicitly skipped by redact_processor, so any
    secret interpolated into the human-readable message text (a very easy mistake,
    e.g. ``log.info(f"connected with token={token}")``) is written verbatim.
    """
    log = get_logger("t")
    token = "sk-live-VERY-SECRET-TOKEN-abc123"
    log.info(f"connected with token={token}")  # noqa: G004 - the mistake under test
    (record,) = _lines(stream)
    assert token not in record["event"], (
        f"secret token leaked via free-text log message: {record['event']!r}"
    )


def test_secret_in_exception_message_is_not_leaked_via_traceback(
    stream: io.StringIO,
) -> None:
    """DEFECT: exception messages (and hence tracebacks rendered by
    ExceptionDictTransformer) are not passed through redact(); any exception whose
    ``str()`` embeds a secret (e.g. a driver error echoing a DSN or password) is
    logged verbatim even though local variables are stripped.
    """
    log = get_logger("t")
    secret = "hunter2-actual-password"
    try:
        raise ValueError(f"authentication failed for password={secret}")
    except ValueError:
        log.exception("db.connect.failed")
    (record,) = _lines(stream)
    rendered = json.dumps(record)
    assert secret not in rendered, f"secret leaked via exception traceback: {rendered}"


# ------------------------------------------------------------- nested secret shapes


def test_secretstr_inside_plain_dict_list_is_redacted(stream: io.StringIO) -> None:
    log = get_logger("t")
    log.info("creds", tokens=[SecretStr("abc123"), "plain-value"])
    (record,) = _lines(stream)
    assert record["tokens"] == ["[REDACTED]", "plain-value"]


class _CredModel(BaseModel):
    password: str
    username: str


def test_secret_field_inside_pydantic_model_value_is_redacted(
    stream: io.StringIO,
) -> None:
    """DEFECT: redact() only recurses into Mapping/list/tuple/set/frozenset. A
    pydantic BaseModel instance logged directly as a single field value (a
    realistic pattern: `log.info("db.connect", settings=my_settings)`) is not
    inspected at all, so a plain (non-SecretStr) sensitive field inside it leaks
    in full via the model's default repr.
    """
    log = get_logger("t")
    cred = _CredModel(password="plaintext-super-secret", username="bob")
    log.info("model-field", cred=cred)
    (record,) = _lines(stream)
    rendered = json.dumps(record)
    assert "plaintext-super-secret" not in rendered, (
        f"secret leaked via nested pydantic model field: {rendered}"
    )


def test_redact_helper_handles_pydantic_models_directly() -> None:
    """Same defect, exercised directly against the redact() helper."""
    cred = _CredModel(password="plaintext-super-secret", username="bob")
    result = redact(cred)
    rendered = repr(result)
    assert "plaintext-super-secret" not in rendered


# --------------------------------------------------------------------- Bearer/Basic


def test_bearer_and_basic_strings_redacted_anywhere_in_value(
    stream: io.StringIO,
) -> None:
    log = get_logger("t")
    log.info(
        "headers",
        auth_header="Bearer abc.def.ghi",
        basic_header="Basic dXNlcjpwYXNz",
    )
    (record,) = _lines(stream)
    assert record["auth_header"] == "[REDACTED]"
    assert record["basic_header"] == "[REDACTED]"


# --------------------------------------------------------- contextvars / concurrency


def test_contextvars_do_not_leak_between_sequential_tasks_on_reused_thread(
    stream: io.StringIO,
) -> None:
    """``log_context`` must not leak across jobs run on a reused pool thread.

    Note (lead decision D7, documented in ``bind_context``): the low-level
    ``bind_context`` *without* a matching clear CAN leak into the next job on the
    same pooled thread — that is documented behaviour, not a bug. ``log_context``
    is the safe API for pooled work and is what this test pins.
    """
    log = get_logger("t")

    def work_bind_no_clear(job_id: str) -> None:
        with log_context(job_id=job_id):
            log.info("processing")

    def work_no_bind() -> None:
        log.info("unrelated task, no job context")

    with ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(work_bind_no_clear, "job_LEAKY").result()
        ex.submit(work_no_bind).result()

    records = _lines(stream)
    assert records[0]["job_id"] == "job_LEAKY"
    assert "job_id" not in records[1], (
        f"correlation id leaked into unrelated task: {records[1]}"
    )


def test_log_context_manager_cleans_up_even_on_exception(stream: io.StringIO) -> None:
    log = get_logger("t")
    with pytest.raises(RuntimeError), log_context(job_id="job_X"):
        raise RuntimeError("boom")
    log.info("after")
    (record,) = _lines(stream)
    assert "job_id" not in record


def test_contextvars_isolated_across_real_threads(stream: io.StringIO) -> None:
    log = get_logger("t")
    barrier = threading.Barrier(5)

    def work(n: int) -> None:
        with log_context(job_id=f"job_{n}"):
            barrier.wait(timeout=5)
            time.sleep(0.01)
            log.info("threaded")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seen = {r["job_id"] for r in _lines(stream)}
    assert seen == {f"job_{i}" for i in range(5)}


def test_configure_logging_concurrent_calls_leave_single_handler() -> None:
    errors: list[BaseException] = []

    def reconfig(i: int) -> None:
        try:
            buf = io.StringIO()
            configure_logging("INFO", fmt="json", stream=buf)
            get_logger("t").info("hello", i=i)
        except BaseException as exc:  # noqa: BLE001 - capturing for assertion
            errors.append(exc)

    threads = [threading.Thread(target=reconfig, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    import logging as stdlib_logging

    handlers = [
        h
        for h in stdlib_logging.getLogger().handlers
        if getattr(h, "_meshforge_handler", False)
    ]
    assert len(handlers) == 1


def test_clear_context_after_use(stream: io.StringIO) -> None:
    bind_context(job_id="job_stale")
    clear_context()
    get_logger("t").info("clean")
    (record,) = _lines(stream)
    assert "job_id" not in record


# ------------------------------------------------------------------- unicode safety


def test_unicode_secret_values_still_redacted_by_key(stream: io.StringIO) -> None:
    log = get_logger("t")
    log.info("event", password="mötdePässe-é中文")
    (record,) = _lines(stream)
    assert record["password"] == "[REDACTED]"


@pytest.mark.parametrize("key", ["num_tokens", "idempotency_key"])
def test_non_sensitive_lookalike_keys_are_not_redacted(key: str) -> None:
    """Regression guard: word-part matching must not over-redact telemetry keys
    that the module docstring explicitly promises to keep readable.
    """
    assert is_sensitive_key(key) is False
