"""V0.7 G6 fix round 1 (D162: T-8, T-3, T-2): the linear multi-pattern matcher.

* the redacted output equals a brute-force oracle (every occurrence of every
  form, **union** of the intervals, one replacement per merged interval named
  after the match that starts first / is longest) for adversarial small-alphabet
  secrets, at any chunking, in both replacement styles, on a shared matcher;
* T-8: the largest job the cap allows compiles in well under a second and a
  few megabytes; a ``StreamRedactor`` on a shared matcher compiles nothing;
  periodic values (the worst case for the failure function) stay linear;
* T-3: what is held back is exactly the longest suffix of the stream that is a
  proper prefix of a form - nothing else;
* T-2: overlapping matches leave no byte of either value, in both styles;
  touching matches stay two replacements; an endless chain of overlaps does
  not grow the buffer.
"""

from __future__ import annotations

import os
import random
import re
import time
import tracemalloc
from urllib.parse import quote, quote_plus

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
ALPHABETS = [b"ab", b"a", b"abc", b"a b", b"ab%4", b"01", b"aAbB\xff", b"YWFh=", b"61"]


def _oracle(data: bytes, secrets: dict[str, bytes], preserve: bool) -> bytes:
    owner: dict[bytes, str] = {}
    for name in sorted(secrets):
        for form in encoded_forms(secrets[name]):
            owner.setdefault(form, name)
    matches: list[tuple[int, int, str]] = []
    for form, name in owner.items():
        at = data.find(form)
        while at != -1:
            matches.append((at, -len(form), name))
            at = data.find(form, at + 1)
    merged: list[list] = []  # type: ignore[type-arg]
    for start, neg_len, name in sorted(matches):
        end = start - neg_len
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end, name])
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


def _case(seed: int) -> tuple[dict[str, bytes], bytes, random.Random]:
    rng = random.Random(seed)  # noqa: S311
    alphabet = rng.choice(ALPHABETS)
    secrets = {
        f"S{i}": bytes(
            rng.choice(alphabet) for _ in range(rng.choice([4, 4, 5, 6, 8, 12, 20, 40]))
        )
        for i in range(rng.randrange(1, 6))
    }
    forms = [form for value in secrets.values() for form in encoded_forms(value)]
    pieces: list[bytes] = []
    for _ in range(rng.randrange(1, 30)):
        roll = rng.random()
        form = rng.choice(forms)
        if roll < 0.35:
            pieces.append(form)
        elif roll < 0.6:
            pieces.append(form[: rng.randrange(len(form) + 1)])
        elif roll < 0.75:
            pieces.append(form[rng.randrange(len(form)) :])
        else:
            noise = alphabet + b"0123456789abcdefABCDEF%=\n"
            pieces.append(bytes(rng.choice(noise) for _ in range(rng.randrange(1, 12))))
    return secrets, b"".join(pieces), rng


def _cuts(rng: random.Random, n: int, style: int) -> list[int]:
    if style == 0 or n < 3:
        return []
    if style == 1:
        return list(range(1, n))
    return sorted(rng.sample(range(1, n), min(n - 1, rng.randrange(0, 8))))


@pytest.mark.parametrize("block", range(8))
def test_output_equals_the_brute_force_oracle_at_any_chunking(block: int) -> None:
    for seed in range(block * 150, block * 150 + 150):
        secrets, data, rng = _case(seed)
        shared = SecretMatcher(secrets)
        for preserve in (False, True):
            expect = _oracle(data, secrets, preserve)
            for style in range(3):
                red = StreamRedactor(shared, length_preserving=preserve)
                out: list[bytes] = []
                last = 0
                for cut in [*_cuts(rng, len(data), style), len(data)]:
                    out.append(red.feed(data[last:cut]))
                    last = cut
                out.append(red.flush())
                assert b"".join(out) == expect, (seed, preserve, style, secrets, data)
                assert red.held == 0


def test_the_percent_tables_equal_urllib() -> None:
    rng = random.Random(3)  # noqa: S311

    def lower(text: str) -> bytes:
        return re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), text).encode()

    for _ in range(300):
        value = bytes(rng.randrange(256) for _ in range(rng.randrange(4, 80)))
        plain, plus = quote(value, safe=""), quote_plus(value, safe="")
        for form in (plain.encode(), plus.encode(), lower(plain), lower(plus)):
            assert form in encoded_forms(value)


