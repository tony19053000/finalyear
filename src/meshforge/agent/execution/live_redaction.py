"""Live log redaction off the agent's event loop (V0.9 G1, D224; RE-D2 residual).

A job's own output is redacted (its secret values masked) before it is written,
kept as evidence or sent (V0.7 G6, D144). The matcher's cost depends on the
values: a job owner who builds pathological secrets can make one 64 KiB chunk
cost about 0.4 s (THREAT_MODEL RE-D2). Until V0.9 that ran **on the agent's
event loop** - heartbeats, pongs (now the clock-offset exchange) and every
other attempt's reports waited behind it, which would also skew benchmark
timings. :class:`LiveRedaction` moves it to **one worker thread per attempt**:

* **order** - one thread, so every chunk of every stream is redacted in the
  order it was fed (one :class:`StreamRedactor` per stream keeps its own
  carry-over), and the shared :class:`SecretMatcher` - lazily extended while it
  scans - is never used by two threads at once;
* **bounded** - each :meth:`feed` is awaited by the stream's pump before it
  reads the next chunk, so at most one chunk per stream (plus the final
  flush) is ever queued; a slow redaction back-pressures the workload's pipe
  instead of growing memory;
* **T-14 ordering** (V0.7 G6: work in a thread must not outlive the caller's
  cleanup) - every call goes through :func:`~meshforge.agent.threads.
  uninterrupted`: a cancel (stop, timeout, agent shutdown) waits for the chunk
  in the thread to finish, then re-raises. So when the attempt's cleanup wipes
  the secrets and drops the matcher (``ACTIVE.remove``), no redaction is still
  running with them, and :meth:`flush` (the drain of the held tails) always
  comes after the last feed.

The output is byte-for-byte what the on-loop redaction produced.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor

from meshforge.agent.threads import uninterrupted
from meshforge.common.secret_redaction import SecretMatcher, StreamRedactor

__all__ = ["LiveRedaction"]


class LiveRedaction[S]:
    """The redactors of one attempt's streams, run in one worker thread."""

    def __init__(
        self,
        matcher: SecretMatcher,
        streams: tuple[S, ...],
        *,
        redactor: Callable[[SecretMatcher], StreamRedactor] = StreamRedactor,
        name: str = "redact",
    ) -> None:
        self.redactors: Mapping[S, StreamRedactor] = {
            stream: redactor(matcher) for stream in streams
        }
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._closed = False

    async def _call(self, fn: Callable[[], bytes]) -> bytes:
        if self._closed:
            raise RuntimeError("the redaction worker is closed")
        loop = asyncio.get_running_loop()
        return await uninterrupted(loop.run_in_executor(self._pool, fn))

    async def feed(self, stream: S, data: bytes) -> bytes:
        """``data`` of ``stream`` redacted (some bytes may be held back for
        the next chunk - a value split across chunks is still found)."""
        redactor = self.redactors[stream]
        return await self._call(lambda: redactor.feed(data))

    async def flush(self, stream: S) -> bytes:
        """The held tail of ``stream``, at the end of the run (after every feed)."""
        redactor = self.redactors[stream]
        return await self._call(redactor.flush)

    def close(self) -> None:
        """Release the thread. Every call was awaited to completion
        (uninterrupted), so nothing is running in it any more."""
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
