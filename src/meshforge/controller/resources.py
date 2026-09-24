"""Live resource state and the 1-minute downsampler (roadmap 0.3.11, brief D11).

Two levels, as ARCHITECTURE §19 puts it ("2 s live -> 1 min history"):

* **Live** - :class:`LiveRing`, one bounded deque per *node* (not per session, so a
  reconnect does not lose the last five minutes). It is what the operator surface
  (G5) and the planner (V0.6+) read; nothing about it touches the database.
* **History** - :class:`Downsampler` accumulates each metric into wall-clock buckets
  of ``bucket_s`` (default 60 s) and hands whole, *closed* buckets to the flusher,
  which upserts them into ``resource_snapshot``. An open bucket is never written, so
  a row is complete the moment it exists.

**Late samples (D26, LATE-1).** A bucket *ends* at ``bucket_start + bucket_s`` but
is *closed* only ``bucket_grace_s`` later (default 20 s), because reports reach the
controller late for ordinary reasons: link latency, and above all the replay after
a reconnect, which delivers samples observed before the drop seconds after it. A
sample that arrives after its bucket was closed anyway is **not** merged into the
stored row (after a controller restart, when the dedupe cursor is gone, a replay
would be merged twice); it stays in the live ring, is counted
(:attr:`Downsampler.dropped_late`) and logged ``controller.resources.sample_late``
with the node, the bucket and how late it was - never dropped silently. Buckets in
their grace period do not count against ``max_open_buckets`` (that cap is for clock
jumps), and the shutdown flush closes every *ended* bucket without waiting for the
grace, because no session is left to deliver into them.

:class:`ResourceTracker` is the one object the session manager talks to, through the
:class:`ResourceSink` protocol - ``manager.py`` must not grow a downsampler.

**Everything here is bounded.** The number of tracked nodes, the ring per node and
the open buckets per node all have caps; passing one evicts the oldest entry, bumps
a counter and logs once. A node whose clock jumps forward by a year therefore costs
a few buckets, not the controller's memory.

**Duplicates.** After a reconnect the agent replays un-acked reports, and the new
session has a fresh ``message_id`` dedupe window, so the same ``ResourceUpdate`` can
arrive twice. The dedupe key is ``ResourceUpdate.sequence`` **alone**: it is the one
counter that is monotonic over the whole thing being deduped, the agent *process*
(``agent/session/client.py``: "reports, per process, never reset"). ``session_epoch``
is deliberately *not* part of the key - it is bumped per connection *attempt*, so a
report first sent under epoch N and replayed after a reconnect arrives under epoch
N+1 with the same sequence and would compare as new.

Because the sequence restarts at 1 when the agent *process* restarts, the cursor is
reset from information already on the wire, through :meth:`ResourceSink.begin_session`:

* a session opens -> the manager calls ``begin_session(node_id, 0)``: assume a fresh
  process, i.e. *arm* a reset that the first report of the session applies;
* the session's ``Reconcile`` (sent only on a reconnect, before any replayed report)
  carries ``replay_from_sequence``; ``> 0`` means this is the same process replaying
  buffered reports, so the arming is cancelled and the cursor survives - the replayed
  duplicates are rejected. ``0`` (nothing buffered) leaves the reset armed, which is
  harmless: the next report's sequence is higher than the cursor anyway.

``session_epoch`` still has one job here: it *fences*. A report arriving under an
epoch lower than the highest this node has been seen with belongs to a superseded
(zombie) stream and is dropped, so a stale session cannot move the cursor.

**Cursors are bounded separately from rings.** The expensive per-node state is the
live ring (``live_ring_size`` samples); the dedupe state is three fields. They used
to share one LRU, which meant a node whose reconnect was in flight could lose its
cursor to *ring* pressure from other nodes and then accept its own replay twice
(the double-count this module exists to prevent). So ``max_tracked_nodes`` bounds
the rings and ``max_cursor_nodes`` - much larger - bounds the cursors. Evicting a
cursor is the only moment a duplicate can slip through, so it is counted
(``evicted_cursors``) and logged at warning level.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.caps import MAX_DISKS, MAX_GPUS

__all__ = [
    "DEFAULT_BUCKET_GRACE_S",
    "DEFAULT_BUCKET_S",
    "DEFAULT_LIVE_RING_SIZE",
    "DEFAULT_MAX_CURSOR_NODES",
    "DEFAULT_MAX_OPEN_BUCKETS",
    "DEFAULT_MAX_TRACKED_NODES",
    "DiskSample",
    "Downsampler",
    "GpuSample",
    "LiveRing",
    "LiveSample",
    "ResourceSink",
    "ResourceTracker",
    "SnapshotRow",
    "bucket_start",
    "live_sample",
]

log = get_logger(__name__)

DEFAULT_LIVE_RING_SIZE: Final = 150
"""5 minutes at one sample per 2 s (``controller.resources.live_ring_size``)."""

DEFAULT_BUCKET_S: Final = 60.0

DEFAULT_BUCKET_GRACE_S: Final = 20.0
"""How long after its end a bucket still accepts samples (``controller.resources.
bucket_grace_s``, D26).

