"""V0.10 TD2 tester: independent attacks on ``strip_terminal_controls`` -
every code point of the Unicode range checked against a spec written from the
docstring (not from the implementation's table), sequences with spoofing
inside, 64 KiB timing for mixes with 8-bit introducers, idempotence and
no-hidden-text properties."""

from __future__ import annotations

import random
import time
import unicodedata

import pytest

from meshforge.common.terminal_text import strip_terminal_controls as strip

pytestmark = pytest.mark.unit

BIDI = [
    *(0x202A, 0x202B, 0x202C, 0x202D, 0x202E),
    *(0x2066, 0x2067, 0x2068, 0x2069),
    *(0x200E, 0x200F, 0x061C),
]
ZERO_WIDTH = [0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF]
SPOOF = [*BIDI, *ZERO_WIDTH]
INTRODUCERS = [
    *("\x1b[", "\x1b]", "\x1bP", "\x1bX", "\x1b^", "\x1b_"),
    *("\x9b", "\x9d", "\x90", "\x98", "\x9e", "\x9f"),
]


def _spec_dropped(cp: int) -> bool:
    """Written from the module docstring: C0 except tab / newline, DEL, C1,
    the bidi marks and the zero-width characters."""
    if cp in (0x09, 0x0A):
        return False
    if cp != 0xAD and unicodedata.category(chr(cp)) == "Cf":  # review N1
        return True
    return cp < 0x20 or cp == 0x7F or 0x80 <= cp <= 0x9F or cp in SPOOF


def test_every_code_point_against_the_spec() -> None:
    """All 1,114,112 code points, each in the middle of a word."""
    wrong: list[int] = []
    for cp in range(0x110000):
        got = strip("a" + chr(cp) + "b")
        if cp == 0x0D:
            want = "a\nb"  # CR is a line break
        elif _spec_dropped(cp):
            want = "ab"
        else:
            want = "a" + chr(cp) + "b"
        # ESC / C1 introducers (0x1b, 0x90, 0x98, 0x9b, 0x9d-0x9f) may also
        # swallow "b" as the final byte of a short sequence; only demand that
        # no control survives and the 'a' stays
        if cp == 0x1B or cp in (0x90, 0x98, 0x9B, 0x9D, 0x9E, 0x9F):
            if not got.startswith("a") or any(
                ord(c) < 0x20 and c not in "\n\t" for c in got
            ):
                wrong.append(cp)
            continue
        if got != want:
            wrong.append(cp)
    assert wrong == [], [hex(c) for c in wrong[:20]]


@pytest.mark.parametrize("cp", SPOOF)
def test_each_listed_code_point_is_dropped_everywhere(cp: int) -> None:
    ch = chr(cp)
    assert strip(ch) == ""
    assert strip(ch * 1000) == ""
    assert strip(f"{ch}start") == "start"
    assert strip(f"end{ch}") == "end"
    assert strip(f"a{ch}{ch}{ch}b") == "ab"
    assert strip(f"\u65e5{ch}\u672c") == "\u65e5\u672c"


@pytest.mark.parametrize("intro", INTRODUCERS)
@pytest.mark.parametrize("cp", [0x202E, 0x200B, 0xFEFF, 0x2066])
def test_spoofing_chars_inside_and_around_escape_sequences(intro: str, cp: int) -> None:
    ch = chr(cp)
    for text in (
        f"x{intro}{ch}31m{ch}y",
        f"x{ch}{intro}31m\x07{ch}y",
        f"x{intro}0;t{ch}itle\x07y",
        f"x{intro}0;title\x07{ch}y",
        f"x{intro}{ch}\x1b\\y",
    ):
        got = strip(text)
        assert not any(c in got for c in ("\x1b", "\x07", "\x9b", "\x9d", "\x9c")), (
            text,
            got,
        )
        assert chr(cp) not in got, (text, got)
        assert got.startswith("x") and got.endswith("y"), (text, got)


def test_terminated_sequence_with_spoof_inside_body_is_removed_whole() -> None:
    assert strip("a\x1b]0;ti\u202etle\x07b") == "ab"
    assert strip("a\x1b]0;ti\u200btle\x1b\\b") == "ab"
    assert strip("a\x1bPdata\u2066\x9cb") == "ab"


def test_unterminated_sequence_never_hides_the_text_after_it() -> None:
    for intro in ("\x1b]", "\x9d", "\x1bP", "\x90"):
        got = strip(f"pre{intro}0;visible words")
        assert "visible words" in got, (intro, got)
        assert got.startswith("pre")


