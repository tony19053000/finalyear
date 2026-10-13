"""Making a fail-closed log redactor visible (V0.7 G6 fix round 3, T-22).

When a loop guard of the secret matcher fires
(:class:`~meshforge.common.secret_redaction.RedactionStallError`), the stream
- and every stream of that attempt - emits ``*`` for every byte from then on:
nothing can leak, nothing can hang. Without a word of explanation the user
only sees stars where the job's output was, and the operator sees nothing.

:class:`FailClosedNotice` (one per redacted stream, on the agent and on the
controller) turns that into exactly two things, each once per stream:

* a **structured warning** by the caller (``first()`` is true once) - the ids
  and the stream name, never a value, a form or a byte of output;
* a **note in the log itself**, where the user is looking:
  ``[meshforge: log redaction failed closed - output withheld]``. The agent's
  stream may grow, so it appends the note as a line of its own
  (:meth:`line`). The controller's copy must keep every byte offset (live
  followers and the final log are addressed by offset), so there the note is
  written **over** the first withheld ``*`` bytes (:meth:`overlay`) - the
  length never changes.

The log was chosen over a job event or an attempt field: it needs no schema,
API or CLI change, it is what ``compute logs`` and a live follower already
show, and it survives in the stored file.
"""

from __future__ import annotations

from typing import Final

from meshforge.common.secret_redaction import StreamRedactor

__all__ = ["FAIL_CLOSED_NOTE", "FailClosedNotice"]

FAIL_CLOSED_NOTE: Final = b"[meshforge: log redaction failed closed - output withheld]"


class FailClosedNotice:
    """What one stream has already said about its redactor failing closed."""

    __slots__ = ("_unwritten", "seen")

    def __init__(self) -> None:
        self.seen = False
        """The redactor was found failed (the warning has been logged)."""
        self._unwritten: bytes = FAIL_CLOSED_NOTE

    def first(self, redactor: StreamRedactor) -> bool:
        """True exactly once: the first call after ``redactor`` failed closed."""
        if self.seen or not redactor.failed:
            return False
        self.seen = True
        return True

    @staticmethod
    def line() -> bytes:
        """The note as a line of its own (a stream that may grow: the agent)."""
        return b"\n" + FAIL_CLOSED_NOTE + b"\n"

    def overlay(self, withheld: bytes) -> bytes:
        """``withheld`` (all ``*``) with the next bytes of the note written
        over its start - same length (the controller's copies)."""
        if not self._unwritten or not withheld:
            return withheld
        n = min(len(self._unwritten), len(withheld))
        out = self._unwritten[:n] + withheld[n:]
        self._unwritten = self._unwritten[n:]
        return out