# ----------------------------------------------------------------- T-8


@pytest.mark.parametrize(
    "shape", ["4x64KiB", "32x8KiB", "1x64KiB+48x4KiB", "periodic", "suffixes"]
)
def test_the_largest_allowed_job_compiles_fast_and_small(shape: str) -> None:
    """256 KiB of values (the default per-job cap), whatever their shape."""
    seed = os.urandom(8 * KIB)
    secrets = {
        "4x64KiB": {f"S{i}": os.urandom(64 * KIB) for i in range(4)},
        "32x8KiB": {f"S{i}": os.urandom(8 * KIB) for i in range(32)},
        "1x64KiB+48x4KiB": {
            "BIG": os.urandom(64 * KIB),
            **{f"S{i}": os.urandom(4 * KIB) for i in range(48)},
        },
        "periodic": {
            "A": b"a" * 64 * KIB,
            "B": b"\xff" * 64 * KIB,
            "C": b"ab" * 32 * KIB,
            "D": b" " * 64 * KIB,
        },
        "suffixes": {f"S{i}": seed[i:] for i in range(32)},
    }[shape]
    assert sum(map(len, secrets.values())) <= 256 * KIB
    SecretMatcher({"WARM": b"warm-up-value"})
    started = time.perf_counter()
    matcher = SecretMatcher(secrets)
    took = time.perf_counter() - started
    assert took < 1.0, f"{shape}: {took:.2f} s"
    tracemalloc.start()
    try:
        again = SecretMatcher(secrets)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 100 * 1024 * 1024, f"{shape}: {peak / 1e6:.0f} MB"
    assert again.forms == matcher.forms


def test_a_redactor_on_a_shared_matcher_compiles_nothing() -> None:
    matcher = SecretMatcher({f"S{i}": os.urandom(64 * KIB) for i in range(4)})
    started = time.perf_counter()
    for _ in range(200):
        StreamRedactor(matcher)
        StreamRedactor(matcher, length_preserving=True)
    assert time.perf_counter() - started < 0.5


def test_streams_on_one_matcher_do_not_share_state() -> None:
    value = b"one-matcher-two-streams"
    matcher = SecretMatcher({"K": value})
    out_stream, err_stream = StreamRedactor(matcher), StreamRedactor(matcher)
    got_out = out_stream.feed(b"out " + value[:10])
    got_err = err_stream.feed(b"err " + value[:5])
    got_out += out_stream.feed(value[10:] + b" end")
    got_err += err_stream.feed(b"-not-it " + value)
    assert got_out + out_stream.flush() == b"out [secret:K] end"
    assert got_err + err_stream.flush() == (b"err " + value[:5] + b"-not-it [secret:K]")


@pytest.mark.parametrize(
    "value",
    [b"a" * 64 * KIB, b"\xff" * 64 * KIB, b"ab" * 32 * KIB],
    ids=["a", "ff", "ab"],
)
def test_periodic_values_stay_linear_in_the_output(value: bytes) -> None:
    """The failure function's worst case: every position starts a match."""
    matcher = SecretMatcher({"K": value})
    near_miss = b"".join(form[:-1] + b"\x01" for form in matcher.forms)

    def run(times: int) -> float:
        red = StreamRedactor(matcher)
        data = near_miss * times
        started = time.perf_counter()
        for at in range(0, len(data), 65536):
            red.feed(data[at : at + 65536])
        red.flush()
        return time.perf_counter() - started

    run(1)  # the lazily built failure records
    one, four = run(1), run(4)
    assert four < one * 4 * 3 + 0.5, (one, four)
    assert four < 20


def test_printing_every_form_of_the_largest_job_never_stalls_one_feed() -> None:
    secrets = {f"S{i}": os.urandom(64 * KIB) for i in range(4)}
    matcher = SecretMatcher(secrets)
    data = b"\n".join(matcher.forms)
    red = StreamRedactor(matcher)
    worst = 0.0
    out = bytearray()
    for at in range(0, len(data), 65536):
        started = time.perf_counter()
        out += red.feed(data[at : at + 65536])
        worst = max(worst, time.perf_counter() - started)
    out += red.flush()
    assert worst < 1.0
    for value in secrets.values():
        assert value[:16] not in out and value.hex().encode()[:32] not in out
    assert len(out) < 4096


# ----------------------------------------------------------------- T-3


