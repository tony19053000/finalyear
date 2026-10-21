"""Make untrusted program output safe to store and print (V0.10 TD1, D287).

A job's stderr is written by the job - arbitrary code. Kept as-is, a stored
tail could carry terminal control sequences that act when an operator later
prints it (a CLI view, ``jq -r``, ``less -R``, a CI log): clear the screen,
move the cursor to overwrite earlier lines, set the window title, write to the
clipboard (OSC 52), hide text, or spoof another line with a bare carriage
return. :func:`strip_terminal_controls` removes all of that and keeps the
readable text:

* escape **sequences** are removed whole (the parameters with the introducer):
  CSI (``ESC [`` / 8-bit ``0x9b`` ... final byte - colours, cursor moves,
  erase), OSC (``ESC ]`` / ``0x9d`` ... ``BEL`` or ``ST`` - titles,
  hyperlinks, clipboard), DCS / SOS / PM / APC (``ESC P``/``X``/``^``/``_``
  ... ``ST``) and the short ``ESC`` + byte forms. An unterminated string
  sequence is not guessed at: only its introducer is dropped and the rest
  stays as plain text - no output text is ever hidden;
* a carriage return becomes a line break (``\\r\\n`` -> ``\\n``; a lone
  ``\\r``, as progress bars write, -> ``\\n``), so a later line cannot
  overprint an earlier one and progress updates stay readable;
* every other **C0** control (``0x00``-``0x1f`` except ``\\t`` and ``\\n``),
  ``DEL`` and every **C1** control (``0x80``-``0x9f``) is dropped;
* the invisible **spoofing** characters are dropped too (V0.10 TD2, D-1):
  bidi embeddings / overrides / isolates / marks (``U+202A``-``U+202E``,
  ``U+2066``-``U+2069``, ``U+200E``, ``U+200F``, ``U+061C``) - which make a
  terminal or browser show text in a different order than it is stored
  (``"harmless\\u202etxt.exe"`` displays as ``harmlessexe.txt``) - and the
  zero-width characters (``U+200B``-``U+200D``, ``U+2060``, ``U+FEFF``),
  which hide text inside a word that looks like another. **Trade-off:** the
  zero-width joiner inside an emoji sequence goes too (a family emoji shows
  as its separate people) and so does the zero-width non-joiner some scripts
  (Persian, Indic) use for shaping; the text stays readable, it only
  renders differently. A stored tail is evidence for an operator, so an
  unambiguous display wins over the exact glyph.

Pure; linear in the input: the escape pattern's bodies stop at every
introducer (no backtracking pattern can be made to blow up) and the character
drops are one ``str.translate``.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["strip_terminal_controls"]

_ST: Final = r"(?:\x1b\\|\x9c)"
_ESCAPE_SEQUENCE: Final = re.compile(
    # CSI: parameter bytes, intermediate bytes, one final byte
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    # OSC: up to BEL or ST (terminated only). The string bodies stop at ESC
    # and at every C1 byte - which covers ST and all 8-bit introducers - so a
    # run of unterminated introducers cannot make each one rescan to the end
    # of the text (that was quadratic; TD1 review B1).
    r"|(?:\x1b\]|\x9d)[^\x07\x1b\x80-\x9f]*(?:\x07|" + _ST + ")"
    # DCS / SOS / PM / APC: up to ST (terminated only)
    r"|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\x80-\x9f]*"
    + _ST
    # any other ESC sequence: intermediates, one final byte
    + r"|\x1b[ -/]*[0-~]"
)
_BIDI_CONTROLS: Final = (
    *range(0x202A, 0x202F),  # LRE RLE PDF LRO RLO
    *range(0x2066, 0x206A),  # LRI RLI FSI PDI
    0x200E,  # LRM
    0x200F,  # RLM
    0x061C,  # ALM
)
_ZERO_WIDTH: Final = (
    *range(0x200B, 0x200E),  # ZWSP ZWNJ ZWJ
    0x2060,  # WORD JOINER
    0xFEFF,  # ZWNBSP / BOM
)
_DROP: Final = {
    point: None
    for point in (
        *range(0x20),
        0x7F,
        *range(0x80, 0xA0),
        *_BIDI_CONTROLS,
        *_ZERO_WIDTH,
    )
    if point not in (0x09, 0x0A)
}


def strip_terminal_controls(text: str) -> str:
    """``text`` without terminal escape sequences, control characters or
    bidi / zero-width spoofing characters, keeping ``\\n`` and ``\\t``
    (carriage returns become ``\\n``; module docstring)."""
    if not text:
        return text
    text = _ESCAPE_SEQUENCE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.translate(_DROP)
