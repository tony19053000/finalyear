"""V0.10 TD1 (D287 follow-up): a chunk's stderr tail is stored without terminal
escape sequences or control characters (``\\n`` / ``\\t`` kept), so no later
API / CLI / log view of it can act on an operator's terminal.

``strip_terminal_controls`` itself, then ``clean_tail`` (the one place a tail
is turned into the stored text) with hostile bytes."""

from __future__ import annotations

import random
import time

import pytest

from meshforge.common.terminal_text import strip_terminal_controls
from meshforge.controller.split.records import clean_tail
from meshforge.controller.store.models.telemetry import STDERR_TAIL_CHARS

pytestmark = pytest.mark.unit

FORBIDDEN = frozenset(
    chr(c) for c in (*range(0x20), 0x7F, *range(0x80, 0xA0)) if c not in (9, 10)
)


def _clean(text: str) -> bool:
    return not (set(text) & FORBIDDEN)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain line\n",
        "Traceback (most recent call last):\n\tFile 'x.py', line 1\n",
        "ünï😀 ключ 文字 [31m not an escape without ESC",
        "brackets [0m and ] and \\ stay",
    ],
)
def test_ordinary_text_is_unchanged(text: str) -> None:
    assert strip_terminal_controls(text) == text


@pytest.mark.parametrize(
    ("hostile", "kept"),
    [
        # colours / styles (SGR)
        ("\x1b[31mred\x1b[0m", "red"),
        ("\x1b[1;38;5;196mbold\x1b[m", "bold"),
        # clear the screen, home the cursor, erase / move up and overwrite
        ("\x1b[2J\x1b[Hgone", "gone"),
        ("line1\n\x1b[1A\x1b[2Kfake line1", "line1\nfake line1"),
        ("\x1b[?1049h\x1b[?25lalt screen", "alt screen"),
        # window title (OSC 0/2) ended by BEL or by ST
        ("\x1b]0;pwned\x07after", "after"),
        ("\x1b]2;pwned\x1b\\after", "after"),
        # clipboard write (OSC 52)
        ("\x1b]52;c;cm0gLXJmIH4=\x07x", "x"),
        # hyperlink (OSC 8): the link target goes, the visible text stays
        ("\x1b]8;;http://evil.example\x1b\\click\x1b]8;;\x1b\\", "click"),
        # DCS (sixel / terminal queries), SOS, PM, APC
        ("\x1bPq#0;2;0;0;0#1~~@@\x1b\\ok", "ok"),
        ("\x1bX sos \x1b\\\x1b^ pm \x1b\\\x1b_ apc \x1b\\ok", "ok"),
        # 8-bit C1 introducers (CSI 0x9b, OSC 0x9d ... ST 0x9c, DCS 0x90)
        ("\x9b31mred\x9b0m", "red"),
        ("\x9d0;title\x9cok", "ok"),
        ("\x90q data\x9cok", "ok"),
        # short ESC forms: charset designation, keypad mode, RIS (full reset)
        ("\x1b(0box\x1b(B", "box"),
        ("\x1b=\x1b>\x1bcreset", "reset"),
        # a bare CR would overprint the line: it becomes a line break
        ("real error\rFAKE OK  ", "real error\nFAKE OK  "),
        ("50%\r100%\r\n", "50%\n100%\n"),
        ("a\r\nb\r\n", "a\nb\n"),
        # backspaces / NUL / BEL / VT / FF / DEL / other C1: dropped
        ("secret\x08\x08\x08\x08\x08\x08shown", "secretshown"),
        ("a\x00b\x07c\x0bd\x0ce\x7ff\x85g\x8dh", "abcdefgh"),
        # tabs and newlines kept
        ("col1\tcol2\nrow2\n", "col1\tcol2\nrow2\n"),
        # unterminated string sequences: only the introducer goes, text stays
        ("\x1b]0;no terminator here", "0;no terminator here"),
        ("\x1bPno terminator", "no terminator"),
        # a lone or truncated ESC (the tail cut it)
        ("abc\x1b", "abc"),
        ("abc\x1b[", "abc"),
        ("abc\x1b[12;", "abc12;"),
    ],
)
def test_hostile_sequences_are_removed_and_the_text_kept(
    hostile: str, kept: str
) -> None:
    got = strip_terminal_controls(hostile)
    assert got == kept
    assert _clean(got)


def test_random_input_never_leaves_a_control_and_is_idempotent() -> None:
    rng = random.Random(20261021)  # noqa: S311 - a reproducible fuzz, not crypto
    alphabet = [chr(c) for c in range(0xA0)] + ["é", "😀", "\x1b[", "\x1b]"]
    for _ in range(2000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
        once = strip_terminal_controls(text)
        assert _clean(once), repr(text)
        assert strip_terminal_controls(once) == once, repr(text)


@pytest.mark.parametrize(
    "pathological",
    [
        "\x1b]" * 30_000,
        "\x1bP" * 30_000,
        "\x1b[" + "0" * 60_000,
        "\x1b]0;" + "x" * 60_000,
        ("\x1b]" + "y" * 50) * 1_000,
    ],
)
def test_pathological_input_is_linear(pathological: str) -> None:
    start = time.perf_counter()
    got = strip_terminal_controls(pathological)
    assert time.perf_counter() - start < 2.0
    assert _clean(got)


# --------------------------------------------------------------- clean_tail


def test_clean_tail_strips_a_hostile_stderr_tail_before_it_is_stored() -> None:
    data = (
        b"Traceback (most recent call last):\n"
        b"\x1b]0;owned\x07\x1b]52;c;cm0gLXJmIH4=\x07"
        b"\x1b[2J\x1b[H\x1b[31mValueError: bad item\x1b[0m\r\n"
        b"progress 10%\rprogress 100%\n"
        # a raw 0x9b byte is invalid UTF-8 (-> U+FFFD, inert); "\xc2\x9b" IS U+009B (CSI)
        b"\x9b?25l\xc2\x9b31m\x00done\t\x07\n"
    )
    tail = clean_tail(data, 16_384)
    assert tail == (
        "Traceback (most recent call last):\n"
        "ValueError: bad item\n"
        "progress 10%\nprogress 100%\n"
        "�?25ldone\t\n"
    )
    assert tail is not None and _clean(tail)


def test_clean_tail_of_only_controls_is_none() -> None:
    assert clean_tail(b"\x1b[2J\x1b[H\x07\x00\x1b]0;t\x07", 100) is None


def test_clean_tail_limits_still_hold_after_stripping() -> None:
    noisy = (b"\x1b[31m" + b"x" * 10 + b"\x1b[0m\r\n") * 20_000
    for limit in (1, 7, 100, 16_384, STDERR_TAIL_CHARS * 4):
        tail = clean_tail(noisy, limit)
        assert tail is not None and _clean(tail)
        assert len(tail.encode()) <= min(limit, STDERR_TAIL_CHARS)
        assert tail.endswith("x\n") or limit < 3
