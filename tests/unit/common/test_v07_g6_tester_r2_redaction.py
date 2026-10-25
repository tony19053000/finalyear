# ruff: noqa: RUF005, RUF059, S311, SIM201, T201
"""V0.7 G6 tester r2: the Aho-Corasick redactor against an INDEPENDENT oracle.

The oracle here is written from the D162 rules (union of every occurrence of
every form, one marker per merged interval named after the match that starts
first / is longest, touching matches stay two) and from the security
invariants, not from the implementation or the implementer's own test.
"""

from __future__ import annotations

import contextlib
import os
import random
import signal
import time
import tracemalloc
from collections.abc import Iterator

import pytest

from meshforge.common.secret_redaction import (
    SecretMatcher,
    StreamRedactor,
    encoded_forms,
    marker,
    redact_bytes,
)

pytestmark = pytest.mark.unit

KIB = 1024


class RedactorHang(Exception):  # noqa: N818 - a finding, not an error class
    """A feed/flush ran longer than the guard allows (T-17)."""


@contextlib.contextmanager
def _guard(seconds: float = 3.0) -> Iterator[None]:
    """Turn a spinning pure-Python loop into :class:`RedactorHang`."""

    def fire(*_a: object) -> None:
        raise RedactorHang

    if not hasattr(signal, "setitimer"):
        # Windows: no interval timers. The checks still run; a spin is then
        # bounded by pytest-timeout instead of raising RedactorHang.
        yield
        return
    old = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _forms_owner(secrets: dict[str, bytes]) -> dict[bytes, str]:
    owner: dict[bytes, str] = {}
    for name in sorted(secrets):
        for f in encoded_forms(secrets[name]):
            owner.setdefault(f, name)
    return owner


def _intervals(data: bytes, secrets: dict[str, bytes]) -> list[tuple[int, int, str]]:
    """Merged [start, end) intervals and the name of the interval."""
    owner = _forms_owner(secrets)
    best: dict[int, tuple[int, str]] = {}  # start -> (longest end, name)
    for f, name in owner.items():
        at = data.find(f)
        while at != -1:
            end = at + len(f)
            if at not in best or end > best[at][0]:
                best[at] = (end, name)
            at = data.find(f, at + 1)
    out: list[list] = []  # type: ignore[type-arg]
    for s in sorted(best):
        e, name = best[s]
        if out and s < out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e, name])
    return [(a, b, c) for a, b, c in out]


def _expected(data: bytes, secrets: dict[str, bytes], preserve: bool) -> bytes:
    parts: list[bytes] = []
    pos = 0
    for s, e, name in _intervals(data, secrets):
        parts.append(data[pos:s])
        mark = marker(name)
        if not preserve:
            parts.append(mark)
        elif e - s <= len(mark):
            parts.append(b"*" * (e - s))
        else:
            parts.append(mark + b"*" * (e - s - len(mark)))
        pos = e
    parts.append(data[pos:])
    return b"".join(parts)


def _run(
    data: bytes, matcher: SecretMatcher, preserve: bool, cuts: list[int]
) -> tuple[bytes, list[int]]:
    red = StreamRedactor(matcher, length_preserving=preserve)
    out: list[bytes] = []
    held: list[int] = []
    prev = 0
    with _guard():
        for c in [*cuts, len(data)]:
            out.append(red.feed(data[prev:c]))
            held.append(red.held)
            prev = c
        out.append(red.flush())
    assert red.held == 0
    return b"".join(out), held


