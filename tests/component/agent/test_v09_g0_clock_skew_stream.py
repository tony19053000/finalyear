"""V0.9 G0 item 1 (D210 item 1, D213): live latency is timed on the
controller's clock, never on an agent's - a real HTTPS controller on real
PostgreSQL, a real agent session, the real ``/v1/events/ws`` stream.

* ``GET /v1/clock`` answers the controller's wall clock (authenticated);
* a heartbeat whose agent clock is 5 s ahead arrives as a ``node.metrics``
  event whose ``recv_at`` is the controller's arrival time - and the perf
  script's heartbeat latency computed from that frame is the same as for an
  agent with a correct clock (the "+5 s agent offset -> result unchanged"
  check of the brief, end to end through the real event path).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mf_ui_perf import load_ui_perf

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_exec_harness import ExecAgent
from mf_users import bearer, create_user, login
from websockets.asyncio.client import ClientConnection, connect

from meshforge.common.clock_offset import ClockExchange, estimate_offset
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(240),
]

ROOT = Path(__file__).resolve().parents[3]
PASSWORD = "carol-password-1"
AGENT_SKEW = dt.timedelta(seconds=5)


def _ui_perf() -> ModuleType:
    return load_ui_perf("ui_perf_v09_g0_component")


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "login_rate_per_minute": 600.0, "login_rate_burst": 100},
        jobs={"dispatch_interval_s": 60.0},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def token(
    pg_settings: DatabaseSettings, runtime: ControllerRuntime
) -> AsyncIterator[str]:
    engine = create_engine(pg_settings)
    try:
        await create_user(engine, "carol", PASSWORD)
    finally:
        await engine.dispose()
    async with httpx.AsyncClient(base_url=_origin(runtime), timeout=30) as anon:
        yield await login(anon, "carol", PASSWORD)


def _origin(runtime: ControllerRuntime) -> str:
    return f"https://127.0.0.1:{runtime.http_port}"


async def _until(ws: ClientConnection, match: Any, within: float) -> dict[str, Any]:
    deadline = time.monotonic() + within
    while True:
        raw = await asyncio.wait_for(ws.recv(), max(0.05, deadline - time.monotonic()))
        frame = json.loads(raw)
        if isinstance(frame, dict) and match(frame):
            return frame


async def test_clock_answers_the_controller_wall_clock_and_needs_a_user(
    runtime: ControllerRuntime, token: str
) -> None:
    async with httpx.AsyncClient(base_url=_origin(runtime), timeout=30) as http:
        assert (await http.get("/v1/clock")).status_code == 401
        exchanges = []
        for _ in range(8):
            sent = time.time() * 1000.0
            got = await http.get("/v1/clock", headers=bearer(token))
            received = time.time() * 1000.0
            assert got.status_code == 200, got.text
            body = got.json()
            assert set(body) == {"server_time"}
            exchanges.append(ClockExchange(sent, body["server_time"] * 1000, received))
    clock = estimate_offset(exchanges)
    # One host, one clock: the estimate is ~0 and certainly inside its bound.
    assert abs(clock.offset_ms) <= clock.bound_ms + 0.01, clock
    assert clock.bound_ms < 50.0, clock


async def test_an_agent_clock_5_s_ahead_leaves_the_heartbeat_latency_unchanged(
    runtime: ControllerRuntime, token: str, grpc_target: str, tmp_path: Path
) -> None:
    perf = _ui_perf()
    url = f"wss://127.0.0.1:{runtime.http_port}/v1/events/ws"
    async with connect(url, additional_headers=bearer(token)) as ws:
        assert json.loads(await ws.recv())["type"] == "hello"
        agent = await ExecAgent.start(grpc_target, tmp_path / "agent")
        try:
            status = await _until(
                ws,
                lambda f: f.get("type") == "node.status" and f["data"]["connected"],
                within=20,
            )
            node_id = status["data"]["node_id"]
            assert runtime.resources is not None and runtime.manager is not None
            live = runtime.manager.get(node_id)
            assert live is not None
            frames: dict[str, dict[str, Any]] = {}
            for sequence, skew in ((1, dt.timedelta(0)), (2, AGENT_SKEW)):
                update = agent_pb2.ResourceUpdate(
                    sequence=sequence, cpu_percent=10.0 * sequence
                )
                update.observed_at.FromDatetime(dt.datetime.now(dt.UTC) + skew)
                before = time.time()
                assert runtime.resources.record(
                    node_id,
                    live.session_epoch,
                    update,
                    asyncio.get_running_loop().time(),
                )
                after = time.time()
                frame = await _until(
                    ws,
                    lambda f, s=sequence: (
                        f.get("type") == "node.metrics"
                        and f["data"]["node_id"] == node_id
                        and f["data"]["cpu_percent"] == 10.0 * s
                    ),
                    within=20,
                )
                # recv_at is the CONTROLLER's arrival time, whatever the agent's clock.
                assert before - 0.001 <= frame["data"]["recv_at"] <= after + 0.001
                frames["skewed" if skew else "true"] = frame["data"]
            async with httpx.AsyncClient(base_url=_origin(runtime), timeout=30) as h:
                got = await h.get(f"/v1/nodes/{node_id}/metrics", headers=bearer(token))
            assert got.status_code == 200
            assert all(p["recv_at"] is not None for p in got.json()["samples"])
        finally:
            await agent.stop()

    observed_gap_ms = (
        dt.datetime.fromisoformat(frames["skewed"]["observed_at"])
        - dt.datetime.fromisoformat(frames["true"]["observed_at"])
    ).total_seconds() * 1000.0
    assert observed_gap_ms > 4000.0  # the agent's clock really jumped 5 s

    # The page shows each sample exactly 120 ms after it reached the controller
    # (browser clock = controller clock here: offset 0 +- 0.5 ms).
    clock = estimate_offset([ClockExchange(0.0, 0.5, 1.0)])

    def lag(data: dict[str, Any]) -> list[float]:
        shown = [{"at": data["observed_at"], "shown": data["recv_at"] * 1000 + 120.0}]
        received = {data["observed_at"]: data["recv_at"]}
        samples = perf.HeartbeatSamples.build(shown, received, clock)
        assert samples.unmatched == 0
        return [round(x, 3) for x in samples.lags]

    assert lag(frames["true"]) == lag(frames["skewed"]) == [120.0]
