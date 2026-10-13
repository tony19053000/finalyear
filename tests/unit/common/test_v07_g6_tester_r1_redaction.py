"""V0.7 G6 tester r1: attack the redactor from the brief's criteria (5), not from
the implementation. Pure logic -> unit.

Criteria: raw, base64 std/urlsafe padded/unpadded, hex lower/upper, URL-encoded;
values split across chunk boundaries at every offset; two secrets where one is a
substring/prefix of another; overlapping matches; a secret printed as the very
last bytes with no newline; very long output must not be quadratic; the
length-preserving controller form must not leak a suffix/prefix of a value;
multi-byte/binary values; one byte per write.
"""

from __future__ import annotations

import base64
import itertools
import os
import random
import re
import time
import urllib.parse

import pytest

from meshforge.common.secret_redaction import (
    ACTIVE,
    StreamRedactor,
    encoded_forms,
    redact_bytes,
)

pytestmark = pytest.mark.unit


def _feed_in(
    secrets: dict[str, bytes],
    data: bytes,
    cuts: list[int],
    *,
    preserve: bool = False,
) -> bytes:
    red = StreamRedactor(secrets, length_preserving=preserve)
    out: list[bytes] = []
    last = 0
    for cut in [*cuts, len(data)]:
        out.append(red.feed(data[last:cut]))
        last = cut
    out.append(red.flush())
    return b"".join(out)


def _all_forms(value: bytes) -> list[bytes]:
    return [
        value,
        base64.b64encode(value),
        base64.b64encode(value).rstrip(b"="),
        base64.urlsafe_b64encode(value),
        base64.urlsafe_b64encode(value).rstrip(b"="),
        value.hex().encode(),
        value.hex().upper().encode(),
        urllib.parse.quote(value, safe="").encode(),
        re.sub(
            r"%[0-9A-F]{2}",
            lambda m: m.group(0).lower(),
            urllib.parse.quote(value, safe=""),
        ).encode(),
        urllib.parse.quote_plus(value, safe="").encode(),
    ]


VALUES = [
    b"abcd",  # the 4-byte minimum
    b"p@ss w0rd/+=&%",  # characters every encoder treats differently
    "pässwörd-日本語-🔑".encode(),  # multi-byte UTF-8
    bytes(range(64)),  # binary incl. NUL and control bytes
    bytes(range(200, 256)) + b"\x00\xff",  # high bytes
    b"\x00\x00\x00\x00\x00",  # all NUL
]


@pytest.mark.parametrize("value", VALUES, ids=lambda v: v[:6].hex())
def test_every_form_at_every_split_offset(value: bytes) -> None:
    for form in _all_forms(value):
        # The URL-encoded forms of the 4-byte value equal the raw value; skip dup.
        data = b"head " + form + b" tail"
        for cut in range(len(data) + 1):
            got = _feed_in({"K": value}, data, [cut])
            assert form not in got, (form, cut, got)
            assert got == b"head [secret:K] tail", (form, cut, got)


@pytest.mark.parametrize("value", VALUES[:3], ids=lambda v: v[:6].hex())
def test_every_pair_of_cuts_and_one_byte_per_write(value: bytes) -> None:
    form = base64.b64encode(value)
    data = b"x" + form + b"y" + value + b"z"
    expect = b"x[secret:K]y[secret:K]z"
    assert _feed_in({"K": value}, data, list(range(1, len(data)))) == expect
    for a, b in itertools.combinations(range(1, len(data)), 2):
        assert _feed_in({"K": value}, data, [a, b]) == expect, (a, b)


def test_secret_as_the_very_last_bytes_without_newline() -> None:
    for form in _all_forms(b"final-secret-value"):
        assert redact_bytes(b"done: " + form, {"K": b"final-secret-value"}) == (
            b"done: [secret:K]"
        )
    # And a proper prefix of a form at the very end is not a secret: kept.
    assert redact_bytes(b"ends in final-secre", {"K": b"final-secret-value"}) == (
        b"ends in final-secre"
    )


