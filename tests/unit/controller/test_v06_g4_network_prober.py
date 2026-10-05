"""V0.6 G4 (D111): :class:`NetworkProber` around the scheduler - what it sends,
which results it believes, how it frees nodes. No database (a fake store), a
fake session manager, an injected clock.

Results are hostile input: only the node the probe was sent to, naming the
probe's target, ends a probe; a failure or impossible numbers are remembered
per pair, never written; node loss frees the probe."""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass, field
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meshforge.common.peer_token import verify_probe_token
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import NodeState
from meshforge.controller.network.service import NetworkProber
from meshforge.controller.network.store import ProbeSample
from meshforge.controller.peer_signing import PeerTokenIssuer
from meshforge.controller.settings import NetworkSettings

A = "node_01J00000000000000000000000"
B = "node_01J00000000000000000000001"
C = "node_01J00000000000000000000002"
KEY = Ed25519PrivateKey.generate()
WALL = 1_800_000_000.0


@dataclass
class FakeLive:
    node_id: str
    peer_endpoint: str = "127.0.0.1:47120"
    state: NodeState = NodeState.ONLINE
    ended: bool = False
    session_id: str = "sess"


@dataclass
class FakeManager:
    lives: list[FakeLive]
    sent: list[tuple[str, str, Any]] = field(default_factory=list)
    refuse: bool = False

    def snapshot(self) -> list[FakeLive]:
        return list(self.lives)

    def send_command(self, node_id: str, name: str, payload: Any) -> str | None:
        if self.refuse:
            return None
        self.sent.append((node_id, name, payload))
        return "cmd"


@dataclass
class FakeStore:
    written: list[ProbeSample] = field(default_factory=list)

    async def record(self, sample: ProbeSample, **_: Any) -> bool:
        self.written.append(sample)
        return True

    async def gpu_busy(self, _states: Any) -> set[str]:
        return set()


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make(
    lives: list[FakeLive],
    *,
    gpu: set[str] | None = None,
    **settings: Any,
) -> tuple[NetworkProber, FakeManager, FakeStore, Clock]:
    clock = Clock()
    manager = FakeManager(lives)
    store = FakeStore()

    async def busy() -> set[str]:
        return gpu or set()

    prober = NetworkProber(
        manager,  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        PeerTokenIssuer(KEY, clock=lambda: WALL),
        NetworkSettings(probe_gap_s=1.0, probe_jitter=0.0, **settings),
        clock=clock,
        gpu_busy=busy,
        rng=random.Random(1),  # noqa: S311
    )
    return prober, manager, store, clock


def result(cmd: agent_pb2.ProbeRequest, **kw: Any) -> agent_pb2.NetProbeResult:
    base: dict[str, Any] = {
        "probe_id": cmd.probe_id,
        "target_node_id": cmd.target_node_id,
        "ok": True,
        "rtt_ms": 2.5,
        "bw_bytes_s": 50e6,
        "bytes": 12 * 1024 * 1024,
    }
    base.update(kw)
    return agent_pb2.NetProbeResult(**base)


async def started(prober: NetworkProber, clock: Clock, manager: FakeManager) -> Any:
    clock.t = prober.scheduler.next_at
    planned = await prober.tick()
    assert planned is not None
    node, name, cmd = manager.sent[-1]
    assert name == "probe_request" and node == planned.src
    return cmd


@pytest.mark.asyncio
async def test_the_command_carries_endpoint_sizes_and_a_valid_probe_token() -> None:
    prober, manager, _store, clock = make([FakeLive(A), FakeLive(B, "10.0.0.2:47120")])
    cmd = await started(prober, clock, manager)
    assert (cmd.target_node_id, cmd.endpoint) == (B, "10.0.0.2:47120")
    assert (cmd.rtt_samples, cmd.bytes, cmd.max_bytes) == (3, 4 << 20, 8 << 20)
    assert cmd.fast_s == 0.5
    claims = verify_probe_token(KEY.public_key(), cmd.token, holder=B, now=WALL)
    assert claims.requester == A and claims.exp - WALL <= 60


