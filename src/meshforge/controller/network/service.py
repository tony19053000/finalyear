"""The controller's network prober (V0.6 G4, D111): one task that starts probes
within the :class:`~.scheduler.ProbeScheduler`'s bounds and folds their results
into ``network_link``.

* **Tick.** When the scheduler is due, the candidates are the nodes with a live
  ONLINE session (a target also needs an advertised peer endpoint, D99); a node
  holding a GPU for a live attempt (one database read per due tick) or whose
  latest CPU sample is above ``busy_cpu_percent`` is skipped. The chosen source
  gets ``ProbeRequest`` ("ProbeLink") with the target's endpoint and a probe
  token signed for ``(requester = source, holder = target)``, ``token_ttl_s``
  long. A command that cannot be queued frees the pair at once.
* **Result.** ``NetProbeResult`` arrives on the source's session (a report:
  acked by the session layer, replayed after a reconnect). It counts only from
  the session of the node the probe was sent to and for its target; anything
  else (unknown, late, duplicate, another node's id) is dropped and counted -
  reports are hostile input. A good result is queued for the database (the
  handler never waits on it); a failure (``ok = false``, a non-finite or
  non-positive number) is remembered per pair for ``compute network``.
* **Node loss.** A node that goes OFFLINE frees its probes in flight; their
  late results are then dropped.
* **Restart.** Nothing is persisted but the links: a restarted controller starts
  with an empty schedule (first probe after one jittered gap) and fills pairs
  oldest-first again.

Every probe start, result and drop is logged with ``node_id`` / ``probe_id``;
the token is never logged.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import math
import random
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import ATTEMPT_TERMINAL_STATES, AttemptState
from meshforge.controller.liveness import NodeState
from meshforge.controller.network.scheduler import (
    PlannedProbe,
    ProbeConfig,
    ProbeNode,
    ProbeScheduler,
)
from meshforge.controller.network.store import NetworkStore, ProbeSample

if TYPE_CHECKING:
    from meshforge.controller.liveness import LivenessEvent
    from meshforge.controller.peer_signing import PeerTokenIssuer
    from meshforge.controller.resources import ResourceTracker
    from meshforge.controller.session.live import LiveSession
    from meshforge.controller.session.manager import SessionManager
    from meshforge.controller.settings import NetworkSettings

__all__ = ["LIVE_ATTEMPT_STATES", "NetworkProber", "PairFailure"]

log = get_logger(__name__)

LIVE_ATTEMPT_STATES: Final = tuple(
    s.value for s in AttemptState if s not in ATTEMPT_TERMINAL_STATES
)
_QUEUE: Final = 256
_FAILURES_KEPT: Final = 1024
_MAX_RTT_MS: Final = 600_000.0
_MAX_BW: Final = 1e12
_ERROR_CHARS: Final = 32


@dataclass(frozen=True, slots=True)
class PairFailure:
    """The latest failed probe of a pair (memory only; ``compute network``)."""

    error: str
    at: dt.datetime


def probe_config(settings: NetworkSettings) -> ProbeConfig:
    return ProbeConfig(
        interval_s=settings.probe_interval_s,
        gap_s=settings.probe_gap_s,
        jitter=settings.probe_jitter,
        budget_bytes_per_min=settings.probe_budget_bytes_per_min,
        probe_bytes=settings.probe_bytes,
        probe_max_bytes=settings.probe_max_bytes,
        timeout_s=settings.probe_timeout_s,
        busy_cpu_percent=settings.busy_cpu_percent,
    )


def _error_code(text: str) -> str:
    clean = "".join(c for c in text[:_ERROR_CHARS] if c.isalnum() or c == "_")
    return clean or "unknown"


class NetworkProber:
    """See module docstring. Owned by the controller runtime."""

    def __init__(
        self,
        manager: SessionManager,
        store: NetworkStore,
        issuer: PeerTokenIssuer,
        settings: NetworkSettings,
        *,
        clock: Callable[[], float],
        wall_clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        resources: ResourceTracker | None = None,
        gpu_busy: Callable[[], Awaitable[set[str]]] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._manager = manager
        self._store = store
        self._issuer = issuer
        self.settings = settings
        self._clock = clock
        self._wall = wall_clock
        self._resources = resources
        self._gpu_busy = gpu_busy or (lambda: store.gpu_busy(LIVE_ATTEMPT_STATES))
        self.scheduler = ProbeScheduler(
            probe_config(settings),
            rng=rng or random.Random(),  # noqa: S311 - jitter, not security
            now=clock(),
        )
        self._queue: asyncio.Queue[ProbeSample] = asyncio.Queue(maxsize=_QUEUE)
        self.failures: OrderedDict[tuple[str, str], PairFailure] = OrderedDict()
        self.measured = 0
        self.failed = 0
        self.dropped = 0
        self.written = 0

    # ------------------------------------------------------------------ loop

    async def run(self) -> None:
        """Tick, then wait for a result or the next due time (bounded 1 s)."""
        while True:
            try:
                await self.tick()
            except Exception:  # one bad tick must not stop probing
                log.exception("controller.network.tick_failed")
            wait = min(1.0, max(0.05, self.scheduler.next_at - self._clock()))
            try:
                sample = await asyncio.wait_for(self._queue.get(), wait)
            except TimeoutError:
                continue
            await self._write(sample)

    async def drain(self) -> None:
        """Write every queued result now (tests, shutdown)."""
        while not self._queue.empty():
            await self._write(self._queue.get_nowait())

    async def _write(self, sample: ProbeSample) -> None:
        try:
            if await self._store.record(
                sample,
                now=self._wall(),
                alpha=self.settings.ewma_alpha,
                stale_after_s=self.settings.stale_after_s,
            ):
                self.written += 1
        except Exception:
            log.exception(
                "controller.network.write_failed",
                src_node_id=sample.src_node_id,
                dst_node_id=sample.dst_node_id,
            )

    def _candidates(self, gpu_busy: set[str]) -> list[ProbeNode]:
        nodes: list[ProbeNode] = []
        for live in self._manager.snapshot():
            if live.ended or live.state is not NodeState.ONLINE:
                continue
            sample = (
                None
                if self._resources is None
                else self._resources.latest(live.node_id)
            )
            nodes.append(
                ProbeNode(
                    node_id=live.node_id,
                    endpoint=live.peer_endpoint,
                    gpu_job=live.node_id in gpu_busy,
                    cpu_percent=None if sample is None else sample.cpu_percent,
                )
            )
        return nodes

    async def tick(self) -> PlannedProbe | None:
        """One scheduling step (public for tests with an injected clock)."""
        now = self._clock()
        for probe_id, flight in self.scheduler.expire(now):
            self._failed(flight.src, flight.dst, "timeout", probe_id)
        if not self.settings.enabled or now < self.scheduler.next_at:
            return None
        if len(self._manager.snapshot()) < 2:  # a pair needs two nodes
            self.scheduler.pick((), now)  # keep the stagger moving
            return None
        busy = await self._gpu_busy()
        planned = self.scheduler.pick(self._candidates(busy), self._clock())
        if planned is None:
            return None
        command = agent_pb2.ProbeRequest(
            probe_id=planned.probe_id,
            target_node_id=planned.dst,
            endpoint=planned.endpoint,
            token=self._issuer.issue_probe(
                requester=planned.src,
                holder=planned.dst,
                ttl_s=self.settings.token_ttl_s,
            ),
            rtt_samples=self.settings.rtt_samples,
            bytes=self.settings.probe_bytes,
            max_bytes=self.settings.probe_max_bytes,
            fast_s=self.settings.probe_fast_s,
        )
        if self._manager.send_command(planned.src, "probe_request", command) is None:
            self.scheduler.abort(planned.probe_id)
            log.info(
                "controller.network.probe_not_sent",
                probe_id=planned.probe_id,
                node_id=planned.src,
                target_node_id=planned.dst,
            )
            return None
        log.info(
            "controller.network.probe_started",
            probe_id=planned.probe_id,
            node_id=planned.src,
            target_node_id=planned.dst,
        )
        return planned

    # --------------------------------------------------------------- results

    async def on_message(self, live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
        self.on_result(live.node_id, msg.net_probe_result)

    def on_result(self, node_id: str, result: agent_pb2.NetProbeResult) -> None:
        """A ``NetProbeResult`` from ``node_id`` (module docstring)."""
        flight = self.scheduler.in_flight.get(result.probe_id)
        if (
            flight is None
            or flight.src != node_id
            or flight.dst != result.target_node_id
        ):
            self.dropped += 1
            log.info(
                "controller.network.result_dropped",
                node_id=node_id,
                probe_id=result.probe_id[:64],
                reason="unknown_probe",
            )
            return
        self.scheduler.finish(result.probe_id, int(result.bytes), self._clock())
        rtt, bw = float(result.rtt_ms), float(result.bw_bytes_s)
        if not result.ok:
            self._failed(
                flight.src, flight.dst, _error_code(result.error), result.probe_id
            )
            return
        if not (
            math.isfinite(rtt)
            and math.isfinite(bw)
            and 0.0 <= rtt <= _MAX_RTT_MS
            and 0.0 < bw <= _MAX_BW
        ):
            self._failed(flight.src, flight.dst, "bad_numbers", result.probe_id)
            return
        self.measured += 1
        self.failures.pop((flight.src, flight.dst), None)
        log.info(
            "controller.network.probe_result",
            probe_id=result.probe_id,
            node_id=flight.src,
            target_node_id=flight.dst,
            rtt_ms=round(rtt, 3),
            bw_bytes_s=round(bw, 1),
            bytes=int(result.bytes),
        )
        try:
            self._queue.put_nowait(ProbeSample(flight.src, flight.dst, rtt, bw))
        except asyncio.QueueFull:
            self.dropped += 1
            log.warning(
                "controller.network.result_dropped",
                node_id=node_id,
                probe_id=result.probe_id,
                reason="queue_full",
            )

    def _failed(self, src: str, dst: str, error: str, probe_id: str) -> None:
        self.failed += 1
        self.failures[(src, dst)] = PairFailure(error, self._wall())
        self.failures.move_to_end((src, dst))
        while len(self.failures) > _FAILURES_KEPT:
            self.failures.popitem(last=False)
        log.info(
            "controller.network.probe_failed",
            probe_id=probe_id,
            node_id=src,
            target_node_id=dst,
            error=error,
        )

    def on_node_offline(self, live: LiveSession, event: LivenessEvent) -> None:
        """Session hook: free the node's probes in flight."""
        for probe_id in self.scheduler.forget_node(live.node_id):
            log.info(
                "controller.network.probe_abandoned",
                probe_id=probe_id,
                node_id=live.node_id,
                reason="node_offline",
            )

    def status(self) -> dict[str, Any]:
        """Counters + in-flight probes for ``GET /v1/network``."""
        return {
            "enabled": self.settings.enabled,
            "started": self.scheduler.started,
            "measured": self.measured,
            "failed": self.failed,
            "timed_out": self.scheduler.timed_out,
            "dropped": self.dropped,
            "in_flight": [
                {"src_node_id": f.src, "dst_node_id": f.dst}
                for f in self.scheduler.in_flight.values()
            ],
            "budget_bytes_per_min": self.settings.probe_budget_bytes_per_min,
            "budget_used_bytes": self.scheduler.spent(self._clock()),
        }

    def failure_rows(self) -> Sequence[tuple[str, str, PairFailure]]:
        return [(s, d, f) for (s, d), f in self.failures.items()]
