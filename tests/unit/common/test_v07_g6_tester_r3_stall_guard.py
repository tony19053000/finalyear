"""V0.7 G6 tester r3 (T-17 fail-closed guard): when a loop guard fires, the
output is value-free, it is confined to THAT matcher (= that attempt), length
is kept for the controller style, and nothing in the product tells the operator
(T-22).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from meshforge.common import secret_redaction as sr
from meshforge.common.secret_redaction import (
    RedactionStallError,
    SecretMatcher,
    StreamRedactor,
)

pytestmark = pytest.mark.unit

SECRETS = {"S0": b"00aaaa000a", "S1": b"a000a"}
STREAM = b"hello 30306161616130303061 world 00aaaa000a a000a tail\n"


def _force_stall_for(matcher: SecretMatcher, monkeypatch: pytest.MonkeyPatch) -> None:
    real = sr._Walker.run

    def run(self: sr._Walker, limit: int):  # type: ignore[no-untyped-def]
        if self.m is matcher:
            raise RedactionStallError("injected")
        return real(self, limit)

    monkeypatch.setattr(sr._Walker, "run", run)


@pytest.mark.parametrize("preserve", [False, True])
def test_a_stalled_matcher_blanks_only_its_own_streams_and_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch, preserve: bool
) -> None:
    mine, theirs = SecretMatcher(SECRETS), SecretMatcher(SECRETS)
    _force_stall_for(mine, monkeypatch)
    a = StreamRedactor(mine, length_preserving=preserve)
    a2 = StreamRedactor(mine, length_preserving=preserve)  # my other stream
    b = StreamRedactor(theirs, length_preserving=preserve)  # another job
    out_a = a.feed(STREAM[:20]) + a.feed(STREAM[20:]) + a.flush()
    out_a2 = a2.feed(STREAM) + a2.flush()
    out_b = b.feed(STREAM) + b.flush()
    # theirs still works... but the patched run raises only for `mine`
    assert b"[secret:S" in out_b or preserve
    assert a.failed and a2.failed and mine.stalled and not theirs.stalled
    for out in (out_a, out_a2):
        assert set(out) == {ord("*")}, out
        assert len(out) == len(STREAM)
    assert b"00aaaa000a" not in out_b and b"30306161616130303061" not in out_b
    if preserve:
        assert len(out_b) == len(STREAM)


def test_after_a_stall_later_feeds_and_flush_stay_blank_and_do_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = SecretMatcher(SECRETS)
    _force_stall_for(m, monkeypatch)
    r = StreamRedactor(m, length_preserving=True)
    outs = [r.feed(b"x" * n) for n in (1, 5, 100)]
    assert all(set(o) <= {ord("*")} for o in outs)
    assert [len(o) for o in outs] == [1, 5, 100]
    assert set(r.flush()) <= {ord("*")}


@pytest.mark.xfail(
    strict=True,
    reason="T-22 (minor): a redactor that failed closed is invisible - no log line, "
    "counter or job note; the user just sees `*` where output was",
)
def test_a_fail_closed_redactor_is_reported_somewhere() -> None:
    src = Path(sr.__file__).parents[1]
    pattern = re.compile(r"RedactionStallError|\.stalled\b|redact\w*\.failed\b")
    readers = [
        str(path)
        for path in src.rglob("*.py")
        if path != Path(sr.__file__) and pattern.search(path.read_text())
    ]
    assert readers, "nothing outside the module reads the fail-closed flag"
