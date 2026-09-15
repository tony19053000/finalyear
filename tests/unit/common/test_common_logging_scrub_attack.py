"""Tester re-test of task 0.1.6 fix round: adversarial attack on ``scrub_text``.

Independent of the implementer's own ``test_common_logging_redaction.py`` /
``test_common_logging_adversarial.py``. Covers:

* false positives on ordinary telemetry (URLs, flags, IDs, IPv6, paths with ``@``);
* performance on large inputs (a slow-but-linear scrubber is a latency bug in the
  hot logging path; a super-linear one is a denial-of-service);
* secrets whose value contains a character the ``key=value`` regex treats as a
  terminator (``]`` ``)`` ``}`` ``;`` ``&`` ``'``) — this is a **secret-leak**
  defect, not merely cosmetic, when found.

Where the implementation does not meet the bar, the test fails and is left
failing (not weakened) — see the tester's report for severity.
"""

from __future__ import annotations

import io
import logging as stdlib_logging
import time

import pytest
import structlog

from meshforge.common.logging import REDACTED, configure_logging, get_logger, scrub_text

pytestmark = pytest.mark.unit


def _reset_logging() -> None:
    root = stdlib_logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_meshforge_handler", False):
            root.removeHandler(handler)
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()


# --------------------------------------------------------------------- false positives


@pytest.mark.parametrize(
    "text",
    [
        "url=http://host:8080/path",
        "pin_memory=True",
        "session_id=sess_01J8Z3K5Q9V6W2X4Y7A0B1C2D3",
        "url=http://[2001:db8::1]:8080/path",
        "path=/home/user@example.com/file",
        "num_tokens=5",
        "idempotency_key=abc123",
        "retry_count=3",
        "status=200 latency_ms=45",
        "GET /api?limit=10&offset=20",
        "duration=12.5s status=ok",
        "time: 12:30:00 event: started",
        "contact user@example.com for help",
        "progress=50%",
        r"C:\Users\me\file.txt",
        '{"user": "bob", "count": 5}',
    ],
)
def test_ordinary_telemetry_is_not_mangled(text: str) -> None:
    assert scrub_text(text) == text, f"false positive: {text!r} -> {scrub_text(text)!r}"


# -------------------------------------------------------------------------- performance


def test_scrub_text_perf_on_large_ordinary_message() -> None:
    """A 100 KB ordinary (non-secret) log message must scrub in well under 50 ms.

    Regression guard for pathological regex backtracking in the hot logging path
    (every log record's ``event`` and every string field goes through this).
    """
    # 57 chars x 1800 = 102 600 (the original x1700 = 96 900 tripped the size guard).
    big = ("component=agent state=running heartbeat=ok node=laptop-1 ") * 1800
    assert len(big) >= 100_000
    start = time.perf_counter()
    scrub_text(big)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.05, f"scrub_text took {elapsed * 1000:.1f} ms for 100 KB input"


def test_scrub_text_perf_on_single_long_token() -> None:
    """A single 20 000-character run of word characters (no separators) — e.g. a
    huge base64 blob, a long identifier, or an attacker-controlled field value —
    must not cause catastrophic (quadratic+) regex backtracking.

    DEFECT (if failing): ``_KV_RE``'s greedy key part combined with per-position
    ``finditer`` scanning is O(n^2) on a long run of identifier characters with no
    ``=``/``:`` separator. Empirically: 2000 chars ~0.06 s, 4000 ~0.26 s (4.2x for
    2x input), 8000 ~1.0 s, 16000 ~4.3 s — clean O(n^2) growth. At 100 KB+ (a
    realistic single-field payload: a serialized tensor summary, a long path, a
    stack trace line) this hangs the whole logging pipeline for the process,
    which is a denial-of-service, not just slow logging.
    """
    token = "x" * 8_000
    start = time.perf_counter()
    scrub_text(token)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, (
        f"scrub_text took {elapsed * 1000:.1f} ms for an 8000-char token with no "
        f"separators (catastrophic backtracking in _KV_RE)"
    )


# ------------------------------------------------------------- secret leak via bracket


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("password=sup3r]s3cr3t", "s3cr3t"),
        ("token=ab)cd;ef", "cd;ef"),
        ("api_key=xy&z123", "z123"),
        ("password=it's-a-secret", "s-a-secret"),
        ('password={"a":"topsecret"}', "topsecret"),
        ("dsn=postgres://u:p@host/db", "host/db"),
    ],
)
def test_secret_value_is_not_truncated_and_leaked_after_special_char(
    text: str, secret: str
) -> None:
    """DEFECT: the ``_KV_RE`` value lookahead stops at the first ``)`` ``}`` ``]``
    ``;`` ``&`` ``,`` character. Everything in the secret value *after* that
    character is copied to the output verbatim instead of being redacted —
    a genuine plaintext secret leak, not merely a cosmetic artifact (a stray
    bracket in the output). Real secrets containing any of these characters
    (JSON-shaped values, DSNs with a path segment, base64 with padding-adjacent
    punctuation, passphrases with punctuation) are only partially masked.
    """
    out = scrub_text(text)
    assert secret not in out, f"secret leaked: {text!r} -> {out!r}"


def test_db_url_with_embedded_credentials_is_fully_redacted() -> None:
    """A ``db_url``/``dsn`` value commonly embeds ``user:pass@host``; the URL-creds
    pass runs first and inserts a literal ``[REDACTED]``, whose closing ``]``
    then truncates the *outer* key=value redaction, leaking host/port/db and
    producing malformed output (``db_url=[REDACTED]]@host:5432/db``).
    """
    text = "db_url=postgres://u:p@host:5432/db"
    out = scrub_text(text)
    assert "host:5432/db" not in out, f"host/port/db leaked: {out!r}"
    assert out == f"db_url={REDACTED}", f"malformed/partial redaction: {out!r}"


def test_dsn_in_exception_message_leaks_host_via_json_log() -> None:
    """End-to-end repro: a realistic driver error message
    (``"failed to connect: dsn=postgres://svc:pw@db.internal:5432/prod"``) logged
    via ``log.exception(...)`` leaks the host/port/db-name (and, for passwords
    containing ``)``/``}``/``]``/``;``/``&``/``'``, part of the password itself)
    into the JSON log line, because the exception-message scrubbing path goes
    through :func:`scrub_text` exactly like any other free text.
    """
    buf = io.StringIO()
    configure_logging("INFO", fmt="json", stream=buf)
    try:
        log = get_logger("t")
        try:
            raise ConnectionError(
                "failed to connect: dsn=postgres://svc:hunter2@db.internal:5432/prod"
            )
        except ConnectionError:
            log.exception("db.connect.failed")
        line = buf.getvalue().strip()
    finally:
        _reset_logging()
    assert "hunter2" not in line
    assert "db.internal:5432/prod" not in line, f"DSN host/db leaked: {line}"
