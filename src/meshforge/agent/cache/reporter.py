"""Send this node's cache inventory to the controller (V0.5 D90/D101).

One :class:`CacheReporter` task per agent process:

* after **every** stream's Reconcile (:meth:`request_snapshot`, wired to the
  session client's ``after_reconcile_hooks``) it sends a paged **snapshot** -
  ``snapshot=true`` on every page, ``snapshot_last`` on the final one, at most
  ``page_items`` (<= 1024) items per page;
* then **deltas** of what changed, at most once per ``flush_s`` (sooner when a
  page's worth is pending), paged the same way;
* a full snapshot again every ``snapshot_interval_s`` (self-healing: the
  controller marks a node stale after a gap and waits for one);
* every ``rescan_s`` it re-lists built environments and env images (those change
  rarely and have no observer).

``seq`` is monotonic for the whole process (D101), never reset by a reconnect.
Reports go through the session's reliable path (``send_report``: ring, replay,
ack). Nothing is sent before the first snapshot of the process; a periodic
snapshot is skipped while the node is offline (the next Hello brings one).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final

from meshforge.agent.cache.inventory import CacheInventory, Item
from meshforge.agent.settings import CacheSettings
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = ["CacheReporter", "to_proto"]

log = get_logger(__name__)

_KINDS: Final = {
    "blob": agent_pb2.CacheItem.KIND_BLOB,
    "tree": agent_pb2.CacheItem.KIND_TREE,
    "env": agent_pb2.CacheItem.KIND_ENV,
    "image": agent_pb2.CacheItem.KIND_IMAGE,
}

type SendReport = Callable[[str, Any], Awaitable[str]]


def to_proto(items: Sequence[Item]) -> list[agent_pb2.CacheItem]:
    return [
        agent_pb2.CacheItem(kind=_KINDS[i.kind], key=i.key, size=max(0, i.size))
        for i in items
    ]


class CacheReporter:
    """See module docstring. ``start()`` then ``close()``."""

    def __init__(
        self,
        inventory: CacheInventory,
        send_report: SendReport,
        settings: CacheSettings,
        *,
        node_id: str,
        online: Callable[[], bool],
        scan_envs: Callable[[], dict[str, int]] | None = None,
        list_images: Callable[[], dict[str, int]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._inv = inventory
        self._send = send_report
        self._cfg = settings
        self._node_id = node_id
        self._online = online
        self._scan_envs = scan_envs
        self._list_images = list_images
        self._clock = clock
        self._seq = 0
        self._snapshot_wanted = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.snapshots_sent = 0
        self.deltas_sent = 0
        self.pages_sent = 0
        self._snapshotted = False

    @property
    def seq(self) -> int:
        return self._seq

    def _now(self) -> float:
        return (
            self._clock()
            if self._clock is not None
            else asyncio.get_running_loop().time()
        )

    # ------------------------------------------------------------ control

    def request_snapshot(self) -> None:
        """Called after every Reconcile (sync hook)."""
        self._snapshot_wanted.set()
        self._wake.set()

    async def start(self) -> None:
        if not self._inv.loaded:
            await asyncio.to_thread(self._inv.load)
        await self._rescan()
        self._task = asyncio.create_task(
            self._run(), name=f"agent-cache:{self._node_id}"
        )
        self._task.add_done_callback(self._on_done)

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        with contextlib.suppress(OSError):
            await asyncio.to_thread(self._inv.save_trees)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "agent.cache.reporter_failed",
                node_id=self._node_id,
                cause=type(exc).__name__,
                error=str(exc)[:200],
            )

    # --------------------------------------------------------------- loop

    async def _run(self) -> None:
        now = self._now()
        next_snapshot = now + self._cfg.snapshot_interval_s
        next_rescan = now + self._cfg.rescan_s
        while True:
            # Polled every flush_s: the CAS observer runs on worker threads and
            # must not touch asyncio objects, so pending changes are looked for.
            timeout = max(
                0.0,
                min(next_snapshot, next_rescan, self._now() + self._cfg.flush_s)
                - self._now(),
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout)
            self._wake.clear()
            now = self._now()
            if now >= next_rescan:
                await self._rescan()
                next_rescan = now + self._cfg.rescan_s
            if now >= next_snapshot:
                next_snapshot = now + self._cfg.snapshot_interval_s
                if self._online() and self._snapshotted:
                    self._snapshot_wanted.set()
            if self._snapshot_wanted.is_set():
                self._snapshot_wanted.clear()
                await self.send_snapshot()
                next_snapshot = self._now() + self._cfg.snapshot_interval_s
            elif self._snapshotted and self._inv.pending_count():
                await self.send_delta()
            await asyncio.to_thread(self._inv.save_trees)

    async def _rescan(self) -> None:
        if self._scan_envs is not None:
            try:
                self._inv.set_envs(await asyncio.to_thread(self._scan_envs))
            except Exception as exc:  # noqa: BLE001 - a broken scan must not stop it
                log.warning("agent.cache.env_scan_failed", cause=type(exc).__name__)
        if self._list_images is not None:
            try:
                self._inv.set_images(await asyncio.to_thread(self._list_images))
            except Exception as exc:  # noqa: BLE001 - Docker may be gone; keep the list
                log.warning("agent.cache.image_scan_failed", cause=type(exc).__name__)

    # ------------------------------------------------------------ sending

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def send_snapshot(self) -> int:
        """Send the whole inventory as pages; returns the page count.

        The CAS is re-listed first, off the event loop, so a blob deleted
        behind the cache (and every tree it completed) leaves the index at the
        next snapshot (G4 fix round 1)."""
        await asyncio.to_thread(self._inv.reconcile_disk)
        # Built in a thread (tens of thousands of items), and the loop is
        # yielded after every page: a big snapshot right after a Hello never
        # starves the session's heartbeats (G4 fix round 2).
        items = await asyncio.to_thread(self._inv.snapshot)
        size = self._cfg.page_items
        pages = [items[i : i + size] for i in range(0, len(items), size)] or [[]]
        for index, page in enumerate(pages):
            await self._send(
                "cache_report",
                agent_pb2.CacheReport(
                    seq=self._next_seq(),
                    snapshot=True,
                    snapshot_last=index == len(pages) - 1,
                    page=index,
                    added=to_proto(page),
                ),
            )
            await asyncio.sleep(0)
        self._snapshotted = True
        self.snapshots_sent += 1
        self.pages_sent += len(pages)
        log.info(
            "agent.cache.snapshot_sent",
            node_id=self._node_id,
            items=len(items),
            pages=len(pages),
            seq=self._seq,
            **self._inv.counts(),
        )
        return len(pages)

    async def send_delta(self) -> int:
        """Send what changed since the last report; returns messages sent."""
        added, removed = self._inv.take_delta()
        size = self._cfg.page_items
        sent = 0
        while added or removed:
            page_added = added[:size]
            page_removed = removed[: size - len(page_added)]
            added = added[len(page_added) :]
            removed = removed[len(page_removed) :]
            await self._send(
                "cache_report",
                agent_pb2.CacheReport(
                    seq=self._next_seq(),
                    added=to_proto(page_added),
                    removed=to_proto(page_removed),
                ),
            )
            sent += 1
            await asyncio.sleep(0)
        self.deltas_sent += sent
        return sent