def test_nothing_is_lost_or_reordered_without_secrets() -> None:
    rng = random.Random(7)  # noqa: S311
    data = bytes(rng.randrange(256) for _ in range(5000))
    for _ in range(50):
        cuts = sorted(rng.sample(range(1, len(data)), rng.randrange(1, 40)))
        assert _feed_in({"K": b"not-in-the-data-xyz"}, data, cuts) == data


def test_substring_and_prefix_secrets_both_vanish() -> None:
    secrets = {"SHORT": b"secretkey", "LONG": b"secretkey-and-more"}
    for secs in (secrets, dict(reversed(list(secrets.items())))):
        for cut in range(40):
            data = b"a secretkey-and-more b secretkey c"
            got = _feed_in(secs, data, [cut] if cut else [])
            assert b"secretkey" not in got, got
            assert b"and-more" not in got, got
            assert got.count(b"[secret:") == 2


def test_a_secret_that_is_a_substring_of_another_secrets_encoding() -> None:
    # SHORT's raw bytes appear inside LONG's hex form: nothing may remain.
    long_value = b"\xde\xad\xbe\xef-long-value"
    short_value = long_value.hex()[:8].encode()  # 'deadbeef'
    secs = {"SHORT": short_value, "LONG": long_value}
    data = b"hex: " + long_value.hex().encode() + b"."
    got = redact_bytes(data, secs)
    assert long_value.hex().encode()[8:] not in got
    assert short_value not in got


def test_overlapping_matches_of_two_secrets_leave_no_tail() -> None:
    got = redact_bytes(b"abcd1234wxyz", {"A": b"abcd1234", "B": b"1234wxyz"})
    assert b"wxyz" not in got and b"abcd" not in got


@pytest.mark.parametrize("preserve", [False, True])
def test_a_repeated_and_adjacent_secret(preserve: bool) -> None:
    v = b"adjacent-secret"
    got = redact_bytes(v * 5, {"K": v}, length_preserving=preserve)
    assert v not in got
    if not preserve:
        assert got == b"[secret:K]" * 5


def test_length_preserving_keeps_every_byte_offset_and_leaks_nothing() -> None:
    rng = random.Random(11)  # noqa: S311
    value = b"offsets-matter-0123"
    forms = _all_forms(value)
    for _ in range(300):
        pieces = [rng.choice([*forms, b" ", b"\n", b"noise"]) for _ in range(12)]
        data = b"".join(pieces)
        cuts = sorted(rng.sample(range(1, len(data)), rng.randrange(1, 25)))
        got = _feed_in({"NAME": value}, data, cuts, preserve=True)
        assert len(got) == len(data), "byte offsets must survive redaction"
        for form in forms:
            assert form not in got
        # No 4+ byte window of any form may survive anywhere: a cut marker or
        # padding must not expose a prefix/suffix of the value.
        shown = b"[secret:NAME]" + b"*" * 8
        for form in forms:
            for i in range(len(form) - 3):
                window = form[i : i + 4]
                assert window not in got or window in shown or window in b"noise \n", (
                    window,
                    got,
                )


def test_scrub_active_hides_every_form_in_our_own_logs() -> None:
    value = b"structlog-secret-ZZ"
    ACTIVE.add("tester-r1", {"LOGKEY": value})
    try:
        from meshforge.common.secret_redaction import scrub_active

        for form in _all_forms(value):
            text = form.decode("ascii", "replace")
            out = scrub_active(f"request failed for {text} (retry)")
            assert text not in out, out
            assert "[secret:LOGKEY]" in out
    finally:
        ACTIVE.remove("tester-r1")
    assert "LOGKEY" not in ACTIVE.names()


def test_not_claimed_transformations_are_documented_as_not_covered() -> None:
    """A transformation the module does not list must not be silently implied:
    the THREAT_MODEL (WS-I3) and the module docstring both say 'accidents only'.
    This test pins what is NOT redacted, so a future claim must change it."""
    value = b"limit-check-value-123"
    secs = {"K": value}
    for not_covered in (
        value[::-1],  # reversed
        base64.b32encode(value),  # base32
        base64.b64encode(base64.b64encode(value)),  # double base64
        b" ".join(bytes([c]) for c in value),  # spaced out
    ):
        assert redact_bytes(b"x" + not_covered + b"y", secs) == (
            b"x" + not_covered + b"y"
        )
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    threat = (root / "docs" / "THREAT_MODEL.md").read_text()
    assert "reversed" in threat and "accident" in threat.lower()


