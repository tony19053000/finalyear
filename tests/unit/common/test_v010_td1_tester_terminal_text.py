"""V0.10 TD1 tester (independent): ``strip_terminal_controls`` and
``clean_tail`` against hostile stderr.

Derived from the property the feature promises, not from the implementation:
whatever a job writes, the stored text holds no ESC, no C1, no DEL, no C0 but
``\\n`` / ``\\t``, no CR; ordinary text survives; processing time stays small
for any input the controller can be handed (<= 64 KiB of tail).
"""

from __future__ import annotations

import random
import time

import pytest

from meshforge.common.terminal_text import strip_terminal_controls as strip
from meshforge.controller.split.records import clean_tail
from meshforge.controller.store.models.telemetry import STDERR_TAIL_CHARS

pytestmark = pytest.mark.unit

ALLOWED = {"\n", "\t"}


def _is_clean(text: str) -> bool:
    return all(
        ch in ALLOWED or not (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F) for ch in text
    )


# ----------------------------------------------------------- exact outcomes
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\x1b[31mred\x1b[0m", "red"),
        ("\x1b[2J\x1b[Hgone", "gone"),
        ("a\x1b[1;31;4mb", "ab"),
        ("\x1b[?25lhide", "hide"),  # private-mode CSI
        ("\x1b[38;2;1;2;3mx", "x"),
        ("\x1b[3 qx", "x"),  # intermediate byte before the final byte
        ("\x9b31mred", "red"),  # 8-bit CSI
        ("\x1b]0;evil title\x07ok", "ok"),  # OSC terminated by BEL
        ("\x1b]0;evil title\x1b\\ok", "ok"),  # OSC terminated by ST
        ("\x9d0;t\x9cok", "ok"),  # 8-bit OSC + 8-bit ST
        ("\x1b]52;c;ZXZpbA==\x07ok", "ok"),  # clipboard write
        ("\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\", "link"),  # hyperlink
        ("\x1bPqsixel\x1b\\after", "after"),  # DCS
        ("\x90q\x9cafter", "after"),  # 8-bit DCS
        ("\x1bXsos\x1b\\a\x1b^pm\x1b\\b\x1b_apc\x1b\\c", "abc"),
        ("\x1bcreset", "reset"),  # ESC c
        ("\x1b7save\x1b8", "save"),
        ("\x1b(Bx", "x"),  # charset designation (intermediate)
        ("bell\x07!", "bell!"),
        ("back\x08\x08\x08xyz", "backxyz"),
        ("nul\x00byte", "nulbyte"),
        ("del\x7fx", "delx"),
        ("c1\x85\x9fx", "c1x"),
        ("tab\tnl\nkept", "tab\tnl\nkept"),
        ("form\x0cfeed\x0bvt", "formfeedvt"),
    ],
)
def test_exact_outputs(raw: str, expected: str) -> None:
    assert strip(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\r\nb", "a\nb"),
        ("a\rb", "a\nb"),
        ("a\r\r\nb", "a\n\nb"),
        ("a\n\rb", "a\n\nb"),
        ("progress 1%\rprogress 2%\rdone\r\n", "progress 1%\nprogress 2%\ndone\n"),
        ("\r", "\n"),
        ("\r\n\r\n", "\n\n"),
    ],
)
def test_carriage_returns_become_line_breaks_never_overprint(
    raw: str, expected: str
) -> None:
    assert strip(raw) == expected
    assert "\r" not in strip(raw)


def test_cr_inside_a_removed_sequence_does_not_leak() -> None:
    # a CR inside CSI parameters breaks the sequence; whatever remains is inert
    out = strip("\x1b[3\r1mX")
    assert _is_clean(out) and "\x1b" not in out and "X" in out


# ------------------------------------------------------- unterminated forms
@pytest.mark.parametrize(
    "raw",
    [
        "\x1b]0;title",  # unterminated OSC
        "\x1b]52;c;" + "A" * 100,
        "\x1bPdata",  # unterminated DCS
        "\x9d0;title",
        "\x90data",
        "\x1b[",  # truncated CSI
        "\x1b[31",  # CSI without final
        "\x1b",  # lone ESC at the end
        "text\x1b",
        "\x9b",
    ],
)
def test_unterminated_sequences_leave_no_control_and_hide_no_text(raw: str) -> None:
    out = strip(raw)
    assert _is_clean(out)
    # the payload text of an unterminated string sequence is kept (not swallowed)
    for payload in ("title", "data"):
        if payload in raw:
            assert payload in out


def test_unterminated_osc_followed_by_real_text_does_not_hide_the_rest() -> None:
    out = strip("\x1b]0;title\nERROR: real failure here\n")
    assert "ERROR: real failure here" in out


