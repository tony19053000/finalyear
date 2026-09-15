"""Tester re-test of task 0.1.6 fix round 3.

Independent verification that DEFECT-1 (secret leaked after ``) } ] ; & ,`` in
unquoted sensitive values) and DEFECT-2 (O(n^2) scrubber) reported in the previous
round are actually fixed, plus new attacks against the manual value scanner added
in this round (quotes, brackets, unicode whitespace, long/unicode keys, URL query
strings, repeated keys, JSON-array-shaped secrets) and its performance on
adversarial inputs at/around the ``MAX_SCRUB_CHARS`` boundary.

Where a finding is a genuine plaintext secret leak the test asserts secure
behaviour and is left failing (not weakened) if the implementation does not meet
it. Where a finding is a *documented* known gap (see ``redaction.py`` module
docstring) the test instead confirms the documented boundary holds, so a future
change to that boundary is caught.
"""

from __future__ import annotations

import time

import pytest

from meshforge.common.logging import MAX_SCRUB_CHARS, scrub_text

pytestmark = pytest.mark.unit


# ------------------------------------------------- DEFECT-1 regression (independent)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("password=sup3r]s3cr3t rest", "s3cr3t"),
        ("token=ab)cd;ef rest", "cd;ef"),
        ("api_key=xy&z123 rest", "z123"),
        ("secret=a,b,c rest", "b,c"),
        ("apikey=x}qzv9 rest", "qzv9"),
    ],
)
def test_defect1_special_char_after_value_no_longer_leaks(
    text: str, secret: str
) -> None:
    out = scrub_text(text)
    assert secret not in out, f"DEFECT-1 regressed: {text!r} -> {out!r}"


# ------------------------------------------------- DEFECT-2 regression (independent)


def test_defect2_no_quadratic_blowup_on_long_unbroken_token() -> None:
    """8k vs 32k unbroken word-char run must scale ~linearly, not quadratically."""

    def timed(n: int) -> float:
        text = "x" * n
        best = float("inf")
        for _ in range(3):
            start = time.perf_counter()
            scrub_text(text)
            best = min(best, time.perf_counter() - start)
        return best

    small = timed(8_000)
    large = timed(32_000)
    ratio = large / max(small, 1e-6)
    assert ratio < 8, f"DEFECT-2 regressed: 4x input took {ratio:.1f}x longer"


# ---------------------------------------------------- new scanner: leak attempts


def test_url_query_string_token_is_fully_redacted_not_leaked() -> None:
    """A sensitive key inside a URL query string must not leak its value, even
    though the surrounding ``&other=1`` gets swallowed too (documented
    over-redaction: punctuation does not end an unquoted value)."""
    out = scrub_text("http://h/?token=abc123&x=1")
    assert "abc123" not in out


def test_authorization_basic_kv_is_fully_redacted() -> None:
    out = scrub_text("Authorization=Basic dXNlcjpwYXNzd29yZA==")
    assert "dXNlcjpwYXNzd29yZA" not in out


def test_repeated_sensitive_keys_all_redacted() -> None:
    out = scrub_text("password=first password=second token=third")
    assert "first" not in out
    assert "second" not in out
    assert "third" not in out


def test_password_plus_concatenation_value_redacted() -> None:
    out = scrub_text('password = "supersecretvalue" + more')
    assert "supersecretvalue" not in out


@pytest.mark.parametrize(
    "sep",
    ["\t", "\n", "\xa0", "\u2003", "\u2028"],
)
def test_value_split_by_whitespace_variants_does_not_leak_the_token_itself(
    sep: str,
) -> None:
    """Regardless of which unicode whitespace character follows the token, the
    token characters up to that separator must never appear in the clear."""
    out = scrub_text(f"token=secretpart{sep}restoftext")
    assert "secretpart" not in out


def test_zero_width_space_inside_value_is_conservatively_redacted() -> None:
    """A zero-width space is not recognised as whitespace by the scanner, so the
    whole run (including trailing text glued to it) is redacted -- safe
    (over-redaction), not a leak."""
    out = scrub_text("token=a\u200bsecrettail rest")
    assert "secrettail" not in out


# --------------------------------------------- new scanner: documented boundaries


def test_key_of_exactly_64_chars_is_still_recognised() -> None:
    key = "password" + "_" * 56
    assert len(key) == 64
    out = scrub_text(f"{key}=hunter2 x")
    assert "hunter2" not in out


