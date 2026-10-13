# ruff: noqa: S311
"""V0.7 G6 fix round 2 (D163, T-17): the matcher terminates and is exact.

* the shrunk T-17 hangs and the missed redaction found while fixing it, as
  explicit regressions;
* a small-alphabet fuzz against a brute-force oracle (2-3 symbols, values of
  4-12 bytes, 1-4 secrets, every form, random chunkings, both styles, two
  streams per matcher) - where overlaps between forms are dense. Each feed
  runs under a CPU-time limit, so a spinning loop fails instead of freezing
  the suite;
* the loop guards: a matcher whose progress is broken on purpose withholds the
  output (``*``) instead of spinning or printing.

The hand-off run of the same fuzz is far larger (progress file, fix round 2).
"""

from __future__ import annotations

import contextlib
import random
import signal
from collections.abc import Iterator

import pytest

from meshforge.common import secret_redaction
from meshforge.common.secret_redaction import (
    RedactionStallError,
    SecretMatcher,
    StreamRedactor,
    encoded_forms,
    marker,
    redact_bytes,
)

pytestmark = pytest.mark.unit

FEED_CPU_SECONDS = 2.0


class _SpinError(Exception):
    """A feed used more CPU time than any correct feed of a tiny input can."""


@contextlib.contextmanager
def _cpu_limit(seconds: float = FEED_CPU_SECONDS) -> Iterator[None]:
    # CPU time of this process (not wall time): a busy machine cannot fire it,
    # a spinning pure-Python loop always does.
    def fire(*_args: object) -> None:
        raise _SpinError

    old = signal.signal(signal.SIGVTALRM, fire)
    signal.setitimer(signal.ITIMER_VIRTUAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_VIRTUAL, 0)
        signal.signal(signal.SIGVTALRM, old)


def _owners(secrets: dict[str, bytes]) -> dict[bytes, str]:
    owner: dict[bytes, str] = {}
    for name in sorted(secrets):
        for form in encoded_forms(secrets[name]):
            owner.setdefault(form, name)
    return owner


def _oracle(data: bytes, secrets: dict[str, bytes], preserve: bool) -> bytes:
    """Brute force: try every form at every position; redact the union."""
    best: dict[int, tuple[int, str]] = {}
    for start in range(len(data)):
        for form, name in _owners(secrets).items():
            if data.startswith(form, start):
                end = start + len(form)
                if start not in best or end > best[start][0]:
                    best[start] = (end, name)
    merged: list[tuple[int, int, str]] = []
    for start in sorted(best):
        end, name = best[start]
        if merged and start < merged[-1][1]:
            first, last_end, first_name = merged[-1]
            merged[-1] = (first, max(last_end, end), first_name)
        else:
            merged.append((start, end, name))
    out: list[bytes] = []
    pos = 0
    for start, end, name in merged:
        out.append(data[pos:start])
        mark = marker(name)
        if not preserve:
            out.append(mark)
        elif end - start <= len(mark):
            out.append(b"*" * (end - start))
        else:
            out.append(mark + b"*" * (end - start - len(mark)))
        pos = end
    out.append(data[pos:])
    return b"".join(out)


def _held_bound(seen: bytes, forms: list[bytes]) -> int:
    """Longest suffix of the stream that is a proper prefix of a form."""
    bound = 0
    for k in range(1, len(seen) + 1):
        tail = seen[-k:]
        if any(len(form) > k and form.startswith(tail) for form in forms):
            bound = k
    return bound


def _stream(
    data: bytes,
    matcher: SecretMatcher,
    preserve: bool,
    cuts: list[int],
    forms: list[bytes] | None = None,
) -> bytes:
    red = StreamRedactor(matcher, length_preserving=preserve)
    out: list[bytes] = []
    prev = 0
    for cut in [*cuts, len(data)]:
        with _cpu_limit():
            out.append(red.feed(data[prev:cut]))
        prev = cut
        if forms is not None and not preserve:
            assert red.held <= _held_bound(data[:cut], forms)
    with _cpu_limit():
        out.append(red.flush())
    assert not red.failed
    assert not matcher.stalled
    return b"".join(out)


T17 = {"S0": b"00aaaa000a", "S1": b"a000a"}
LEAK = {"S0": b"aaa aaa  ", "S1": b"aa  ", "S2": b" aaa  a"}


@pytest.mark.parametrize("preserve", [False, True], ids=["agent", "controller"])
@pytest.mark.parametrize(
    "data",
    [b"3030616161613030306", b"30306161616130303061"],
    ids=["hex-minus-one-char", "hex-of-the-first-secret"],
)
@pytest.mark.parametrize("chunked", [False, True], ids=["whole", "bytewise"])
def test_t17_two_interlocking_secrets_terminate(
    data: bytes, preserve: bool, chunked: bool
) -> None:
    cuts = list(range(1, len(data))) if chunked else []
    got = _stream(data, SecretMatcher(T17), preserve, cuts)
    assert got == _oracle(data, T17, preserve)


@pytest.mark.parametrize("preserve", [False, True], ids=["agent", "controller"])
def test_a_form_hidden_behind_a_waiting_scan_is_still_redacted(preserve: bool) -> None:
    # Found by the fuzz below: hex(S1) = 61612020 was printed in clear - the
    # self-scan of hex(S2) read hex(S0)'s episode list while that scan was
    # waiting, half updated (unsorted), and concluded "no shorter candidate".
    data = b"6161612061616120206161612020"
    got = _stream(data, SecretMatcher(LEAK), preserve, [])
    assert got == _oracle(data, LEAK, preserve)
    assert b"61612020" not in got


