"""V0.10 TD2 (D-1): ``strip_terminal_controls`` also drops the bidi
embedding / override / isolate / mark characters and the zero-width spoofing
characters, stays linear on 64 KiB of them (mixed with every escape
introducer), and leaves ordinary text alone."""

from __future__ import annotations

import time

import pytest

from meshforge.common.terminal_text import strip_terminal_controls
from meshforge.controller.split.records import clean_tail

pytestmark = pytest.mark.unit

BIDI = [
    *(chr(p) for p in range(0x202A, 0x202F)),
    *(chr(p) for p in range(0x2066, 0x206A)),
    "‎",
    "‏",
    "؜",
]
ZERO_WIDTH = ["​", "‌", "‍", "⁠", "﻿"]
SPOOFING = BIDI + ZERO_WIDTH
INTRODUCERS = ["\x1b", "\x1b[", "\x1b]", "\x1bP", "\x9b", "\x9d", "\x90", "\x9e"]


@pytest.mark.parametrize("char", SPOOFING, ids=lambda c: f"U+{ord(c):04X}")
def test_every_spoofing_character_is_dropped_and_the_neighbours_kept(
    char: str,
) -> None:
    assert strip_terminal_controls(f"a{char}b") == "ab"
    assert strip_terminal_controls(char * 3) == ""


def test_the_classic_rlo_filename_spoof_reads_in_storage_order() -> None:
    # displayed as "invoice_exe.pdf" by a bidi-aware terminal / browser
    assert strip_terminal_controls("invoice_‮fdp.exe") == "invoice_fdp.exe"
    assert strip_terminal_controls("⁧evil⁩ ok") == "evil ok"


def test_zero_width_hidden_inside_a_word_is_gone() -> None:
    assert strip_terminal_controls("pay​pal⁠.com﻿") == "paypal.com"


def test_zwj_emoji_sequence_loses_its_joiner_trade_off() -> None:
    """The documented trade-off: the joined glyph becomes its parts."""
    family = "\U0001f469‍\U0001f467"
    assert strip_terminal_controls(family) == "\U0001f469\U0001f467"


@pytest.mark.parametrize(
    "text",
    [
        "plain ascii\n\twith tab",
        "مرحبا שלום",  # RTL letters stay
        "   separators",
        " nbsp ­ soft hyphen",
        "\U0001f680 rocket",
    ],
)
def test_ordinary_text_including_rtl_letters_is_untouched(text: str) -> None:
    assert strip_terminal_controls(text) == text


def test_spoofing_inside_an_escape_sequence_body_is_removed_with_it() -> None:
    assert strip_terminal_controls("a\x1b]0;‮title\x07b") == "ab"
    # unterminated: only the introducer goes; the rest is text, spoofing dropped
    assert strip_terminal_controls("a\x1b]0;‮title") == "a0;title"


def test_clean_tail_drops_spoofing_before_storage() -> None:
    data = "ok ‮exe.txt​\n".encode()
    assert clean_tail(data, 1000) == "ok exe.txt\n"
    assert clean_tail("​‮﻿".encode(), 1000) is None


def _timed(text: str) -> tuple[float, str]:
    start = time.perf_counter()
    out = strip_terminal_controls(text)
    return time.perf_counter() - start, out


@pytest.mark.parametrize(
    "unit",
    [
        *SPOOFING,
        "".join(SPOOFING),
        *(intro + spoof for intro in INTRODUCERS for spoof in ("‮", "​")),
        "\x1b]0;‮",
        "\x1bP⁦x",
        "\x9d‍\x90؜",
    ],
    ids=lambda u: "+".join(f"{ord(c):04X}" for c in u),
)
def test_64_kib_of_spoofing_and_introducers_stays_linear(unit: str) -> None:
    """The TD1 ReDoS lesson: every input the controller can be handed (a 64 KiB
    tail) is processed in bounded, linear time - 4x the input is ~4x the time."""
    big = unit * (65_536 // len(unit))
    small = big[: len(big) // 4]
    t_small, _ = _timed(small)
    t_big, out = _timed(big)
    assert t_big < 0.25
    assert t_big < max(t_small, 0.005) * 12
    assert not any(c in SPOOFING or c == "\x1b" or 0x80 <= ord(c) <= 0x9F for c in out)


def test_idempotent_with_spoofing_mixed_in() -> None:
    raw = "\x1b[31m‮A\r\n​B\x1b]0;t\x07﻿\x9d⁦"
    once = strip_terminal_controls(raw)
    assert strip_terminal_controls(once) == once