def test_key_of_65_chars_is_a_documented_gap_not_a_new_regression() -> None:
    """Module docstring: 'keys longer than 64 characters... are not recognised.'
    This pins the documented boundary; it is *not* a new defect, but if this
    boundary silently moves (e.g. gets worse) this test will catch it."""
    key = "password" + "_" * 57
    assert len(key) == 65
    out = scrub_text(f"{key}=hunter2 x")
    assert out == f"{key}=hunter2 x"  # documented gap: unredacted


# ------------------------------------------- new scanner: gaps found this round


@pytest.mark.xfail(
    reason=(
        "MINOR/DEFECT (not blocking): a sensitive key with non-ASCII letters "
        "(e.g. accented) is not recognised by _KV_KEY_RE, which only matches "
        "[A-Za-z_]. Not in the documented 'known gaps' list. Low real-world "
        "likelihood (field names are near-universally ASCII in this codebase) "
        "but should be documented or fixed in a future round."
    ),
    strict=True,
)
def test_unicode_letter_key_is_recognised_as_sensitive() -> None:
    out = scrub_text("pásswörd=geheim123 rest")
    assert "geheim123" not in out


@pytest.mark.xfail(
    reason=(
        "MINOR/DEFECT (not blocking, out of documented scope): a JSON-array- or "
        "tuple-shaped ('key', 'value') pair where the sensitive word appears as "
        "an array element rather than via '=' or ':' is not recognised at all, "
        'e.g. ["password", "topsecretvalue"] or ("password", "topsecretvalue"). '
        "The module docstring's scope is 'key=value / key: value pairs', so this "
        "is a scope gap, not a regression, but worth tracking."
    ),
    strict=True,
)
def test_json_array_shaped_password_value_pair_is_redacted() -> None:
    out = scrub_text('["password", "topsecretvalue"]')
    assert "topsecretvalue" not in out


# --------------------------------------------------------- over-redaction guard


@pytest.mark.parametrize(
    "text",
    [
        "url=http://host:8080/path",
        "session_id=sess_01J8Z3K5Q9V6W2X4Y7A0B1C2D3",
        "pin_memory=True",
        "num_tokens=5",
        "progress=50%",
        "time 12:30:00 event: started",
        "idempotency_key=abc123",
    ],
)
def test_normal_telemetry_survives_round3_scanner_unchanged(text: str) -> None:
    assert scrub_text(text) == text


# --------------------------------------------------------------------- performance


def test_perf_many_unclosed_quotes() -> None:
    text = "password='unclosed " * 5000
    start = time.perf_counter()
    scrub_text(text)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"{elapsed * 1000:.1f} ms for many unclosed quotes"


def test_perf_repeated_unclosed_open_bracket() -> None:
    text = "password=[" * 20_000
    start = time.perf_counter()
    scrub_text(text)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"{elapsed * 1000:.1f} ms for repeated 'password=['"


def test_perf_deeply_nested_brackets_within_one_value() -> None:
    text = "password=" + "[" * 2000 + "x" + "]" * 2000 + " next"
    start = time.perf_counter()
    scrub_text(text)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"{elapsed * 1000:.1f} ms for deeply nested brackets"


def test_perf_worst_case_unbalanced_brackets_at_cap() -> None:
    unit = "password={"
    text = (unit * (MAX_SCRUB_CHARS // len(unit) + 1))[:MAX_SCRUB_CHARS]
    start = time.perf_counter()
    scrub_text(text)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"{elapsed * 1000:.1f} ms for unbalanced brackets at cap"


# --------------------------------------------------- truncation / multi-byte boundary


def test_truncation_boundary_does_not_split_multibyte_char_or_leak() -> None:
    """A quoted secret whose closing quote falls after the MAX_SCRUB_CHARS cut,
    with multi-byte characters straddling the cut, must not leak the secret and
    must not raise (invalid UTF-8 sequences are impossible in a Python str, but
    the produced marker/text must still be valid and secret-free)."""
    prefix = "x" * (MAX_SCRUB_CHARS - 20)
    text = (
        prefix
        + "token='"
        + ("é" * 15)
        + "SECRETVALUE"
        + ("日" * 10)
        + "' rest visible-marker-XYZ"
    )
    out = scrub_text(text)
    assert "SECRETVALUE" not in out
    assert "visible-marker-XYZ" not in out
    out.encode("utf-8")  # must not raise


def test_truncation_at_cap_with_trailing_emoji_is_stable() -> None:
    text = "a" * (MAX_SCRUB_CHARS - 1) + "\U0001f600" * 5
    out = scrub_text(text)
    assert out.encode("utf-8")
    assert "[TRUNCATED" in out