def test_osc_broken_by_a_nested_escape_still_strips_the_nested_csi() -> None:
    out = strip("\x1b]0;t\x1b[2Jtext")
    assert "\x1b" not in out and "text" in out


def test_sequence_split_by_removal_cannot_be_reassembled() -> None:
    # removing the inner sequence must not leave a fresh live ESC sequence
    for raw in ("\x1b\x1b[31m[2J", "\x1b[\x1b[31m2J", "\x1b\x00[2J", "\x1b\r[2J"):
        out = strip(raw)
        assert _is_clean(out), (raw, out)
        assert strip(out) == out


# ------------------------------------------------------------------ unicode
@pytest.mark.parametrize(
    "text",
    [
        "héllo wörld",
        "\u65e5\u672c\u8a9e\u306e\u30a8\u30e9\u30fc: not found",
        "emoji \U0001f680\U0001f525 and ZWJ \U0001f469\u200d\U0001f4bb",
        "\u0391\u0392\u0393 \u00a0nbsp \u00e9 \u00ff",  # U+00A0..U+00FF are not C1
        "\u2028\u2029 separators",
        "\U0001d518\U0001d52b",
        "a\ufffdb",
    ],
)
def test_ordinary_unicode_is_untouched(text: str) -> None:
    assert strip(text) == text


def test_c1_as_unicode_codepoints_are_stripped_but_neighbours_kept() -> None:
    for point in range(0x80, 0xA0):
        assert (
            strip(f"a{chr(point)}b") == "ab" or chr(point) in "\x90\x98\x9b\x9d\x9e\x9f"
        )


@pytest.mark.xfail(
    strict=True,
    reason="OBSERVATION (minor): bidi overrides / zero-width spoofing characters "
    "are not terminal controls and are kept; listed in the tester report",
)
def test_bidi_override_is_removed() -> None:
    assert chr(0x202E) not in strip("harmless" + chr(0x202E) + "txt.exe")  # RLO


def test_empty_and_whitespace() -> None:
    assert strip("") == ""
    assert strip("   ") == "   "
    assert strip("\n\n\n") == "\n\n\n"


def test_idempotent_on_exact_cases() -> None:
    for raw in ("\x1b[31mA\r\nB\x1b]0;t\x07\x00", "\x9b1m\x9d0;x\x9c"):
        once = strip(raw)
        assert strip(once) == once


# ------------------------------------------------------------------ fuzzing
_ATOMS = [
    "\x1b",
    "\x1b[",
    "\x1b]",
    "\x1bP",
    "\x1b\\",
    "\x9b",
    "\x9d",
    "\x9c",
    "\x90",
    "\x07",
    "\r",
    "\n",
    "\t",
    "\x00",
    "\x7f",
    "31m",
    "0;",
    ";",
    "?",
    " ",
    "A",
    "é",
    "\u0085",
    "[",
    "]",
    "\\",
    "\x18",
    "\x1a",
]


