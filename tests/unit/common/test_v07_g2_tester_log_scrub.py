"""V0.7 G2 tester: a join code reaches no log line through any logging path.

Real structlog/stdlib pipeline (``configure_logging`` to a buffer), checking the four
ways a code can get into a record - a bound field, a message, a stdlib ``%s``
argument and an exception (message and traceback) - plus the mutation check that
the test notices when the scrubber rule is disabled (checklist 20).
"""

# ruff: noqa: G003  (tester file: adversarial literals, blunt asserts)

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from meshforge.common import join_code, redaction
from meshforge.common import logging as mf_logging
from meshforge.common.logging import configure_logging, get_logger

TOKEN = bytes(range(32, 48))
CODE = join_code.encode(
    join_code.JoinCode(host="192.168.1.20", port=47101, token=TOKEN, ca_pin="cd" * 16)
)
BODY = CODE.split("-", 1)[1]


def _emit() -> str:
    buffer = io.StringIO()
    configure_logging("DEBUG", stream=buffer)
    log = get_logger("tester.join")
    log.info("joining with " + CODE)
    log.info("bound field", detail=CODE, other={"nested": [CODE, f"x {CODE} y"]})
    log.info("sensitive key", join_code=CODE, token=TOKEN.hex())
    logging.getLogger("tester.stdlib").error("stdlib %s and %r", CODE, CODE.upper())
    try:
        raise ValueError(f"cannot join with {CODE}")
    except ValueError:
        log.exception("failed")
    try:
        try:
            raise ValueError(CODE)
        except ValueError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError:
        logging.getLogger("tester.stdlib").exception("chained %s", "x")
    return buffer.getvalue()


def _leaks(output: str) -> list[str]:
    flat = output.lower().replace("\\n", "")
    wanted = [CODE, CODE.upper(), BODY, BODY.replace("-", ""), TOKEN.hex()]
    return [w for w in wanted if w.lower() in flat]


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    yield
    configure_logging("INFO")


def test_no_logging_path_leaks_the_code_or_token() -> None:
    output = _emit()
    assert output  # something was logged
    assert _leaks(output) == [], _leaks(output)


def test_the_check_is_not_vacuous_it_fails_when_the_scrub_rule_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re

    monkeypatch.setattr(redaction, "_JOIN_CODE_RE", re.compile(r"(?!x)x"))
    leaked = _leaks(_emit())
    assert leaked, "mutation (scrubber rule disabled) was not detected"
    assert mf_logging  # imported for its side-effect-free re-exports