@pytest.mark.asyncio
async def test_a_good_result_from_the_right_node_is_queued_and_written() -> None:
    prober, manager, store, clock = make([FakeLive(A), FakeLive(B)])
    cmd = await started(prober, clock, manager)
    prober.on_result(A, result(cmd))
    await prober.drain()
    assert store.written == [ProbeSample(A, B, 2.5, 50e6)]
    assert prober.measured == 1 and not prober.scheduler.in_flight


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sender", "target", "probe_id"),
    [
        (B, B, None),  # another node reports it
        (A, C, None),  # names another target
        (A, B, "prb-unknown"),  # never sent
    ],
)
async def test_results_that_do_not_match_the_probe_are_dropped(
    sender: str, target: str, probe_id: str | None
) -> None:
    prober, manager, store, clock = make([FakeLive(A), FakeLive(B)])
    cmd = await started(prober, clock, manager)
    fake = result(cmd, target_node_id=target)
    if probe_id is not None:
        fake.probe_id = probe_id
    prober.on_result(sender, fake)
    await prober.drain()
    assert store.written == [] and prober.dropped == 1
    assert cmd.probe_id in prober.scheduler.in_flight  # still waiting


@pytest.mark.asyncio
async def test_a_duplicate_result_counts_once() -> None:
    prober, manager, store, clock = make([FakeLive(A), FakeLive(B)])
    cmd = await started(prober, clock, manager)
    prober.on_result(A, result(cmd))
    prober.on_result(A, result(cmd))
    await prober.drain()
    assert len(store.written) == 1 and prober.dropped == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"ok": False, "error": "timeout", "rtt_ms": 0, "bw_bytes_s": 0}, "timeout"),
        ({"bw_bytes_s": 0.0}, "bad_numbers"),
        ({"bw_bytes_s": math.inf}, "bad_numbers"),
        ({"rtt_ms": -1.0}, "bad_numbers"),
        ({"rtt_ms": math.nan}, "bad_numbers"),
        (
            {"ok": False, "error": "\x1b[31mevil\n" + "x" * 99},
            "31mevil" + "x" * 22,
        ),
    ],
)
async def test_failures_are_remembered_per_pair_and_never_written(
    fields: dict[str, Any], error: str
) -> None:
    prober, manager, store, clock = make([FakeLive(A), FakeLive(B)])
    cmd = await started(prober, clock, manager)
    prober.on_result(A, result(cmd, **fields))
    await prober.drain()
    assert store.written == []
    assert prober.failures[(A, B)].error == error
    assert not prober.scheduler.in_flight


@pytest.mark.asyncio
async def test_a_command_that_cannot_be_sent_frees_the_pair() -> None:
    prober, manager, _store, clock = make([FakeLive(A), FakeLive(B)])
    manager.refuse = True
    clock.t = prober.scheduler.next_at
    assert await prober.tick() is None
    assert not prober.scheduler.in_flight and prober.scheduler.started == 0


@pytest.mark.asyncio
async def test_offline_node_frees_its_probe_and_its_late_result_is_dropped() -> None:
    prober, manager, store, clock = make([FakeLive(A), FakeLive(B)])
    cmd = await started(prober, clock, manager)
    prober.on_node_offline(FakeLive(B), "lease_expired")  # type: ignore[arg-type]
    assert not prober.scheduler.in_flight
    prober.on_result(A, result(cmd))
    await prober.drain()
    assert store.written == [] and prober.dropped == 1


@pytest.mark.asyncio
async def test_timeout_is_recorded_as_a_pair_failure() -> None:
    prober, manager, _store, clock = make(
        [FakeLive(A), FakeLive(B)], probe_timeout_s=5.0
    )
    await started(prober, clock, manager)
    clock.t += 5.0
    await prober.tick()
    assert prober.failures[(A, B)].error == "timeout"
    assert prober.scheduler.timed_out == 1


@pytest.mark.asyncio
async def test_suspect_or_gpu_busy_nodes_are_not_probed() -> None:
    prober, manager, _store, clock = make(
        [FakeLive(A), FakeLive(B, state=NodeState.SUSPECT), FakeLive(C)], gpu={C}
    )
    clock.t = prober.scheduler.next_at
    assert await prober.tick() is None
    assert manager.sent == []


@pytest.mark.asyncio
async def test_disabled_prober_sends_nothing() -> None:
    prober, manager, _store, clock = make([FakeLive(A), FakeLive(B)], enabled=False)
    clock.t = 10_000.0
    assert await prober.tick() is None and manager.sent == []


def test_settings_refuse_a_budget_that_fits_no_probe() -> None:
    with pytest.raises(ValueError, match="probe_budget_bytes_per_min"):
        NetworkSettings(probe_budget_bytes_per_min=1024)
    NetworkSettings(probe_budget_bytes_per_min=1024, enabled=False)


def test_status_reports_counters_and_budget() -> None:
    prober, _m, _s, _c = make([FakeLive(A)])
    status = prober.status()
    assert status["enabled"] is True and status["in_flight"] == []
    assert status["budget_bytes_per_min"] == 48 * 1024 * 1024
    assert isinstance(dt.datetime.now(dt.UTC), dt.datetime)