@pytest.mark.parametrize("seed", range(5))
def test_fuzz_output_is_clean_idempotent_and_a_subsequence(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311
    for _ in range(1500):
        raw = "".join(rng.choice(_ATOMS) for _ in range(rng.randint(0, 40)))
        out = strip(raw)
        assert _is_clean(out), (raw, out)
        assert strip(out) == out, (raw, out)
        # never invents text: every output char occurs in the input (CR -> LF aside)
        pool = set(raw) | {"\n"}
        assert set(out) <= pool, (raw, out)


def test_plain_text_between_sequences_is_always_preserved() -> None:
    rng = random.Random(7)  # noqa: S311
    seqs = ["\x1b[31m", "\x1b[0m", "\x1b]0;t\x07", "\x9b2J", "\x1bPq\x1b\\", "\x00"]
    for _ in range(500):
        words = ["w" + str(rng.randint(0, 99)) for _ in range(rng.randint(1, 8))]
        raw = "".join(rng.choice(seqs) + w for w in words)
        assert strip(raw) == "".join(words)


# ----------------------------------------------------------- performance
def _timed(raw: str) -> float:
    start = time.perf_counter()
    strip(raw)
    return time.perf_counter() - start


_N = STDERR_TAIL_CHARS // 2  # 32768 chars: what 64 KiB of UTF-8 C1 text decodes to


@pytest.mark.parametrize(
    "raw",
    [
        "\x1b[" + "9" * 70000,
        "\x1b]" + "a" * 70000,
        "\x1bP" + "a" * 70000,
        "\x1b[" * 30000,
        "\x1b]" * 30000,
        "\x1bP" * 30000,
        "\x1b" * 70000,
        "\x1b" + " " * 70000,
        "\x9b" * 70000,
        "\x9b" + "9" * 70000,
        "\r" * 70000,
        "\x00" * 70000,
        "\x1b]0;" + "\x1b" * 30000,
    ],
    ids=lambda s: repr(s[:6]) + f"x{len(s)}",
)
def test_pathological_7bit_and_csi_inputs_are_fast(raw: str) -> None:
    assert _timed(raw) < 1.0


@pytest.mark.parametrize(
    "intro", ["\x9d", "\x90", "\x98", "\x9e", "\x9f"], ids=lambda c: f"U+{ord(c):04X}"
)
def test_repeated_8bit_string_introducers_are_linear_not_quadratic(intro: str) -> None:
    """REDOS: ``\\x9d`` / ``\\x90`` ... are not excluded from the body class of
    their own pattern, so every introducer rescans the rest of the input:
    O(n^2). 64 KiB of the UTF-8 text ``U+009D`` * 32768 stalls the controller's
    event loop for seconds."""
    raw = intro * _N
    elapsed = _timed(raw)
    assert elapsed < 1.0, f"{len(raw)} chars took {elapsed:.2f}s (quadratic)"


@pytest.mark.parametrize(
    "unit",
    [
        "\x9dab",
        "\x90ab",
        "\x9d\x90\x98",
        "\x9d\x9d\x9c",
        "\x9b\x9d",
        "\x1b]\x9d",
        "\x90\x1bP",
    ],
    ids=lambda u: repr(u),
)
def test_mixed_8bit_introducer_floods_are_linear(unit: str) -> None:
    raw = unit * (70000 // len(unit))
    elapsed = _timed(raw)
    assert elapsed < 1.0, f"{elapsed:.2f}s"


def test_doubling_the_input_does_not_quadruple_the_time() -> None:
    small, big = "\x9dab" * 20000, "\x9dab" * 80000
    t_small = min(_timed(small) for _ in range(3))
    t_big = min(_timed(big) for _ in range(3))
    assert t_big < max(t_small * 12, 0.05), (t_small, t_big)


def test_clean_tail_of_a_hostile_utf8_c1_flood_is_fast() -> None:
    data = ("\u009d" * _N).encode()  # 64 KiB of valid UTF-8
    start = time.perf_counter()
    clean_tail(data, 1 << 20)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"clean_tail took {elapsed:.2f}s on 64 KiB"


# --------------------------------------------------------- clean_tail itself
def test_clean_tail_strips_and_returns_text() -> None:
    assert clean_tail(b"\x1b[31mboom\x1b[0m\r\n", 1000) == "boom\n"


def test_clean_tail_none_cases() -> None:
    assert clean_tail(b"", 100) is None
    assert clean_tail(b"abc", 0) is None
    assert clean_tail(b"abc", -5) is None
    assert clean_tail("\x1b[2J\x00\x07\u009d".encode(), 100) is None  # only controls
    assert clean_tail(b"\x1b[2J\x00\x07", 100) is None


def test_clean_tail_truncation_cut_inside_a_sequence_leaves_inert_remnant() -> None:
    data = b"0123456789\x1b[31;1;4mTAIL"
    for limit in range(1, len(data) + 1):
        out = clean_tail(data, limit)
        assert out is None or _is_clean(out), (limit, out)
        assert out is None or "TAIL"[-min(4, limit) :] in out or limit < 4


def test_clean_tail_cut_inside_a_multibyte_character() -> None:
    data = "ab日本語".encode()  # 3-byte chars
    for limit in range(1, len(data) + 1):
        out = clean_tail(data, limit)
        assert out is None or "�" not in out, (limit, out)


def test_clean_tail_keeps_the_end_after_stripping() -> None:
    body = ("\x1b[31mx\x1b[0m" * 40000).encode()  # lots of stripped bytes
    out = clean_tail(body, 1 << 20)
    # the byte cut may land inside a sequence: its inert remnant is plain text
    assert out is not None and set(out) <= {"x", "m", "[", "0", "3", "1"}
    assert out.endswith("x") and _is_clean(out)
    assert len(out) <= STDERR_TAIL_CHARS


def test_clean_tail_invalid_utf8_and_c1_bytes() -> None:
    out = clean_tail(b"ok\xff\xfe\x9b31m\x80\x9dend", 100)
    assert out is not None and _is_clean(out) and out.endswith("end")


def test_clean_tail_never_exceeds_the_stored_cap() -> None:
    out = clean_tail(b"y" * (STDERR_TAIL_CHARS * 3), 1 << 30)
    assert out is not None and len(out) <= STDERR_TAIL_CHARS


def test_clean_tail_respects_the_byte_limit_given() -> None:
    out = clean_tail(b"a" * 1000, 10)
    assert out == "a" * 10
