"""Small asyncio helpers shared by the controller and the agent.

:func:`run_to_completion` - cleanup that a cancellation must not cut short.
A ``finally: await resource.close()`` is interrupted by a cancel that lands
*while* it is awaiting: the close stops half-way and the resource leaks. Seen
in V1.0 G8 F6: an agent stopped while its mDNS browse was closing left the
zeroconf UDP sockets (port 5353, one per interface) open, reported as
``ResourceWarning: unclosed transport`` in a later test of the same worker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

__all__ = ["finished_within", "run_to_completion"]


async def run_to_completion[T](aw: Awaitable[T]) -> T:
    """Await ``aw`` to its end even if the caller is cancelled meanwhile.

    The work runs in its own task behind :func:`asyncio.shield`; a cancel of
    the caller is remembered, the wait goes on until the task is done, and
    then :class:`asyncio.CancelledError` is raised so the caller still sees
    its cancellation. ``aw``'s own exception (or its own cancellation, e.g. at
    loop shutdown) propagates unchanged. ``aw`` must be bounded: it is never
    abandoned.
    """
    task = asyncio.ensure_future(aw)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            interrupted = True
        except BaseException:  # noqa: BLE001, S110 - re-raised by task.result() below
            pass
    if interrupted:
        if task.exception() is not None:
            task.result()  # the work failed too: its error wins, as in a finally
        raise asyncio.CancelledError
    return task.result()


async def finished_within(aw: Awaitable[object], timeout_s: float) -> bool:
    """Await ``aw`` for at most ``timeout_s``; False (``aw`` cancelled) when it
    did not finish in time. The bound a caller puts on cleanup it hands to
    :func:`run_to_completion` - which never abandons its work - so a wedged
    close cannot hold a stop forever; the caller logs the False."""
    try:
        async with asyncio.timeout(timeout_s):
            await aw
    except TimeoutError:
        return False
    return True
