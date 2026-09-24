"""Time and the operator, injectable so the unit tests never sleep.

* **Monotonic** time (``time.monotonic``) measures every latency and every
  duration. **Wall** time (UTC) only labels events for humans and for matching
  against the controller's ``ended_at`` timestamps.
* The operator marks moments by pressing Enter ("cable pulled now"). A daemon
  thread reads stdin and stamps each line with the monotonic clock **when it is
  read**, so the main loop can keep polling at 100 ms while waiting.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, TextIO


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def wall(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


def iso(moment: datetime) -> str:
    """UTC, millisecond precision, explicit offset (like the controller's JSON)."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


class Operator(Protocol):
    def prompt(self, text: str) -> None:
        """Show ``text`` and forget any Enter pressed before it."""

    def poll_mark(self) -> float | None:
        """The monotonic time of the next Enter since the prompt, or ``None``."""

    def say(self, text: str) -> None: ...


@dataclass
class ConsoleOperator:
    """Real operator: prompts on stderr, Enter presses read by a daemon thread."""

    clock: Clock
    stream: TextIO = field(default_factory=lambda: sys.stdin)
    out: TextIO = field(default_factory=lambda: sys.stderr)
    _marks: queue.Queue[float] = field(default_factory=queue.Queue)
    _thread: threading.Thread | None = None

    def _start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._read, name="operator-enter", daemon=True
            )
            self._thread.start()

    def _read(self) -> None:
        for _line in self.stream:  # one Enter = one line; EOF ends the thread
            self._marks.put(self.clock.monotonic())

    def prompt(self, text: str) -> None:
        self._start()
        while not self._marks.empty():
            self._marks.get_nowait()
        self.say(f"\n>>> {text}")

    def poll_mark(self) -> float | None:
        try:
            return self._marks.get_nowait()
        except queue.Empty:
            return None

    def say(self, text: str) -> None:
        self.out.write(text + "\n")
        self.out.flush()