Sized to the reconnect + replay window of a blip the fabric treats as "not gone"
(ARCHITECTURE §12.3): a stream lost just after a sample is observed can stay
silent for up to one lease (6 s at the shipped 2 s heartbeat) before the node is
declared OFFLINE, a restored link must be ONLINE again within 10 s (V0.3 criterion
2, which the 5 s backoff cap of D27 guarantees for the sleep part), and the replay
itself follows the Reconcile at once (a few hundred ms for a few minutes of
buffered 2 s reports). 6 + 10 + ~1 s rounds up to 20 s. Longer outages are real
outages: their samples arrive late and are counted and logged, not merged.
"""
DEFAULT_MAX_TRACKED_NODES: Final = 256
DEFAULT_MAX_OPEN_BUCKETS: Final = 8
"""Open buckets kept per node; more than a couple means a clock that jumped."""

DEFAULT_MAX_CURSOR_NODES: Final = 16_384
"""Dedupe cursors kept (three fields each), independently of the live rings.

Deliberately far above ``DEFAULT_MAX_TRACKED_NODES``: dropping a cursor is what
lets a replayed sample be counted twice, while the memory it costs is negligible.
"""


@dataclass(slots=True)
class _Cursor:
    """One node's dedupe state: what to reject, and from which stream.

    Kept out of :class:`LiveRing` so ring pressure can never delete it (see the
    module docstring).
    """

    sequence: int | None = None
    """The newest report ``sequence`` recorded (per agent *process*)."""
    epoch: int | None = None
    """The highest ``session_epoch`` seen; older ones are fenced out."""
    reset_armed: bool = False
    """The next report clears ``sequence`` (the agent process may be new)."""


@dataclass(frozen=True, slots=True)
class GpuSample:
    index: int
    util_percent: float
    vram_used_bytes: int
    vram_total_bytes: int


@dataclass(frozen=True, slots=True)
class DiskSample:
    mount: str
    free_bytes: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class LiveSample:
    """One ``ResourceUpdate`` as the controller keeps it in memory."""

    session_epoch: int
    sequence: int
    observed_at: dt.datetime
    """The agent's wall clock; what the history buckets are aligned on."""
    received_at: float
    """The controller's **monotonic** clock, for "how stale is this?"."""
    cpu_percent: float = 0.0
    ram_used_bytes: int = 0
    ram_total_bytes: int = 0
    gpus: tuple[GpuSample, ...] = ()
    disks: tuple[DiskSample, ...] = ()


