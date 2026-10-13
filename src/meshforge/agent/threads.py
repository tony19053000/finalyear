"""Worker-thread calls whose effects must not outlive their caller's cleanup.

``await asyncio.to_thread(f)`` can be cancelled, but the thread cannot: ``f``
runs on, and whatever it creates *after* the caller's cleanup ran stays behind
- a secret folder (V0.7 G6 T-14), a created container (``4a79155``), a staging
folder, a cgroup. Two rules cover every such call in the agent:

* the cleanup works **by path / by name** known before the call, never from
  the call's result (a cancelled ``await`` never delivers it);
* the cleanup runs **after the thread has ended** - :func:`uninterrupted`
  (short calls: wait, then re-raise the cancel) or :func:`settled` (wait for a
  future the caller kept).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

__all__ = ["settled", "uninterrupted"]


async def uninterrupted[T](work: Awaitable[T]) -> T:
    """Await ``work`` to completion even if cancelled meanwhile, then re-raise.

    For a short call in a thread that a cancellation cannot stop: returning
    early would let the caller clean up while the thread still writes.
    """
    future = asyncio.ensure_future(work)
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not future.cancelled():
            future.exception()  # retrieved on purpose: the stop wins
        raise asyncio.CancelledError
    return future.result()


async def settled(work: asyncio.Future[Any]) -> None:
    """Wait until ``work`` has ended, whatever its outcome (never raises it)."""
    await asyncio.wait([work])
    if not work.cancelled():
        work.exception()  # retrieved: its awaiter reported it, or a stop won
