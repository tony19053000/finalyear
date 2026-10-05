"""V0.6 G4 (D111): the agent's ``ProbeService`` - idempotent by probe id,
bounded inputs, one probe at a time, the measurement rules (median RTT
without the warm-up request; the larger download only when the first was
fast), failures reported as results. HTTP through ``httpx.MockTransport`` with
a fake clock that advances per request."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from meshforge.agent.peer.probe import MAX_PROBE_BYTES, ProbeService
from meshforge.agent.settings import ProbeSettings
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.asyncio

MIB = 1024 * 1024


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make(
    *,
    per_request_s: dict[int, float] | None = None,
    status: int = 200,
    enabled: bool = True,
    hold: asyncio.Event | None = None,
) -> tuple[ProbeService, list[agent_pb2.NetProbeResult], list[int], asyncio.Event]:
    clock = Clock()
    sent: list[agent_pb2.NetProbeResult] = []
    sizes: list[int] = []
    reported = asyncio.Event()
    timing = per_request_s or {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok"
        size = int(request.url.params["bytes"])
        sizes.append(size)
        if hold is not None:
            await hold.wait()
        clock.t += timing.get(size, 0.001 * (len(sizes)))
        return httpx.Response(status, content=bytes(size))

    async def report(name: str, msg: Any) -> str:
        assert name == "net_probe_result"
        sent.append(msg)
        reported.set()
        return "id"

    service = ProbeService(
        ProbeSettings(enabled=enabled),
        report,
        node_id="node_a",
        clock=clock,
        transport=httpx.MockTransport(handler),
    )
    return service, sent, sizes, reported


def request(**kw: Any) -> agent_pb2.ProbeRequest:
    base: dict[str, Any] = {
        "probe_id": "prb1",
        "target_node_id": "node_b",
        "endpoint": "10.0.0.2:47120",
        "token": "tok",
        "rtt_samples": 3,
        "bytes": 4 * MIB,
        "max_bytes": 8 * MIB,
        "fast_s": 0.5,
    }
    base.update(kw)
    return agent_pb2.ProbeRequest(**base)


async def test_fast_link_rtt_median_and_the_larger_download() -> None:
    timing = {0: 0.002, 4 * MIB: 0.1, 8 * MIB: 0.2}
    service, sent, sizes, reported = make(per_request_s=timing)
    service.handle(request())
    await asyncio.wait_for(reported.wait(), 5)
    # warm-up + 3 RTT samples, 4 MiB (fast: 0.1 s < 0.5 s), then 8 MiB
    assert sizes == [0, 0, 0, 0, 4 * MIB, 8 * MIB]
    (result,) = sent
    assert result.ok and result.rtt_ms == pytest.approx(2.0)
    assert result.bw_bytes_s == pytest.approx(8 * MIB / 0.2, rel=1e-3)
    assert result.bytes == 12 * MIB


async def test_slow_link_stops_after_the_first_download() -> None:
    timing = {0: 0.05, 4 * MIB: 2.0}
    service, sent, sizes, reported = make(per_request_s=timing)
    service.handle(request())
    await asyncio.wait_for(reported.wait(), 5)
    assert sizes == [0, 0, 0, 0, 4 * MIB]
    assert sent[0].bw_bytes_s == pytest.approx(4 * MIB / 2.0, rel=1e-3)
    assert sent[0].rtt_ms == pytest.approx(50.0)


async def test_a_replayed_probe_id_is_not_measured_again() -> None:
    service, sent, sizes, reported = make()
    service.handle(request())
    await asyncio.wait_for(reported.wait(), 5)
    before = list(sizes)
    reported.clear()
    service.handle(request())  # the controller re-sent it
    await asyncio.wait_for(reported.wait(), 5)
    assert sizes == before
    assert len(sent) == 2 and sent[0] == sent[1]


async def test_one_probe_at_a_time_the_second_is_busy() -> None:
    hold = asyncio.Event()
    service, sent, _sizes, reported = make(hold=hold)
    service.handle(request(probe_id="prb1"))
    service.handle(request(probe_id="prb2"))
    await asyncio.wait_for(reported.wait(), 5)
    assert [(r.probe_id, r.ok, r.error) for r in sent] == [("prb2", False, "busy")]
    hold.set()
    await service.close()


@pytest.mark.parametrize(
    "bad",
    [
        {"endpoint": "no-port"},
        {"endpoint": "http://x:1"},
        {"endpoint": "10.0.0.2:0"},
        {"endpoint": "10.0.0.2:99999"},
        {"token": ""},
        {"bytes": MAX_PROBE_BYTES + 1},
        {"max_bytes": MAX_PROBE_BYTES + 1},
        {"rtt_samples": 11},
        {"fast_s": -1.0},
    ],
)
async def test_bad_requests_are_refused_without_a_request(bad: dict[str, Any]) -> None:
    service, sent, sizes, reported = make()
    service.handle(request(**bad))
    await asyncio.wait_for(reported.wait(), 5)
    assert sizes == []
    assert (sent[0].ok, sent[0].error) == (False, "bad_request")


async def test_a_bad_probe_id_is_dropped_silently() -> None:
    service, sent, sizes, _reported = make()
    service.handle(request(probe_id="../../etc"))
    await asyncio.sleep(0.05)
    assert sent == [] and sizes == [] and service.refused == 1


async def test_disabled_refuses() -> None:
    service, sent, sizes, reported = make(enabled=False)
    service.handle(request())
    await asyncio.wait_for(reported.wait(), 5)
    assert sizes == [] and sent[0].error == "disabled"


@pytest.mark.parametrize(("status", "error"), [(403, "http_403"), (503, "http_503")])
async def test_a_refusing_target_is_a_typed_failure(status: int, error: str) -> None:
    service, sent, _sizes, reported = make(status=status)
    service.handle(request())
    await asyncio.wait_for(reported.wait(), 5)
    assert (sent[0].ok, sent[0].error) == (False, error)


async def test_rtt_only_probe() -> None:
    service, sent, sizes, reported = make()
    service.handle(request(bytes=0, max_bytes=0, rtt_samples=1))
    await asyncio.wait_for(reported.wait(), 5)
    assert sizes == [0] and sent[0].ok and sent[0].bw_bytes_s == 0.0