def live_sample(
    update: agent_pb2.ResourceUpdate,
    *,
    session_epoch: int,
    received_at: float,
    wall_clock: Callable[[], dt.datetime] | None = None,
) -> LiveSample:
    """Convert a (already bounds-checked) ``ResourceUpdate`` into a :class:`LiveSample`.

    An agent that sent no ``observed_at`` gets the controller's receive time, so the
    sample still lands in a sensible bucket instead of 1970.
    """
    if update.HasField("observed_at"):
        observed = update.observed_at.ToDatetime(tzinfo=dt.UTC)
    else:
        observed = (wall_clock or _utcnow)()
    return LiveSample(
        session_epoch=session_epoch,
        sequence=update.sequence,
        observed_at=observed,
        received_at=received_at,
        cpu_percent=update.cpu_percent,
        ram_used_bytes=update.ram_used_bytes,
        ram_total_bytes=update.ram_total_bytes,
        gpus=tuple(
            GpuSample(
                index=gpu.index,
                util_percent=gpu.util_percent,
                vram_used_bytes=gpu.vram_used_bytes,
                vram_total_bytes=gpu.vram_total_bytes,
            )
            for gpu in update.gpus
        ),
        disks=tuple(
            DiskSample(
                mount=disk.mount,
                free_bytes=disk.free_bytes,
                total_bytes=disk.total_bytes,
            )
            for disk in update.disks
        ),
    )


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def bucket_start(moment: dt.datetime, bucket_s: float) -> dt.datetime:
    """Start of the ``bucket_s``-long bucket containing ``moment`` (UTC, floor).

    A sample exactly on a boundary belongs to the **new** bucket (the interval is
    half-open, ``[start, start + bucket_s)``).
    """
    if bucket_s <= 0:
        raise ValueError("bucket_s must be > 0")
    moment = moment.astimezone(dt.UTC)
    epoch_s = moment.timestamp()
    return dt.datetime.fromtimestamp(math.floor(epoch_s / bucket_s) * bucket_s, dt.UTC)


class LiveRing:
    """Bounded, newest-last history of one node's samples."""

    __slots__ = ("_samples", "capacity")

    def __init__(self, capacity: int = DEFAULT_LIVE_RING_SIZE) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._samples: deque[LiveSample] = deque(maxlen=capacity)

    def append(self, sample: LiveSample) -> None:
        self._samples.append(sample)

    @property
    def latest(self) -> LiveSample | None:
        return self._samples[-1] if self._samples else None

    def snapshot(self) -> list[LiveSample]:
        """A copy, oldest first (safe to iterate while new samples arrive)."""
        return list(self._samples)

    def __len__(self) -> int:
        return len(self._samples)


@dataclass(slots=True)
class _Accumulator:
    """Running mean/max of one bucket for one node."""

    samples: int = 0
    cpu_sum: float = 0.0
    cpu_max: float = 0.0
    ram_used_sum: float = 0.0
    ram_used_max: int = 0
    ram_total: int = 0
    gpus: dict[int, dict[str, float]] = field(default_factory=dict)
    disks: dict[str, dict[str, float]] = field(default_factory=dict)
    dropped_keys: int = 0
    """GPU indices / disk mounts refused because the bucket already holds the cap.

    D12 bounds a *single* ``ResourceUpdate`` at ``MAX_GPUS``/``MAX_DISKS``
    (:mod:`meshforge.controller.caps`), but a
    bucket accumulates every sample in it, so an agent varying the mount string
    each sample would otherwise write hundreds of JSONB keys into one row (NB-4).
    The first ``MAX_*`` keys of a bucket win; the rest are counted here and by
    :attr:`Downsampler.dropped_keys`.
    """

    def add(self, sample: LiveSample, *, max_gpus: int, max_disks: int) -> None:
        self.samples += 1
        self.cpu_sum += sample.cpu_percent
        self.cpu_max = max(self.cpu_max, sample.cpu_percent)
        self.ram_used_sum += sample.ram_used_bytes
        self.ram_used_max = max(self.ram_used_max, sample.ram_used_bytes)
        if sample.ram_total_bytes:
            self.ram_total = sample.ram_total_bytes
        for gpu in sample.gpus:
            if gpu.index not in self.gpus and len(self.gpus) >= max_gpus:
                self.dropped_keys += 1
                continue
            entry = self.gpus.setdefault(
                gpu.index,
                {
                    "n": 0.0,
                    "util_sum": 0.0,
                    "util_max": 0.0,
                    "vram_used_sum": 0.0,
                    "vram_used_max": 0.0,
                    "vram_total": 0.0,
                },
            )
            entry["n"] += 1
            entry["util_sum"] += gpu.util_percent
            entry["util_max"] = max(entry["util_max"], gpu.util_percent)
            entry["vram_used_sum"] += gpu.vram_used_bytes
            entry["vram_used_max"] = max(entry["vram_used_max"], gpu.vram_used_bytes)
            if gpu.vram_total_bytes:
                entry["vram_total"] = float(gpu.vram_total_bytes)
        for disk in sample.disks:
            if disk.mount not in self.disks and len(self.disks) >= max_disks:
                self.dropped_keys += 1
                continue
            entry = self.disks.setdefault(
                disk.mount,
                {
                    "n": 0.0,
                    "free_sum": 0.0,
                    "free_min": float(disk.free_bytes),
                    "total": 0.0,
                },
            )
            entry["n"] += 1
            entry["free_sum"] += disk.free_bytes
            entry["free_min"] = min(entry["free_min"], float(disk.free_bytes))
            if disk.total_bytes:
                entry["total"] = float(disk.total_bytes)


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    """One closed bucket, shaped exactly like a ``resource_snapshot`` row."""

    node_id: str
    bucket_start: dt.datetime
    samples: int
    cpu_percent_mean: float
    cpu_percent_max: float
    ram_used_bytes_mean: int
    ram_used_bytes_max: int
    ram_total_bytes: int
    gpu: dict[str, Any]
    disks: dict[str, Any]