def _proper_prefix_tail(stream: bytes, forms: set[bytes]) -> int:
    longest = max(map(len, forms))
    for length in range(min(len(stream), longest - 1), 0, -1):
        tail = stream[-length:]
        if any(len(form) > length and form.startswith(tail) for form in forms):
            return length
    return 0


def test_only_a_tail_that_could_begin_a_form_is_held_back() -> None:
    rng = random.Random(5)  # noqa: S311
    value = b"abcabcabd-hold-back"
    secrets = {"K": value, "L": b"bcab-other"}
    forms = set().union(*(encoded_forms(v) for v in secrets.values()))
    samples = [form[: rng.randrange(1, len(form))] for form in forms for _ in range(4)]
    for _ in range(400):
        red = StreamRedactor(secrets)
        stream = b""
        emitted = b""
        for _ in range(rng.randrange(1, 8)):
            piece = rng.choice(
                [*samples, b"Epoch 1 done\n", b"a", b"ab", b"\n", b"YWJj"]
            )
            stream += piece
            emitted += red.feed(piece)
            if any(form in stream for form in forms):
                break  # replacements change lengths; the oracle test covers them
            assert red.held == _proper_prefix_tail(stream, forms), (stream, red.held)
            assert emitted == stream[: len(stream) - red.held]


def test_a_status_line_is_out_at_once_and_a_completed_value_too() -> None:
    value = b"a-typical-api-key-0123456789abcdef"
    red = StreamRedactor({"K": value})
    assert red.feed(b"step 1 done\n") == b"step 1 done\n"
    assert red.held == 0
    # The value as the last bytes, no newline: its marker is out at once.
    assert red.feed(b"key=" + value) == b"key=[secret:K]"
    assert red.held == 0
    # A proper prefix is held (it may become the value) and released on flush.
    assert red.feed(b"x " + value[:9]) == b"x "
    assert red.held == 9
    assert red.flush() == value[:9]


# ----------------------------------------------------------------- T-2


@pytest.mark.parametrize("preserve", [False, True])
def test_overlapping_matches_are_redacted_as_their_union(preserve: bool) -> None:
    secrets = {"A": b"abcd1234", "B": b"1234wxyz"}
    data = b"< abcd1234wxyz >"
    for cut in range(len(data) + 1):
        red = StreamRedactor(secrets, length_preserving=preserve)
        got = red.feed(data[:cut]) + red.feed(data[cut:]) + red.flush()
        # One replacement, named after the match that starts first.
        if preserve:
            assert got == b"< [secret:A]** >", (cut, got)
            assert len(got) == len(data)
        else:
            assert got == b"< [secret:A] >", (cut, got)
        assert red.replaced == 1


def test_the_longest_match_at_the_first_start_names_the_interval() -> None:
    secrets = {
        "LONG": b"token-abcdef-long",
        "SHORT": b"token-abc",
        "TAIL": b"long-tail",
    }
    assert redact_bytes(b"token-abcdef-long-tail.", secrets) == b"[secret:LONG]."
    assert redact_bytes(b"token-abc.", secrets) == b"[secret:SHORT]."


def test_touching_matches_stay_two_replacements() -> None:
    secrets = {"A": b"first-value", "B": b"second-value"}
    assert redact_bytes(b"first-valuesecond-value", secrets) == (
        b"[secret:A][secret:B]"
    )
    kept = redact_bytes(b"first-valuesecond-value", secrets, length_preserving=True)
    assert kept == b"[secret:A]*" + b"[secret:B]**"


@pytest.mark.parametrize("preserve", [False, True])
def test_an_endless_chain_of_overlaps_does_not_grow_the_buffer(preserve: bool) -> None:
    red = StreamRedactor({"K": b"aaaa"}, length_preserving=preserve)
    out = bytearray()
    for _ in range(2000):
        out += red.feed(b"a" * 50)
        assert red.held <= 64
    out += red.flush()
    assert b"aaaa" not in out
    if preserve:
        assert bytes(out) == b"[secret:K]" + b"*" * (100_000 - 10)
    else:
        assert bytes(out) == b"[secret:K]"
    assert red.replaced == 1


def test_a_short_interval_in_length_preserving_mode_is_only_stars() -> None:
    # Not longer than its marker: stars only, so no cut marker is ever shown.
    got = redact_bytes(b"x abcd y", {"LONGNAME": b"abcd"}, length_preserving=True)
    assert got == b"x **** y"