ALPHABETS = [
    b"ab",
    b"a0",
    b"0a",
    b"abc",
    b"a01",
    b"A=",
    b"%2",
    b"a ",
    b"\x00\xff",
    b"+/a",
    b"-_a",
    b"aB0",
    b"a",
    b"%2B",
]


def _secrets(rnd: random.Random, alpha: bytes) -> dict[str, bytes]:
    secrets: dict[str, bytes] = {}
    for i in range(rnd.randint(1, 4)):
        value = bytes(rnd.choice(alpha) for _ in range(rnd.randint(4, 12)))
        if secrets and rnd.random() < 0.3:
            base = rnd.choice(list(secrets.values()))
            a = rnd.randint(0, len(base) - 4)
            value = base[a : a + rnd.randint(4, len(base))]
        secrets[f"S{i}"] = value
    return secrets


def _output(rnd: random.Random, pool: list[bytes], alpha: bytes) -> bytes:
    pieces: list[bytes] = []
    for _ in range(rnd.randint(0, 10)):
        if rnd.random() < 0.6:
            form = rnd.choice(pool)
            if rnd.random() < 0.4:
                form = form[rnd.randint(0, len(form) - 1) :]
            if rnd.random() < 0.4:
                form = form[: rnd.randint(1, len(form))]
            pieces.append(form)
        else:
            source = rnd.choice([alpha, alpha, b"0123456", b"ab%=", rnd.choice(pool)])
            pieces.append(bytes(rnd.choice(source) for _ in range(rnd.randint(0, 6))))
    return b"".join(pieces)


def _cuts(rnd: random.Random, n: int) -> list[int]:
    style = rnd.randrange(4)
    if style == 0 or n < 2:
        return []
    if style == 1:
        return list(range(1, n))
    return sorted({rnd.randint(1, n - 1) for _ in range(rnd.randint(1, 6))})


@pytest.mark.parametrize("seed", [20261013, 20261014, 20261015])
def test_small_alphabet_fuzz_against_a_brute_force_oracle(seed: int) -> None:
    rnd = random.Random(seed)
    for case in range(1500):
        alpha = rnd.choice(ALPHABETS)
        secrets = _secrets(rnd, alpha)
        forms = sorted(_owners(secrets))  # sorted: independent of the hash seed
        matcher = SecretMatcher(secrets)  # shared: the lazy records accumulate
        for _stream_no in range(2):
            data = _output(rnd, forms, alpha)
            for preserve in (False, True):
                cuts = _cuts(rnd, len(data))
                got = _stream(data, matcher, preserve, cuts, forms)
                want = _oracle(data, secrets, preserve)
                assert got == want, (seed, case, secrets, data, cuts, preserve)


def test_a_chain_of_256_nested_values_builds_and_matches() -> None:
    # The trie is as deep as the chain; the build recursion must cope.
    secrets = {f"S{i:03d}": b"a" * (4 + i) for i in range(256)}
    matcher = SecretMatcher(secrets)
    data = b"x" + b"a" * 300 + b"y" + b"61" * 50 + b"z"
    for preserve in (False, True):
        with _cpu_limit(20):
            got = redact_bytes(data, matcher, length_preserving=preserve)
        assert got == _oracle(data, secrets, preserve)


def test_a_scan_that_claims_done_without_advancing_withholds_the_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The T-17 failure itself, forced: the guards answer with ``*``, at once."""
    monkeypatch.setattr(secret_redaction._SelfScan, "advance", lambda _s, _d: None)
    data = b"3030616161613030306"
    for preserve in (False, True):
        matcher = SecretMatcher(T17)
        red = StreamRedactor(matcher, length_preserving=preserve)
        with _cpu_limit():
            out = red.feed(data[:10]) + red.feed(data[10:]) + red.flush()
        assert red.failed
        assert matcher.stalled
        assert len(out) == len(data)
        assert set(out[len(out) - 9 :]) == {ord("*")}
        # every other stream of the same matcher is closed too, and stays so
        other = StreamRedactor(matcher, length_preserving=preserve)
        assert other.feed(b"00aaaa000a plain") == b"*" * 16
        assert other.failed
        assert other.feed(b"more") + other.flush() == b"****"


def test_a_self_scan_that_stops_short_raises_instead_of_answering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matcher = SecretMatcher(T17)
    k = matcher.forms.index(b"30306161616130303061")
    scan = matcher.scan(k)
    monkeypatch.setattr(secret_redaction._Walker, "run", lambda _w, _limit: None)
    with pytest.raises(RedactionStallError, match="stopped short"):
        scan.advance(19)


def test_self_scans_waiting_for_each_other_raise_instead_of_spinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matcher = SecretMatcher(T17)
    turns = 0

    def ping_pong(scan: object, depth: int) -> tuple[int, int] | None:
        nonlocal turns
        turns += 1
        return None if turns % 2 == 0 else (0, depth)

    monkeypatch.setattr(secret_redaction._SelfScan, "advance", ping_pong)
    with _cpu_limit(), pytest.raises(RedactionStallError, match="wait for each other"):
        matcher.ensure((1, 9))
    assert turns <= 9 + 4


def test_a_walker_that_does_not_move_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    matcher = SecretMatcher({"K": b"abcdefgh"})
    walker = secret_redaction._Walker(matcher, b"abcdXabcdefgh", 0, lambda *_a: None)
    # A candidate that "falls back" to itself: the measure stays put.
    monkeypatch.setattr(secret_redaction._Walker, "_die", lambda *_a: None)
    with _cpu_limit(), pytest.raises(RedactionStallError, match="did not advance"):
        walker.run(13)