def _row(node_id: str, start: dt.datetime, acc: _Accumulator) -> SnapshotRow:
    count = max(acc.samples, 1)
    return SnapshotRow(
        node_id=node_id,
        bucket_start=start,
        samples=acc.samples,
        cpu_percent_mean=acc.cpu_sum / count,
        cpu_percent_max=acc.cpu_max,
        ram_used_bytes_mean=int(acc.ram_used_sum / count),
        ram_used_bytes_max=acc.ram_used_max,
        ram_total_bytes=acc.ram_total,
        gpu={
            str(index): {
                "util_mean": entry["util_sum"] / max(entry["n"], 1.0),
                "util_max": entry["util_max"],
                "vram_used_mean": int(entry["vram_used_sum"] / max(entry["n"], 1.0)),
                "vram_used_max": int(entry["vram_used_max"]),
                "vram_total": int(entry["vram_total"]),
                "samples": int(entry["n"]),
            }
            for index, entry in sorted(acc.gpus.items())
        },
        disks={
            mount: {
                "free_mean": int(entry["free_sum"] / max(entry["n"], 1.0)),
                "free_min": int(entry["free_min"]),
                "total": int(entry["total"]),
                "samples": int(entry["n"]),
            }
            for mount, entry in sorted(acc.disks.items())
        },
    )