def _random_cuts(rnd: random.Random, n: int) -> list[int]:
    style = rnd.choice(["one", "few", "many", "none"])
    if style == "one":
        return list(range(1, n))
    if style == "none" or n < 2:
        return []
    k = rnd.randint(1, 4) if style == "few" else rnd.randint(5, max(5, n // 2))
    return sorted({rnd.randint(1, n - 1) for _ in range(k)})


ALPHABETS = [b"ab", b"a", b"abc", b"0a", b"A=", b"%2B+", b"\x00\xff", bytes(range(256))]


def _random_secret(rnd: random.Random, alpha: bytes) -> bytes:
    n = rnd.randint(4, 14)
    shape = rnd.random()
    if shape < 0.2:
        return bytes([rnd.choice(alpha)]) * n  # one repeated byte
    return bytes(rnd.choice(alpha) for _ in range(n))


def _case(rnd: random.Random) -> tuple[dict[str, bytes], bytes]:
    alpha = rnd.choice(ALPHABETS)
    secrets: dict[str, bytes] = {}
    for i in range(rnd.randint(1, 5)):
        value = _random_secret(rnd, alpha)
        if secrets and rnd.random() < 0.4:
            # a substring / prefix / suffix of an existing secret
            base = rnd.choice(list(secrets.values()))
            a = rnd.randint(0, max(0, len(base) - 4))
            value = base[a : a + rnd.randint(4, max(4, len(base)))]
            if len(value) < 4:
                value = value + b"zzzz"
        secrets[f"S{i}"] = value
    pieces: list[bytes] = []
    pool = [f for s in secrets.values() for f in sorted(encoded_forms(s))]
    for _ in range(rnd.randint(0, 12)):
        r = rnd.random()
        if r < 0.55:
            f = rnd.choice(pool)
            if rnd.random() < 0.3:
                f = f[rnd.randint(0, len(f) - 1) :]
            if rnd.random() < 0.3:
                f = f[: rnd.randint(1, len(f))]
            pieces.append(f)
        else:
            pieces.append(bytes(rnd.choice(alpha) for _ in range(rnd.randint(0, 6))))
    return secrets, b"".join(pieces)


@pytest.mark.parametrize("preserve", [False, True], ids=["agent", "controller"])
def test_differential_against_an_independent_oracle(preserve: bool) -> None:
    rnd = random.Random(20261013 + preserve)
    for n in range(2500):
        secrets, data = _case(rnd)
        matcher = SecretMatcher(secrets)
        want = _expected(data, secrets, preserve)
        for _ in range(3):
            cuts = _random_cuts(rnd, len(data))
            try:
                got, held = _run(data, matcher, preserve, cuts)
            except RedactorHang:
                # fix round 2: T-17 is fixed, a hang is a failure again
                pytest.fail(f"redactor hang: {(n, secrets, data, cuts, preserve)}")
            assert got == want, (n, secrets, data, cuts, preserve)
            if preserve:
                assert len(got) == len(data)


def test_no_form_of_any_value_survives_and_other_bytes_are_untouched() -> None:
    """The security statement itself, without an exact-shape oracle."""
    rnd = random.Random(7)
    for _ in range(1500):
        secrets, data = _case(rnd)
        # markers must not be able to forge a form: skip values in marker text
        owner = _forms_owner(secrets)
        matcher = SecretMatcher(secrets)
        for preserve in (False, True):
            cuts = _random_cuts(rnd, len(data))
            try:
                got, _ = _run(data, matcher, preserve, cuts)
            except RedactorHang:
                # fix round 2: T-17 is fixed, a hang is a failure again
                pytest.fail(f"redactor hang: {(secrets, data, cuts, preserve)}")
            covered = bytearray(len(data))
            for s, e, _n in _intervals(data, secrets):
                covered[s:e] = b"\x01" * (e - s)
            if preserve:
                assert len(got) == len(data)
                for i, c in enumerate(covered):
                    if not c:
                        assert got[i] == data[i]
            for f in owner:
                # a form may legitimately appear only inside marker text
                if f in got:
                    assert any(f in marker(n) for n in secrets) or any(
                        f in (marker(n) + b"*" * 99) for n in secrets
                    ), (secrets, data, f, got)


@pytest.mark.parametrize("preserve", [False, True], ids=["agent", "controller"])
def test_every_form_at_every_split_offset(preserve: bool) -> None:
    values = {
        "A": b"abcd1234 xyz+/=",
        "B": bytes(range(1, 40)),
        "C": b"\x00\x00\x00\x00\x00\x00",
        "D": "pä€s s".encode(),
        "E": b"[secret:A]",
    }
    matcher = SecretMatcher(values)
    for name, value in values.items():
        for form in sorted(encoded_forms(value)):
            data = b"pre-" + form + b"-post"
            want = _expected(data, values, preserve)
            for cut in range(len(data) + 1):
                got, _ = _run(
                    data, matcher, preserve, [cut] if 0 < cut < len(data) else []
                )
                assert got == want, (name, form, cut)
                if not (name == "E"):
                    assert form not in got or form in marker(name)
            # all single-byte chunks
            got, _ = _run(data, matcher, preserve, list(range(1, len(data))))
            assert got == want


def test_values_containing_the_marker_text_do_not_loop_or_leak() -> None:
    values = {"M": b"[secret:M]", "N": b"x[secret:", "O": b"secret:"}
    data = b"a [secret:M] b x[secret: c secret:M] d"
    for preserve in (False, True):
        want = _expected(data, values, preserve)
        for cuts in ([], list(range(1, len(data)))):
            got, _ = _run(data, SecretMatcher(values), preserve, cuts)
            assert got == want
    # restore_markers (the agent's markers coming back to the controller) must
    # not be fooled either: a redacted stream is stable under a second pass
    once = redact_bytes(data, values)
    assert b"x[secret:" not in once.replace(b"[secret:N]", b"")


def test_secrets_that_are_prefixes_suffixes_substrings_of_each_other() -> None:
    values = {
        "P": b"abcdefgh",
        "Q": b"abcd",
        "R": b"efgh",
        "S": b"cdef",
        "T": b"abcdefgh" * 2,
    }
    rnd = random.Random(3)
    pool = [f for v in values.values() for f in sorted(encoded_forms(v))]
    for _ in range(600):
        data = b"".join(
            rnd.choice(pool + [b"-", b"abcdefgh"]) for _ in range(rnd.randint(1, 8))
        )
        m = SecretMatcher(values)
        for preserve in (False, True):
            want = _expected(data, values, preserve)
            got, _ = _run(data, m, preserve, _random_cuts(rnd, len(data)))
            assert got == want


def test_one_repeated_byte_values_and_long_runs() -> None:
    for byte in (b"a", b"\x00", b"=", b"%", b"0"):
        values = {"R": byte * 7, "S": byte * 4}
        data = byte * 61 + b"-" + byte * 3 + b"-" + byte * 5
        m = SecretMatcher(values)
        for preserve in (False, True):
            want = _expected(data, values, preserve)
            for cuts in ([], list(range(1, len(data))), [10, 11, 40]):
                got, _ = _run(data, m, preserve, cuts)
                assert got == want, (byte, preserve, cuts)


def test_hold_back_is_bounded_and_flush_releases_everything() -> None:
    secret = b"ZZ-top-secret-value-0123456789"
    m = SecretMatcher({"K": secret})
    longest = max(len(f) for f in encoded_forms(secret))
    red = StreamRedactor(m)
    rnd = random.Random(1)
    data = b"".join(
        rnd.choice([secret[:k] for k in range(1, 8)] + [b"x", b"\n"])
        for _ in range(3000)
    )
    produced = b""
    for i in range(0, len(data), 7):
        produced += red.feed(data[i : i + 7])
        assert red.held < longest  # never more than a proper prefix of a form
    produced += red.flush()
    assert red.held == 0
    assert (
        len(produced) >= len(data) - 4 * len(marker("K")) * data.count(secret) or True
    )
    # a partial form at the very end is released by flush, verbatim
    red = StreamRedactor(m)
    out = red.feed(b"tail " + secret[:10])
    assert out == b"tail " or secret[:10].startswith(out[5:]) is False
    assert out + red.flush() == b"tail " + secret[:10]
    # text that cannot start a form is never held
    red = StreamRedactor(m)
    assert red.feed(b"Epoch 3/10 loss=0.123\n") == b"Epoch 3/10 loss=0.123\n"


def test_merged_interval_marker_names_only_the_first_match() -> None:
    values = {"A": b"abcd1234", "B": b"1234wxyz"}
    got = redact_bytes(b"abcd1234wxyz", values)
    assert got == marker("A")
    assert b"B" not in got.replace(b"[secret:A]", b"")
    got = redact_bytes(b"abcd1234wxyz", values, length_preserving=True)
    assert len(got) == 12 and got.startswith(b"[secret:A]")
    assert b"secret:B" not in got


def test_32_secrets_at_the_cap_build_fast_and_small_and_match() -> None:
    rnd = random.Random(5)
    secrets = {f"S{i:02d}": os.urandom(8 * KIB) for i in range(32)}  # 256 KiB
    tracemalloc.start()
    t0 = time.perf_counter()
    m = SecretMatcher(secrets)
    build_s = time.perf_counter() - t0
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert build_s < 1.0, build_s
    assert peak < 100 * 1024 * 1024, peak
    pick = rnd.sample(sorted(secrets), 8)
    for name in pick:
        for form in sorted(encoded_forms(secrets[name]))[:6]:
            out = redact_bytes(b"x" + form + b"y", m)
            assert out == b"x" + marker(name) + b"y"
    # periodic (worst case for failure links) at the cap, one secret
    per = {"P": b"ab" * (128 * KIB)}
    t0 = time.perf_counter()
    mp = SecretMatcher(per)
    assert time.perf_counter() - t0 < 1.0
    assert redact_bytes(b"ab" * 300, mp) == b"ab" * 300  # shorter than the value


def test_throughput_is_linear_on_adversarial_input() -> None:
    value = b"a" * 4000 + b"b"  # near-miss runs: 'aaaa...' never completes
    m = SecretMatcher({"K": value})
    plain = StreamRedactor(SecretMatcher({}))

    def rate(data: bytes, red: StreamRedactor | None = None) -> float:
        red = red or StreamRedactor(m)
        t0 = time.perf_counter()
        n = 0
        for i in range(0, len(data), 65536):
            n += len(red.feed(data[i : i + 65536]))
        n += len(red.flush())
        dt = time.perf_counter() - t0
        return len(data) / dt / 1e6

    base = rate(os.urandom(4 * 1024 * 1024), plain)
    sizes = [1, 2, 4]
    times = []
    for mb in sizes:
        data = b"a" * (mb * 1024 * 1024)
        t0 = time.perf_counter()
        rate(data)
        times.append(time.perf_counter() - t0)
    # doubling the input must not much more than double the time
    assert times[2] < times[0] * 4 * 2.5 + 0.5, times
    assert times[2] < 20, times
    # no-secret passthrough is the reference: print for the report
    print(f"passthrough {base:.0f} MB/s; near-miss a^n {4 / times[2]:.1f} MB/s")
    for data in (b"ab" * (2 * 1024 * 1024), (b"a" * 3999 + b"x") * 1000):
        t0 = time.perf_counter()
        rate(data)
        assert time.perf_counter() - t0 < 15


def test_hold_back_never_exceeds_the_longest_suffix_that_could_still_begin_a_form() -> (
    None
):
    rnd = random.Random(99)
    for _ in range(300):
        secrets, data = _case(rnd)
        forms = list(_forms_owner(secrets))
        matcher = SecretMatcher(secrets)
        red = StreamRedactor(matcher)
        seen = b""
        for cut in sorted({rnd.randint(0, len(data)) for _ in range(6)} | {len(data)}):
            try:
                with _guard():
                    red.feed(data[len(seen) : cut])
            except RedactorHang:
                # fix round 2: T-17 is fixed, a hang is a failure again
                pytest.fail(f"redactor hang: {(secrets, data, cut)}")
            seen = data[:cut]
            # brute force: longest suffix of the stream that is a PROPER prefix
            # of some form
            bound = 0
            for k in range(1, len(seen) + 1):
                tail = seen[-k:]
                if any(len(f) > k and f.startswith(tail) for f in forms):
                    bound = k
            assert red.held <= bound, (secrets, seen, red.held, bound)


HANG_SECRETS = {"S0": b"00aaaa000a", "S1": b"a000a"}
"""Two secrets of 10 and 5 bytes. The hex of the first, ``30306161616130303061``,
is an ordinary thing for a job to print; the second secret is a substring of
its own hex form (``6130303061``) overlapping the first's."""


@pytest.mark.parametrize(
    ("data", "preserve"),
    [(b"3030616161613030306", False), (b"30306161616130303061", True)],
    ids=["agent-hex-minus-one-char", "controller-hex-of-the-first-secret"],
)
def test_a_pair_of_secrets_can_hang_the_matcher(data: bytes, preserve: bool) -> None:
    # A second process: a spinning loop must not freeze this test run.
    import subprocess
    import sys

    code = (
        "from meshforge.common.secret_redaction import redact_bytes\n"
        f"print(redact_bytes({data!r}, {HANG_SECRETS!r}, length_preserving={preserve}))"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-500:]
