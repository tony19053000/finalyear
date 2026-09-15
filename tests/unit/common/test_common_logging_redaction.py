"""Task 0.1.6 fix round: redaction shapes, containers, console tracebacks."""

import collections
import dataclasses
import io
import json
import logging
from collections.abc import Iterator
from typing import Any, NamedTuple, Self

import pytest
import structlog
from pydantic import BaseModel

from meshforge.common.logging import (
    REDACTED,
    TRUNCATED,
    clear_context,
    configure_logging,
    get_logger,
    is_sensitive_key,
    redact,
    scrub_text,
)

pytestmark = pytest.mark.unit


def _reset_logging() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_meshforge_handler", False):
            root.removeHandler(handler)
    clear_context()
    structlog.reset_defaults()


@pytest.fixture
def json_stream() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    configure_logging("INFO", fmt="json", stream=buf)
    yield buf
    _reset_logging()


@pytest.fixture
def console_stream() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    configure_logging("INFO", fmt="console", stream=buf)
    yield buf
    _reset_logging()


# ------------------------------------------------------------------- R4/D1: keys


@pytest.mark.parametrize(
    "key",
    [
        "dsn",
        "database_url",
        "DATABASE_URL",
        "db_url",
        "connection_string",
        "connectionString",
        "otp",
        "jwt",
        "pin",
        "user_pin",
        "join_code",
        "pairing_code",
        "session_key",
        "refresh_token",
        "refresh_secret",
    ],
)
def test_new_sensitive_keys(key: str) -> None:
    assert is_sensitive_key(key) is True


@pytest.mark.parametrize(
    "key", ["pin_memory", "session_id", "join_url", "code", "refresh"]
)
def test_lookalike_keys_stay_readable(key: str) -> None:
    assert is_sensitive_key(key) is False


# ------------------------------------------------------- R4/D1: value scrubbing


def test_url_credentials_are_scrubbed_but_user_and_host_kept() -> None:
    assert (
        scrub_text("postgresql://app:hunter2@db.internal:5432/mf")
        == f"postgresql://app:{REDACTED}@db.internal:5432/mf"
    )
    assert scrub_text("see https://example.org/a:b") == "see https://example.org/a:b"


def test_bearer_and_basic_matched_anywhere_in_text() -> None:
    out = scrub_text("sent header Bearer abc.def-ghi and Basic dXNlcjpwYXNz= ok")
    assert "abc.def-ghi" not in out and "dXNlcjpwYXNz" not in out
    assert out == f"sent header Bearer {REDACTED} and Basic {REDACTED} ok"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A non-sensitive "key: value" must not swallow the sensitive pair after it.
        ("login failed: password=pw1", f"login failed: password={REDACTED}"),
        ("a: b token=t1 c=d", f"a: b token={REDACTED} c=d"),
        ('{"token": "t2", "n": 1}', f'{{"token": "{REDACTED}", "n": 1}}'),
        ("token='t3' pwd=t4", f"token='{REDACTED}' pwd={REDACTED}"),
        ("count=3 status: ok", "count=3 status: ok"),
    ],
)
def test_key_value_pairs_in_text(text: str, expected: str) -> None:
    assert scrub_text(text) == expected


def test_url_credentials_in_non_sensitive_field_are_scrubbed(
    json_stream: io.StringIO,
) -> None:
    get_logger("t").info("x", target="redis://:s3cr3t@cache:6379/0")
    (rec,) = [json.loads(line) for line in json_stream.getvalue().splitlines()]
    assert rec["target"] == f"redis://:{REDACTED}@cache:6379/0"


# ------------------------------------------------ D2: event + exception scrubbing


def test_event_text_is_scrubbed(json_stream: io.StringIO) -> None:
    get_logger("t").info("connecting to postgresql://u:pw-secret@h/db")
    assert "pw-secret" not in json_stream.getvalue()


def _raise_with_local_secret() -> None:
    local_secret = "local-var-secret-value"  # noqa: F841 - must never be rendered
    raise RuntimeError("login failed: password=exc-msg-secret url=ftp://u:p4ss@h")


def test_console_traceback_has_no_locals_and_is_scrubbed(
    console_stream: io.StringIO,
) -> None:
    try:
        _raise_with_local_secret()
    except RuntimeError:
        get_logger("t").exception("op.failed")
    out = console_stream.getvalue()
    assert "Traceback" in out and "RuntimeError" in out
    assert "local-var-secret-value" not in out  # R3: no locals
    assert "exc-msg-secret" not in out and "p4ss" not in out  # D2
    assert f"password={REDACTED}" in out


def test_json_traceback_has_no_locals_and_is_scrubbed(
    json_stream: io.StringIO,
) -> None:
    try:
        _raise_with_local_secret()
    except RuntimeError:
        get_logger("t").exception("op.failed")
    out = json_stream.getvalue()
    assert "local-var-secret-value" not in out
    assert "exc-msg-secret" not in out and "p4ss" not in out


# ------------------------------------------------- R1 / D3 / D4: container shapes


class _Creds(NamedTuple):
    user: str
    token: str


class _Weird(collections.namedtuple("_Weird", ["a"])):
    """Namedtuple whose constructor signature differs from its fields."""

    def __new__(cls, a: str, *, strict: bool) -> Self:
        return super().__new__(cls, a)


def test_namedtuple_is_redacted_by_field_name_without_crashing() -> None:
    out = redact(_Creds("bob", "tok-value"))
    assert out == _Creds("bob", REDACTED)
    assert isinstance(out, _Creds)
    # A namedtuple whose constructor cannot be re-called degrades to a dict keyed
    # by field name (keeps the names readable in the log).
    assert redact(_Weird("x", strict=True)) == {"a": "x"}


def test_namedtuple_logged_as_field(json_stream: io.StringIO) -> None:
    get_logger("t").info("x", creds=_Creds("bob", "tok-value"))
    assert "tok-value" not in json_stream.getvalue()


class _Model(BaseModel):
    username: str
    password: str
    dsn: str


@dataclasses.dataclass
class _DC:
    host: str
    api_key: str
    nested: _Model


def test_pydantic_model_and_dataclass_are_redacted() -> None:
    model = _Model(username="bob", password="pw-secret", dsn="postgres://a:b@c/d")
    assert redact(model) == {"username": "bob", "password": REDACTED, "dsn": REDACTED}
    out = redact(_DC(host="h", api_key="k-secret", nested=model))
    assert out == {
        "host": "h",
        "api_key": REDACTED,
        "nested": {"username": "bob", "password": REDACTED, "dsn": REDACTED},
    }


def test_deep_nesting_is_truncated_not_emitted_raw() -> None:
    value: Any = {"password": "deep-secret"}
    for _ in range(20):
        value = {"n": value}
    rendered = json.dumps(redact(value))
    assert TRUNCATED in rendered
    assert "deep-secret" not in rendered