class Downsampler:
    """Per-node, per-bucket accumulators; :meth:`closed_buckets` drains whole buckets."""

    def __init__(
        self,
        *,
        bucket_s: float = DEFAULT_BUCKET_S,
        max_open_buckets: int = DEFAULT_MAX_OPEN_BUCKETS,
        bucket_grace_s: float = DEFAULT_BUCKET_GRACE_S,
        wall_clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        if bucket_s <= 0:
            raise ValueError("bucket_s must be > 0")
        if not bucket_grace_s >= 0:
            raise ValueError("bucket_grace_s must be >= 0")
        self.max_gpu_keys = MAX_GPUS
        self.max_disk_keys = MAX_DISKS
        self.bucket_s = bucket_s
        self.bucket_grace_s = bucket_grace_s
        self._wall_clock = wall_clock or _utcnow
        """Only for how late a late sample is (the log line); never for bucketing."""
        # Buckets that ended but are still in their grace period are expected, not
        # a clock jump: they do not count against the operator's cap.
        self.max_open_buckets = max(1, max_open_buckets) + math.ceil(
            bucket_grace_s / bucket_s
        )
        self._open: dict[str, OrderedDict[dt.datetime, _Accumulator]] = {}
        self._last_flushed: dict[str, dt.datetime] = {}
        self._late_logged: dict[str, dt.datetime] = {}
        """The last bucket each node's late samples were logged for (D26)."""
        self.dropped_late = 0
        """Samples that arrived after their bucket was closed - later than the grace
        period (a long outage's replay, or an agent whose clock went back). Kept in
        the live ring, left out of history, logged (``sample_late``)."""
        self.dropped_buckets = 0
        """Open buckets evicted because one node had too many (a clock jump)."""
        self.dropped_keys = 0
        """GPU/disk keys refused by a bucket already at ``MAX_GPUS``/``MAX_DISKS``."""

    def add(self, node_id: str, sample: LiveSample) -> None:
        start = bucket_start(sample.observed_at, self.bucket_s)
        last = self._last_flushed.get(node_id)
        if last is not None and start <= last:
            self._late(node_id, start, sample)
            return
        buckets = self._open.setdefault(node_id, OrderedDict())
        accumulator = buckets.get(start)
        if accumulator is None:
            accumulator = _Accumulator()
            buckets[start] = accumulator
            # Keep the *newest* buckets: an old one that never closes is the
            # symptom of a clock that jumped forward once.
            while len(buckets) > self.max_open_buckets:
                evicted, _ = min(buckets.items(), key=lambda item: item[0])
                del buckets[evicted]
                self.dropped_buckets += 1
                log.warning(
                    "controller.resources.bucket_evicted",
                    node_id=node_id,
                    bucket_start=evicted.isoformat(),
                    open_buckets=len(buckets),
                    dropped_total=self.dropped_buckets,
                )
        before = accumulator.dropped_keys
        accumulator.add(
            sample, max_gpus=self.max_gpu_keys, max_disks=self.max_disk_keys
        )
        if accumulator.dropped_keys > before:
            self.dropped_keys += accumulator.dropped_keys - before
        if before == 0 and accumulator.dropped_keys:
            # Once per bucket: an agent varying its mounts would log every sample.
            log.warning(
                "controller.resources.bucket_keys_dropped",
                node_id=node_id,
                bucket_start=start.isoformat(),
                gpus=len(accumulator.gpus),
                disks=len(accumulator.disks),
                dropped_total=self.dropped_keys,
            )

    def _late(self, node_id: str, start: dt.datetime, sample: LiveSample) -> None:
        """Count a sample whose bucket is already closed and say so (D26).

        Warning once per (node, bucket) - a long outage's replay would otherwise
        write one line per buffered report - and debug for the rest of that
        bucket's late samples, so every one is still visible at DEBUG.
        """
        self.dropped_late += 1
        end = start + dt.timedelta(seconds=self.bucket_s)
        lateness_s = (self._wall_clock() - end).total_seconds()
        first = self._late_logged.get(node_id) != start
        self._late_logged[node_id] = start
        (log.warning if first else log.debug)(
            "controller.resources.sample_late",
            node_id=node_id,
            bucket_start=start.isoformat(),
            lateness_s=round(lateness_s, 3),
            grace_s=self.bucket_grace_s,
            sequence=sample.sequence,
            session_epoch=sample.session_epoch,
            dropped_late=self.dropped_late,
        )

    def forget(self, node_id: str) -> None:
        """Drop every open bucket of a node the tracker stopped following."""
        self._open.pop(node_id, None)
        self._last_flushed.pop(node_id, None)
        self._late_logged.pop(node_id, None)

    @property
    def open_buckets(self) -> int:
        return sum(len(buckets) for buckets in self._open.values())

    def closed_buckets(
        self, now: dt.datetime, *, grace: bool = True
    ) -> list[SnapshotRow]:
        """Remove and return every bucket whose end plus ``bucket_grace_s`` is at or
        before ``now`` (D26). ``grace=False`` closes every bucket that has *ended*:
        for the shutdown flush, when no session is left to deliver a late sample.

        ``now`` is a *wall clock* on purpose: buckets are wall-clock aligned so the
        history lines up across nodes.
        """
        rows: list[SnapshotRow] = []
        wait_s = self.bucket_s + (self.bucket_grace_s if grace else 0.0)
        cutoff = now.astimezone(dt.UTC) - dt.timedelta(seconds=wait_s)
        for node_id, buckets in self._open.items():
            for start in [s for s in buckets if s <= cutoff]:
                rows.append(_row(node_id, start, buckets.pop(start)))
                previous = self._last_flushed.get(node_id)
                if previous is None or start > previous:
                    self._last_flushed[node_id] = start
        rows.sort(key=lambda row: (row.bucket_start, row.node_id))
        return rows

    def drain(self) -> list[SnapshotRow]:
        """Remove and return **every** bucket, open ones included (shutdown only)."""
        rows = [
            _row(node_id, start, accumulator)
            for node_id, buckets in self._open.items()
            for start, accumulator in buckets.items()
        ]
        self._open.clear()
        rows.sort(key=lambda row: (row.bucket_start, row.node_id))
        return rows


class ResourceSink(Protocol):
    """What the session manager needs; the manager depends on this, not on the class."""

    def begin_session(
        self,
        node_id: str,
        replay_from_sequence: int,
        *,
        session_epoch: int | None = None,
    ) -> None:
        """Declare how this node's new session starts (see the module docstring).

        Called twice per session at most: once when the session is accepted
        (``replay_from_sequence=0``, "this may be a fresh process", with the
        ``Hello``'s ``session_epoch``) and once more from the session's
        ``Reconcile``, before any replayed report. The last call before the first
        report of the session wins.
        """

    def record(
        self,
        node_id: str,
        session_epoch: int,
        update: agent_pb2.ResourceUpdate,
        now: float,
    ) -> bool:
        """Record one update; False when it was a duplicate/replay and ignored."""


class ResourceTracker:
    """Live rings + downsampler for every node (the :class:`ResourceSink` in use)."""

    def __init__(
        self,
        *,
        live_ring_size: int = DEFAULT_LIVE_RING_SIZE,
        bucket_s: float = DEFAULT_BUCKET_S,
        max_tracked_nodes: int = DEFAULT_MAX_TRACKED_NODES,
        max_cursor_nodes: int = DEFAULT_MAX_CURSOR_NODES,
        max_open_buckets: int = DEFAULT_MAX_OPEN_BUCKETS,
        bucket_grace_s: float = DEFAULT_BUCKET_GRACE_S,
        wall_clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._ring_size = live_ring_size
        self._max_nodes = max(1, max_tracked_nodes)
        self._max_cursor_nodes = max(self._max_nodes, max_cursor_nodes)
        self._wall_clock = wall_clock or _utcnow
        self._rings: OrderedDict[str, LiveRing] = OrderedDict()
        self._cursors: OrderedDict[str, _Cursor] = OrderedDict()
        """node_id -> dedupe state, bounded by ``max_cursor_nodes`` on its own."""
        self.downsampler = Downsampler(
            bucket_s=bucket_s,
            max_open_buckets=max_open_buckets,
            bucket_grace_s=bucket_grace_s,
            wall_clock=self._wall_clock,
        )
        self.duplicates = 0
        self.recorded = 0
        self.evicted_nodes = 0
        """Live rings dropped under ``max_tracked_nodes`` (cursors are kept)."""
        self.evicted_cursors = 0
        """Dedupe cursors dropped under ``max_cursor_nodes`` - a duplicate window."""
        self.stale_epochs = 0
        """Reports dropped because they came from a superseded session's epoch."""

    # ------------------------------------------------------------- read side

    def ring(self, node_id: str) -> LiveRing | None:
        return self._rings.get(node_id)

    def latest(self, node_id: str) -> LiveSample | None:
        ring = self._rings.get(node_id)
        return ring.latest if ring is not None else None

    def nodes(self) -> list[str]:
        return list(self._rings)

    # ------------------------------------------------------------ write side

    def begin_session(
        self,
        node_id: str,
        replay_from_sequence: int,
        *,
        session_epoch: int | None = None,
    ) -> None:
        """See :class:`ResourceSink`; ``> 0`` keeps the cursor, ``0`` arms a reset.

        ``session_epoch`` (when the caller knows it, i.e. from ``Hello``) raises the
        node's epoch high-water mark immediately, so the fence is already armed in
        the window between the session opening and its first report - otherwise a
        zombie stream could get one report in at that moment.
        """
        cursor = self._cursor_for(node_id)
        cursor.reset_armed = replay_from_sequence <= 0
        if session_epoch is not None:
            cursor.epoch = (
                session_epoch
                if cursor.epoch is None
                else max(cursor.epoch, session_epoch)
            )
        log.debug(
            "controller.resources.begin_session",
            node_id=node_id,
            replay_from_sequence=replay_from_sequence,
            session_epoch=session_epoch,
            highest_epoch=cursor.epoch,
            cursor_reset_armed=cursor.reset_armed,
        )

    def record(
        self,
        node_id: str,
        session_epoch: int,
        update: agent_pb2.ResourceUpdate,
        now: float,
    ) -> bool:
        """See :class:`ResourceSink`. ``now`` is the controller's monotonic clock."""
        cursor_state = self._cursor_for(node_id)
        highest_epoch = cursor_state.epoch
        if highest_epoch is not None and session_epoch < highest_epoch:
            # A superseded stream still delivering: it must not move the cursor.
            self.stale_epochs += 1
            self.duplicates += 1
            log.debug(
                "controller.resources.stale_epoch_update",
                node_id=node_id,
                session_epoch=session_epoch,
                highest_epoch=highest_epoch,
                sequence=update.sequence,
                stale_epochs=self.stale_epochs,
            )
            return False
        if cursor_state.reset_armed:
            # First report of a session that did not announce a replay: treat the
            # sequence space as new (the agent process may have restarted).
            cursor_state.reset_armed = False
            cursor_state.sequence = None
        cursor = cursor_state.sequence
        if cursor is not None and update.sequence <= cursor:
            self.duplicates += 1
            log.debug(
                "controller.resources.duplicate_update",
                node_id=node_id,
                session_epoch=session_epoch,
                sequence=update.sequence,
                cursor=cursor,
                duplicates=self.duplicates,
            )
            return False
        sample = live_sample(
            update,
            session_epoch=session_epoch,
            received_at=now,
            wall_clock=self._wall_clock,
        )
        self._ring_for(node_id).append(sample)
        self.downsampler.add(node_id, sample)
        cursor_state.sequence = update.sequence
        cursor_state.epoch = (
            session_epoch
            if cursor_state.epoch is None
            else max(cursor_state.epoch, session_epoch)
        )
        self.recorded += 1
        return True

    def _cursor_for(self, node_id: str) -> _Cursor:
        """The node's dedupe state, created on demand, LRU-bounded on its own.

        Losing a cursor is the one way a replayed sample can be counted twice, so
        this cap is separate from (and much larger than) ``max_tracked_nodes`` and
        every eviction is counted and logged.
        """
        cursor = self._cursors.get(node_id)
        if cursor is None:
            cursor = _Cursor()
            self._cursors[node_id] = cursor
            while len(self._cursors) > self._max_cursor_nodes:
                evicted, dropped = self._cursors.popitem(last=False)
                self.evicted_cursors += 1
                log.warning(
                    "controller.resources.cursor_evicted",
                    node_id=evicted,
                    cursor=dropped.sequence,
                    highest_epoch=dropped.epoch,
                    cursor_reset_armed=dropped.reset_armed,
                    tracked_cursors=len(self._cursors),
                    max_cursor_nodes=self._max_cursor_nodes,
                    evicted_cursors=self.evicted_cursors,
                )
        self._cursors.move_to_end(node_id)
        return cursor

    def _ring_for(self, node_id: str) -> LiveRing:
        ring = self._rings.get(node_id)
        if ring is None:
            ring = LiveRing(self._ring_size)
            self._rings[node_id] = ring
            while len(self._rings) > self._max_nodes:
                evicted, _ = self._rings.popitem(last=False)
                # The dedupe cursor deliberately survives: it is three fields, and
                # dropping it here would let an in-flight reconnect replay its
                # buffered reports into a fresh ring and be counted twice.
                self.downsampler.forget(evicted)
                self.evicted_nodes += 1
                log.warning(
                    "controller.resources.node_evicted",
                    node_id=evicted,
                    tracked=len(self._rings),
                    max_tracked_nodes=self._max_nodes,
                )
        self._rings.move_to_end(node_id)
        return ring

    # ------------------------------------------------------------- flush side

    def closed_rows(
        self, now: dt.datetime | None = None, *, grace: bool = True
    ) -> list[SnapshotRow]:
        """Closed buckets (see :meth:`Downsampler.closed_buckets` for ``grace``)."""
        return self.downsampler.closed_buckets(now or self._wall_clock(), grace=grace)

    def drain_rows(self) -> list[SnapshotRow]:
        return self.downsampler.drain()


def total_samples(rows: Iterable[SnapshotRow]) -> int:
    """Convenience for logs and tests."""
    return sum(row.samples for row in rows)
