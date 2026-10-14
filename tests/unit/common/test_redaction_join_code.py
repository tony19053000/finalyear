"""V0.7 G2 (checklist 20): a join code never reaches a log line.

The code is a single-use credential (128-bit token). ``common.redaction`` masks
its ``mf1-...`` shape in every string of every record - also when it sits under
an innocent key or inside an exception message. The mutation test proves the
first test fails if the scrubber's join-code rule is removed.
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Iterator

import pytest
import structlog

from meshforge.common import redaction
from meshforge.common.join_code import JoinCode, encode, new_token
from meshforge.common.logging import (
    REDACTED,
    clear_context,
    configure_logging,
    get_logger,
    scrub_text,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _code() -> str:
    return encode(
        JoinCode(host="192.168.1.10", port=47101, token=new_token(), ca_pin="ab" * 16)
    )


@pytest.fixture
def json_stream() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    configure_logging("INFO", fmt="json", stream=buf)
    yield buf
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_meshforge_handler", False):
            root.removeHandler(handler)
    clear_context()
    structlog.reset_defaults()


def _leaks(code: str, text: str) -> bool:
    groups = code.split("-")[1:-1]  # every full 5-character group
    return any(group in text for group in groups)


def test_join_code_is_scrubbed_from_a_log_record(json_stream: io.StringIO) -> None:
    code = _code()
    get_logger("t").info(
        "user.pasted", text=f"joining with {code} now", note=code.upper()
    )
    line = json_stream.getvalue()
    record = json.loads(line)
    assert not _leaks(code, line), line
    assert record["text"] == f"joining with mf1-{REDACTED} now"
    assert REDACTED in record["note"]


def test_join_code_in_an_exception_message_is_scrubbed() -> None:
    code = _code()
    scrubbed = scrub_text(f"ValueError: bad code {code}")
    assert not _leaks(code, scrubbed)


def test_short_mf_words_are_left_alone() -> None:
    text = "the mf1-format; xmf1-aaaaaaaaaaaaaaaaaaaa; mf1-abc"
    assert scrub_text(text) == text


def test_scrub_stays_linear_on_a_hostile_string() -> None:
    import time

    start = time.perf_counter()
    scrub_text("mf1-" + "a-" * 30_000)
    scrub_text("mf1-a" * 10_000)
    assert time.perf_counter() - start < 1.0


def test_mutation_without_the_rule_the_code_leaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checklist 20 mutation check: with the join-code rule disabled the first
    test's assertion would fail."""
    code = _code()
    monkeypatch.setattr(redaction, "_JOIN_CODE_RE", re.compile(r"(?!)"))
    assert _leaks(code, scrub_text(f"joining with {code} now"))