def test_a_spoof_char_cannot_assemble_an_escape_sequence() -> None:
    """Dropping a char between ESC and '[' must not leave a working CSI."""
    got = strip("\x1b\u202e[31mred")
    assert "\x1b" not in got and "\x9b" not in got
    assert "red" in got


def test_ordinary_text_is_untouched() -> None:
    for text in (
        "hello world",
        "tab\there\nnewline",
        "\u65e5\u672c\u8a9e",
        "caf\u00e9 na\u00efve",
        "\u05e9\u05dc\u05d5\u05dd \u0627\u0644\u0633\u0644\u0627\u0645",  # RTL letters
        "emoji \U0001f600 \U0001f468",
        "a\u00a0b\u00adc",  # NBSP, soft hyphen
        "x\u2028y",
    ):
        assert strip(text) == text


def test_tag_characters_ascii_smuggling_is_removed() -> None:
    """U+E0000-U+E007F render as nothing yet encode ASCII (a hidden prompt)."""
    hidden = "".join(chr(0xE0000 + ord(c)) for c in "ignore previous")
    assert strip("safe" + hidden + "text") == "safetext"
    assert strip(hidden) == ""


def test_input_never_mutated_and_str_type() -> None:
    text = "a\u202eb"
    out = strip(text)
    assert text == "a\u202eb" and type(out) is str


@pytest.mark.parametrize("seed", range(40))
def test_fuzz_idempotent_and_safe(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311
    pieces = [
        *INTRODUCERS,
        "\x07",
        "\x9c",
        "\x1b\\",
        "\r",
        "\r\n",
        "\n",
        "\t",
        "abc",
        "31m",
        ";",
        "0",
        " ",
        "[",
        "]",
        "~",
        *(chr(c) for c in SPOOF),
        "\u65e5",
        "\x00",
        "\x7f",
        "\u2028",
    ]
    text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 300)))
    once = strip(text)
    assert strip(once) == once, repr(text)
    assert not any(
        (ord(c) < 0x20 and c not in "\n\t") or 0x7F <= ord(c) <= 0x9F or ord(c) in SPOOF
        for c in once
    ), repr(text)
    # a text without any introducer keeps every visible character
    plain = text.replace("\x1b", "")
    for c in "\x9b\x9d\x90\x98\x9e\x9f":
        plain = plain.replace(c, "")
    assert strip(plain).count("\u65e5") == plain.count("\u65e5")


# ------------------------------------------------------------------ timing
def _bound(text: str, limit_s: float = 1.0) -> None:
    start = time.perf_counter()
    strip(text)
    one = time.perf_counter() - start
    assert one < limit_s, f"{one:.2f}s for {len(text)} chars"
    doubled = text * 4
    start = time.perf_counter()
    strip(doubled)
    four = time.perf_counter() - start
    # linear: 4x input must stay far below quadratic (16x); generous for load
    assert four < max(0.15, one * 14), (one, four)


SIZE = 65_536


@pytest.mark.parametrize("intro", INTRODUCERS)
def test_64_kib_of_unterminated_introducers_mixed_with_spoof(intro: str) -> None:
    unit = intro + "\u202e" + "\u200b"
    _bound(unit * (SIZE // len(unit)))


@pytest.mark.parametrize("intro", INTRODUCERS)
def test_64_kib_of_one_introducer_then_a_long_body(intro: str) -> None:
    _bound(intro + "0;" * (SIZE // 2))
    _bound(intro + ("\u202e" * (SIZE // 2)))


def test_64_kib_of_all_introducers_interleaved() -> None:
    unit = "".join(INTRODUCERS) + "\u200f"
    _bound(unit * (SIZE // len(unit)))


def test_64_kib_of_parameter_then_intermediate_bytes_without_a_final_byte() -> None:
    _bound("\x1b[" + "0" * (SIZE // 2) + " " * (SIZE // 2))
    _bound("\x9b" + "0" * (SIZE // 2) + " " * (SIZE // 2))
    _bound(("\x1b[" + "0" * 30 + " " * 30) * (SIZE // 62))
    _bound("\x1b" + " " * SIZE)
    _bound(("\x1b" + " " * 30) * (SIZE // 31))


def test_64_kib_with_only_spoof_chars_and_only_controls() -> None:
    _bound("".join(chr(c) for c in SPOOF) * (SIZE // len(SPOOF)))
    _bound("\r\n\x00\x7f\x85" * (SIZE // 5))


def test_64_kib_of_osc_starts_with_late_terminators() -> None:
    _bound("\x1b]0;" * (SIZE // 4) + "\x07")
    _bound("\x9d" * (SIZE // 2) + "\x9c")
    _bound("\x1b]" + "a" * SIZE + "\x07")