def test_json_or_repr_escaped_values_are_not_redacted_today() -> None:
    """Pins reality (T-4): a multi-line / quoted secret printed through
    json.dumps or repr() is NOT caught (only the listed encodings are)."""
    import json

    value = b'-----BEGIN KEY-----\nabc"def\n-----END KEY-----'
    escaped = json.dumps(value.decode()).encode()
    assert redact_bytes(b"cfg=" + escaped, {"K": value}) == b"cfg=" + escaped


@pytest.mark.xfail(
    strict=True,
    reason="T-4 (minor, docs): OPERATIONS/THREAT_MODEL name 'reversed, split, "
    "written to an output' but not JSON/repr-escaped forms of multi-line or "
    "quoted secrets, a common accident (PEM keys in json logs)",
)
def test_docs_say_escaped_forms_are_not_covered() -> None:
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    docs = (root / "docs" / "OPERATIONS.md").read_text() + (
        root / "docs" / "THREAT_MODEL.md"
    ).read_text()
    lines = [x for x in docs.splitlines() if "secret" in x.lower()]
    assert any("escap" in x.lower() for x in lines)


def test_bytes_that_cannot_start_a_secret_are_not_held_back() -> None:
    red = StreamRedactor({"K": b"a-typical-api-key-0123456789abcdef"})
    out = red.feed(b"Epoch 1 done\n")  # no byte of this can begin any form
    assert out == b"Epoch 1 done\n", f"held {red.held} bytes: live logs stall"


def test_held_back_tail_is_bounded_by_the_longest_form() -> None:
    value = b"v" * 40
    red = StreamRedactor({"K": value})
    red.feed(os.urandom(100_000))
    assert red.held <= max(len(f) for f in encoded_forms(value)) - 1


def test_not_quadratic_in_total_output() -> None:
    """Scaled-down proof: 16x the data takes ~16x, not ~256x, the time."""
    secs = {f"S{i}": os.urandom(24) for i in range(8)}
    line = b"2026-10-13 INFO step=1234 loss=0.123 acc=0.99 token=ghp_xxxxxxxxxxxx\n"

    def run(n_lines: int) -> float:
        red = StreamRedactor(secs)
        data = line * n_lines
        t0 = time.perf_counter()
        for i in range(0, len(data), 4096):
            red.feed(data[i : i + 4096])
        red.flush()
        return time.perf_counter() - t0

    small = run(4_000)
    big = run(64_000)
    assert big < small * 16 * 3 + 0.5, (small, big)


def test_one_huge_chunk_is_linear_not_quadratic() -> None:
    secs = {"K": os.urandom(32)}
    blob = b"A" * (8 * 1024 * 1024)
    t0 = time.perf_counter()
    assert redact_bytes(blob, secs) == blob
    assert time.perf_counter() - t0 < 10


def test_a_64kib_secret_compiles_quickly_enough_to_start_an_attempt() -> None:
    """Every attempt compiles one redactor per stream (+ the controller's): a
    maximum-size secret must not stall the start by tens of seconds."""
    value = os.urandom(64 * 1024)
    t0 = time.perf_counter()
    for _ in range(3):  # stdout, stderr, controller
        StreamRedactor({"BIG": value})
    assert time.perf_counter() - t0 < 10


def test_construction_cost_is_bounded_for_many_large_secrets() -> None:
    secs = {f"S{i}": os.urandom(64 * 1024) for i in range(4)}
    t0 = time.perf_counter()
    StreamRedactor(secs)
    assert time.perf_counter() - t0 < 1.0


def test_a_64kib_secret_dribbled_one_byte_at_a_time_stays_usable() -> None:
    value = os.urandom(64 * 1024)
    red = StreamRedactor({"BIG": value})
    t0 = time.perf_counter()
    for _ in range(300):
        red.feed(b"x")
    assert time.perf_counter() - t0 < 5, "per-write cost must not explode"
