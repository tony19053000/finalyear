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
  ``DEL`` and every **C1** control (``0x80``-``0x9f``) is dropped.

Pure; linear in the input (no backtracking pattern can be made to blow up).
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["strip_terminal_controls"]

_ST: Final = r"(?:\x1b\\|\x9c)"
_ESCAPE_SEQUENCE: Final = re.compile(
    # CSI: parameter bytes, intermediate bytes, one final byte
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    # OSC: up to BEL or ST (terminated only)
    r"|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|" + _ST + ")"
    # DCS / SOS / PM / APC: up to ST (terminated only)
    r"|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\x9c]*"
    + _ST
    # any other ESC sequence: intermediates, one final byte
    + r"|\x1b[ -/]*[0-~]"
)
_DROP: Final = {
    code: None
    for code in (*range(0x20), 0x7F, *range(0x80, 0xA0))
    if code not in (0x09, 0x0A)
}


def strip_terminal_controls(text: str) -> str:
    """``text`` without terminal escape sequences or control characters,
    keeping ``\\n`` and ``\\t`` (carriage returns become ``\\n``; module
    docstring)."""
    if not text:
        return text
    text = _ESCAPE_SEQUENCE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.translate(_DROP)
