"""The agent's GC task (V0.5 G5, D91): periodic passes, on demand, pins.

* A pass runs at start (after the index is reconciled with the disk), every
  ``[agent.gc] interval_s``, and whenever :meth:`GcService.request` is called -
  a controller ``GcRequest{run_now}`` or a staging that left the CAS over its
  quota. Requests during a pass coalesce into one more pass.
* ``GcRequest{set_pins, pinned_trees}`` replaces the pinned trees (pinned named
  inputs, D92). A pinned tree whose manifest is cached gets its members recorded
  so the pin protects every member blob.
* After a pass that removed environments the ``on_envs_changed`` hook runs (the
  cache reporter re-lists envs, so the controller learns at once); removed blobs
  reach the reporter through the CAS observer.

One pass at a time, in a worker thread; the task is kept and its failure is
logged (checklist 5).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import re
from collections.abc import Awaitable, Callable, Iterable
from typing import Final

from meshforge.agent.gc.collector import GarbageCollector, GcReport
from meshforge.agent.gc.index import GcIndex
from meshforge.agent.gc.leases import AttemptLeases
from meshforge.common.cas import CasStore
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.tree import TreeError, TreeLimits, parse_tree

__all__ = ["MAX_PINS", "GcService"]

log = get_logger(__name__)

MAX_PINS: Final = 4096
_DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")


class GcService:
    """See module docstring."""

    def __init__(
        self,
        collector: GarbageCollector,
        *,
        interval_s: float,
        node_id: str = "",
        on_envs_changed: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.collector = collector
        self.index: GcIndex = collector.index
        self._interval_s = interval_s
        self._node_id = node_id
        self.on_envs_changed = on_envs_changed
        self._wake = asyncio.Event()
        self._reasons: list[str] = []
        self._reconcile = True
        """The next pass scans the disk (start, periodic, a controller request);
        a pass asked for after a staging trusts the index."""
        self._pin_tasks: set[asyncio.Task[int]] = set()
        self._task: asyncio.Task[None] | None = None
        self.passes = 0
        self.last: GcReport | None = None
        self._idle = asyncio.Event()

    @property
    def store(self) -> CasStore:
        return self.collector.store

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await asyncio.to_thread(self.index.open)
        self.request("start", reconcile=True)
        self._task = asyncio.create_task(self._run(), name="agent-gc")
        self._task.add_done_callback(self._on_done)

    async def close(self) -> None:
        for pin_task in list(self._pin_tasks):
            pin_task.cancel()
        await asyncio.gather(*self._pin_tasks, return_exceptions=True)
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await asyncio.to_thread(self.index.close)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            log.error(
                "agent.gc.task_failed",
                node_id=self._node_id,
                cause=type(exc).__name__,
                error=str(exc)[:200],
            )

    def on_cas_change(self, added: bool, digest: Digest, size: int) -> None:
        """CAS observer (worker threads): keep the blob rows current."""
        try:
            if added:
                self.index.upsert("blob", digest.hex, size)
            else:
                self.index.remove("blob", digest.hex)
        except RuntimeError:
            pass  # index closed (shutdown): the next start reconciles the disk

    def attempt_leases(self, attempt_id: str) -> AttemptLeases:
        return AttemptLeases(self.index, attempt_id)

    # --------------------------------------------------------------- passes

    def request(self, reason: str, *, reconcile: bool = False) -> None:
        """Run a pass soon (coalesced)."""
        self._reasons.append(reason)
        self._reconcile = self._reconcile or reconcile
        self._idle.clear()
        self._wake.set()

    async def wait_idle(self) -> None:
        """Until no pass is running or requested (tests, shutdown)."""
        await self._idle.wait()

    async def run_now(self, reason: str = "manual") -> GcReport:
        """One pass, awaited (tests and the controller's GcRequest path use
        :meth:`request`; this is for callers that need the report)."""
        report = await asyncio.to_thread(self.collector.run_pass, reason)
        await self._after(report)
        return report

    async def _run(self) -> None:
        while True:
            woken = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._interval_s)
                woken = True
            self._wake.clear()
            reason = ",".join(dict.fromkeys(self._reasons)) or "periodic"
            self._reasons.clear()
            reconcile = self._reconcile or not woken
            self._reconcile = False
            try:
                report = await asyncio.to_thread(
                    functools.partial(
                        self.collector.run_pass, reason, reconcile=reconcile
                    )
                )
            except Exception as exc:  # noqa: BLE001 - a failed pass is retried later
                log.warning(
                    "agent.gc.pass_failed",
                    node_id=self._node_id,
                    reason=reason,
                    cause=type(exc).__name__,
                    error=str(exc)[:200],
                )
            else:
                await self._after(report)
            if not self._wake.is_set():
                self._idle.set()

    async def _after(self, report: GcReport) -> None:
        self.passes += 1
        self.last = report
        if report.evicted.get("env") and self.on_envs_changed is not None:
            try:
                await self.on_envs_changed()
            except Exception as exc:  # noqa: BLE001 - reporting is best effort
                log.info("agent.gc.envs_hook_failed", cause=type(exc).__name__)

    # ------------------------------------------------------------- commands

    def handle(self, request: agent_pb2.GcRequest) -> None:
        """A controller ``GcRequest`` (synchronous, before its Ack): pins are
        replaced in a kept task, a pass is requested."""
        if request.set_pins:
            task = asyncio.create_task(
                self.set_pins(list(request.pinned_trees)), name="agent-gc-pins"
            )
            self._pin_tasks.add(task)
            task.add_done_callback(self._pin_done)
        if request.run_now:
            self.request("controller", reconcile=True)

    def _pin_done(self, task: asyncio.Task[int]) -> None:
        self._pin_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning(
                "agent.gc.pins_failed",
                node_id=self._node_id,
                cause=type(task.exception()).__name__,
            )

    # ----------------------------------------------------------------- pins

    async def set_pins(self, trees: Iterable[str]) -> int:
        """Replace the pinned trees (``sha256:<hex>`` each; malformed refused
        as a whole). Returns how many are pinned now."""
        listed = list(trees)
        if len(listed) > MAX_PINS or not all(_DIGEST_RE.fullmatch(t) for t in listed):
            log.warning(
                "agent.gc.pins_refused",
                node_id=self._node_id,
                count=len(listed),
                reason="too_many_or_malformed",
            )
            return -1
        hexes = {t.removeprefix("sha256:") for t in listed}
        pinned, unpinned = await asyncio.to_thread(self.index.set_pins, "tree", hexes)
        for tree_hex in hexes:
            await asyncio.to_thread(self._record_cached_tree, tree_hex)
        log.info(
            "agent.gc.pins_set",
            node_id=self._node_id,
            pinned=len(hexes),
            added=pinned,
            removed=unpinned,
        )
        return len(hexes)

    def _record_cached_tree(self, tree_hex: str) -> None:
        if self.index.tree_known(tree_hex):
            return
        digest = Digest.parse(f"sha256:{tree_hex}")
        try:
            data = self.store.path_for(digest).read_bytes()
            tree = parse_tree(data, expected=digest, limits=TreeLimits())
        except (OSError, TreeError):
            return  # not cached (or not a valid manifest): nothing to protect
        self.index.record_tree(tree_hex, {e.sha256 for e in tree.files()})
