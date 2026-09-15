"""Fix round 3 of task 0.1.6: ``scrub_text`` value rules, size cap and linear time.

Perf bounds are generous (slow CI) but still far below what a quadratic scrubber
needs: the pre-fix implementation took seconds on these inputs.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from meshforge.common.logging import MAX_SCRUB_CHARS, REDACTED, scrub_text

pytestmark = pytest.mark.unit

_BUDGET_S = 0.1


def _best_time(text: str, runs: int = 3) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        scrub_text(text)
        best = min(best, time.perf_counter() - start)
    return best


def _word_chars(n: int) -> str:
    return "x" * n


def _colon_pairs(n: int) -> str:
    return "a:" * (n // 2)


def _base64url(n: int) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    return "".join(alphabet[(i * 7) % 64] for i in range(n))


def _telemetry(n: int) -> str:
    rec = "job_id=job_01J8Z attempt=2 node=laptop-1 phase=exec ms=12.5 ok=true "
    return (rec * (n // len(rec) + 1))[:n]


def _dotted(n: int) -> str:  # stresses the URL-credentials scheme pattern
    return "a." * (n // 2)


def _sensitive_pairs(n: int) -> str:
    return "token=abc " * (n // 10)


def _unbalanced_brackets(n: int) -> str:
    return "password={\n" * (n // 11)


def _unclosed_quote_keys(n: int) -> str:
    return "pwd=' " * (n // 6)


_INPUTS: dict[str, Callable[[int], str]] = {
    "word_chars": _word_chars,
    "colon_pairs": _colon_pairs,
    "base64url": _base64url,
    "telemetry": _telemetry,
    "dotted": _dotted,
    "sensitive_pairs": _sensitive_pairs,
    "unbalanced_brackets": _unbalanced_brackets,
    "unclosed_quote_keys": _unclosed_quote_keys,
}


@pytest.mark.parametrize(
    ("name", "size"),
    [
        ("word_chars", 100_000),
        ("colon_pairs", 100_000),
        ("base64url", 20_000),
        ("telemetry", 100_000),
        ("dotted", 100_000),
        ("sensitive_pairs", 100_000),
        ("unbalanced_brackets", 100_000),
    ],
)
def test_scrub_text_is_fast_on_large_inputs(name: str, size: int) -> None:
    elapsed = _best_time(_INPUTS[name](size))
    assert elapsed < _BUDGET_S, f"{name}: {elapsed * 1000:.1f} ms for {size} chars"


@pytest.mark.parametrize("name", sorted(_INPUTS))
def test_scrub_text_scales_linearly(name: str) -> None:
    n = 8_000  # 4n = 32 000 stays below MAX_SCRUB_CHARS, so the cap does not help
    small = _best_time(_INPUTS[name](n), runs=5)
    large = _best_time(_INPUTS[name](4 * n), runs=5)
    ratio = large / max(small, 1e-6)
    assert ratio < 8, f"{name}: time(4n)/time(n) = {ratio:.1f} (super-linear)"


# ----------------------------------------------------------------- value rules


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("password=sup3r]s3cr3t", f"password={REDACTED}"),
        ("token=ab)cd;ef rest", f"token={REDACTED} rest"),
        ("api_key=xy&z123,more next", f"api_key={REDACTED} next"),
        ('password={"a":"topsecret"} n=1', f"password={REDACTED} n=1"),
        ('secret=[1, {"x": "]"}, 3] n=1', f"secret={REDACTED} n=1"),
        ("token='a b c' n=1", f"token='{REDACTED}' n=1"),
        ('{"password": "a\\"b c"}', f'{{"password": "{REDACTED}"}}'),
        ("token='unclosed and the rest", f"token='{REDACTED}"),
        ("password={never closed\nnext line", f"password={REDACTED}"),
        ("--password=hunter2 --verbose", f"--password={REDACTED} --verbose"),
        ("dsn=postgres://svc:hunter2@db.internal:5432/prod x", f"dsn={REDACTED} x"),
        ("password=", "password="),
    ],
)
def test_sensitive_value_rules(text: str, expected: str) -> None:
    assert scrub_text(text) == expected


def test_already_redacted_text_is_stable() -> None:
    once = scrub_text("db_url=postgres://u:p@host:5432/db token=[1,2]")
    assert once == f"db_url={REDACTED} token={REDACTED}"
    assert scrub_text(once) == once


def test_long_text_is_truncated_with_marker() -> None:
    head = "token=abc " + "x" * (MAX_SCRUB_CHARS - 10)
    tail = "password=leaked " * 100
    out = scrub_text(head + tail)
    assert out.startswith(f"token={REDACTED} ")
    assert "leaked" not in out
    assert out.endswith(f"[TRUNCATED {len(tail)} bytes]")


def test_truncation_counts_utf8_bytes() -> None:
    out = scrub_text("a" * MAX_SCRUB_CHARS + "é" * 10)
    assert out.endswith("[TRUNCATED 20 bytes]")


def test_text_at_cap_is_not_truncated() -> None:
    text = "a" * MAX_SCRUB_CHARS
    assert scrub_text(text) == text
