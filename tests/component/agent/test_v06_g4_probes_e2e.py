"""V0.6 G4 (D111) end to end: a real controller (PostgreSQL, gRPC, REST) and two
real agents whose peer servers listen on loopback.

* the controller's prober sends ``ProbeRequest`` to one agent, which measures
  the other's ``/v1/probe`` and reports ``NetProbeResult``; both directions end
  up in ``network_link`` and in ``GET /v1/network`` (RTT, bandwidth, samples,
  age, not stale), and ``compute network`` (a real CLI process) prints them;
* the probe route refuses a request without a token (401), with a blob token
  or a probe token for another holder (403), and a size above the cap (400);
* a second result for the same pair is folded in as an EWMA (``samples`` 2) -
  ``NetworkStore.record`` on the real table, including the stale restart;
* the agent's ``ProbeService`` reports an unreachable target as a failure.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio

from meshforge.common.fabric_tls import peer_server_name

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent
from mf_fabric import ca_file, client_ssl_context, peer_client_context
from mf_jobs_harness import HARNESS_NODE_ID, token_of
from sqlalchemy import text

from meshforge.agent.peer.probe import ProbeService
from meshforge.agent.settings import ProbeSettings
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_attempt_id
from meshforge.common.peer_token import sign_peer_token, sign_probe_token
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import NodeState
from meshforge.controller.network.service import NetworkProber
from meshforge.controller.network.store import NetworkStore, ProbeSample
from meshforge.controller.peer_signing import (
    PeerTokenIssuer,
    load_or_create_signing_key,
)
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings, NetworkSettings
from meshforge.controller.store.engine import create_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(90)]

PEER = {"peer": {"bind": "127.0.0.1", "port": 0}}
MIB = 1024 * 1024


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        **tolerant_liveness(),
        network={
            "probe_gap_s": 0.2,
            "probe_jitter": 0.0,
            "probe_bytes": 1 * MIB,
            "probe_max_bytes": 2 * MIB,
            "probe_budget_bytes_per_min": 64 * MIB,
        },
    )


@pytest_asyncio.fixture
async def pair(
    grpc_target: str, tmp_path: Path
) -> AsyncIterator[tuple[ExecAgent, ExecAgent]]:
    a = await ExecAgent.start(grpc_target, tmp_path / "a", agent_overrides=PEER)
    b = await ExecAgent.start(grpc_target, tmp_path / "b", agent_overrides=PEER)
    try:
        yield a, b
    finally:
        await a.stop()
        await b.stop()


async def _network(runtime: ControllerRuntime) -> dict[str, Any]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        trust_env=False,
        verify=client_ssl_context(node_id=HARNESS_NODE_ID),
    ) as http:
        resp = await http.get("/v1/network")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def test_probes_fill_the_matrix_both_ways(
    runtime: ControllerRuntime, pair: tuple[ExecAgent, ExecAgent], tmp_path: Path
) -> None:
    a, b = pair
    deadline = time.monotonic() + 30
    links: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        links = (await _network(runtime))["links"]
        if len(links) >= 2:
            break
        await asyncio.sleep(0.2)
    pairs = {(x["src_node_id"], x["dst_node_id"]) for x in links}
    assert pairs == {(a.node_id, b.node_id), (b.node_id, a.node_id)}, links
    for link in links:
        assert link["samples"] >= 1 and not link["stale"]
        assert link["bw_bytes_s"] > 1e6  # loopback: well above 1 MB/s
        assert 0 <= link["rtt_ms"] < 1000 and link["age_s"] < 60
        assert link["source"] == "measured"
    payload = await _network(runtime)
    assert payload["prober"]["measured"] >= 2
    assert payload["prober"]["failed"] == 0
    for agent in (a, b):
        assert agent.peer is not None and agent.peer.stats.probes_served >= 3
        assert agent.prober is not None and agent.prober.measured >= 1

    # `compute network` (a real CLI process) prints the matrix
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")},
        "MESHFORGE_CLI__CONTROLLER_URL": f"https://127.0.0.1:{runtime.http_port}",
        "MESHFORGE_CLI__CA_FILE": str(ca_file()),
        "MESHFORGE_CLI__TOKEN": token_of(runtime),
        "MESHFORGE_DATA_DIR": str(tmp_path / "no-agent-here"),
        "XDG_CONFIG_HOME": str(tmp_path / "cli-config"),
        "COLUMNS": "250",
    }
    out = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "meshforge.cli", "network"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert a.node_id[-8:] in out.stdout and b.node_id[-8:] in out.stdout
    assert "MB/s" in out.stdout or "GB/s" in out.stdout
    assert "probes: on" in out.stdout


async def test_a_link_older_than_stale_after_is_shown_stale_with_its_age(
    runtime: ControllerRuntime,
    pair: tuple[ExecAgent, ExecAgent],
    pg_settings: Any,
    tmp_path: Path,
) -> None:
    """``GET /v1/network`` and ``compute network`` age a real probed row: the
    pair measured two hours ago is ``stale`` (the other one is not)."""
    a, b = pair
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if len((await _network(runtime))["links"]) >= 2:
            break
        await asyncio.sleep(0.2)
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn:
            done = await conn.execute(
                text(
                    "UPDATE network_link SET measured_at = measured_at - "
                    "interval '2 hours' WHERE src_node_id = :s AND dst_node_id = :d"
                ),
                {"s": a.node_id, "d": b.node_id},
            )
        assert done.rowcount == 1
    finally:
        await engine.dispose()
    payload = await _network(runtime)
    assert payload["stale_after_s"] == 3600.0
    by_pair = {(x["src_node_id"], x["dst_node_id"]): x for x in payload["links"]}
    old, fresh = by_pair[(a.node_id, b.node_id)], by_pair[(b.node_id, a.node_id)]
    assert old["stale"] is True and 7200 <= old["age_s"] < 7200 + 120
    assert fresh["stale"] is False and fresh["age_s"] < 120
    out = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "meshforge.cli", "network"],
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")},
            "MESHFORGE_CLI__CONTROLLER_URL": f"https://127.0.0.1:{runtime.http_port}",
            "MESHFORGE_CLI__CA_FILE": str(ca_file()),
            "MESHFORGE_CLI__TOKEN": token_of(runtime),
            "MESHFORGE_DATA_DIR": str(tmp_path / "no-agent-here"),
            "XDG_CONFIG_HOME": str(tmp_path / "cli-config"),
            "COLUMNS": "250",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.count("(stale)") == 1 and "2.0h (stale)" in out.stdout


async def test_a_restarted_prober_keeps_the_interval_of_stored_links(
    runtime: ControllerRuntime, pair: tuple[ExecAgent, ExecAgent], pg_settings: Any
) -> None:
    """D111 interval across a restart, with the real table: a second prober
    (what a restarted controller builds) started on the links the first one
    measured sends nothing - both pairs are inside their 900 s interval -
    while one with an empty table probes within a gap."""
    a, b = pair
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if len((await _network(runtime))["links"]) >= 2:
            break
        await asyncio.sleep(0.2)

    class Sessions:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []

        def snapshot(self) -> list[Any]:
            return [
                SimpleNamespace(
                    node_id=n.node_id,
                    ended=False,
                    state=NodeState.ONLINE,
                    peer_endpoint="127.0.0.1:9",
                )
                for n in (a, b)
            ]

        def send_command(self, node_id: str, name: str, payload: Any) -> str:
            self.sent.append((node_id, payload.target_node_id))
            return "cmd"

    async def run_for(seconds: float) -> tuple[Sessions, NetworkProber]:
        sessions = Sessions()
        prober = NetworkProber(
            sessions,  # type: ignore[arg-type]
            NetworkStore(engine),
            PeerTokenIssuer(key, clock=time.time),
            NetworkSettings(probe_gap_s=0.1, probe_jitter=0.0),
            clock=time.monotonic,
        )
        task = asyncio.create_task(prober.run())
        await asyncio.sleep(seconds)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return sessions, prober

    key, _ = load_or_create_signing_key(runtime.settings.peer_key_path)
    engine = create_engine(pg_settings)
    try:
        sessions, prober = await run_for(1.5)
        assert sessions.sent == []
        for src, dst in ((a.node_id, b.node_id), (b.node_id, a.node_id)):
            last = prober.scheduler.last_probed(src, dst)
            assert last is not None and time.monotonic() - last < 120
        # control: the same prober on an empty matrix probes at once
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM network_link"))
        sessions, _ = await run_for(1.5)
        assert sessions.sent[:1] == [(a.node_id, b.node_id)]
    finally:
        await engine.dispose()


async def test_the_probe_route_checks_token_kind_holder_and_size(
    runtime: ControllerRuntime, pair: tuple[ExecAgent, ExecAgent]
) -> None:
    a, b = pair
    assert b.peer is not None
    key, _ = load_or_create_signing_key(runtime.settings.peer_key_path)
    now = time.time()
    good = sign_probe_token(key, requester=a.node_id, holder=b.node_id, now=now)
    other = sign_probe_token(key, requester=b.node_id, holder=a.node_id, now=now)
    blob = sign_peer_token(
        key,
        tree=Digest("ab" * 32),
        requester=a.node_id,
        holder=b.node_id,
        attempt_id=new_attempt_id(),
        now=now,
    )
    base = f"https://127.0.0.1:{b.peer.port}/v1/probe"
    async with httpx.AsyncClient(
        trust_env=False, verify=peer_client_context(), timeout=10
    ) as http:

        async def get(n: int, token: str | None) -> httpx.Response:
            headers = {} if token is None else {"authorization": f"Bearer {token}"}
            return await http.get(
                base,
                params={"bytes": str(n)},
                headers=headers,
                extensions={"sni_hostname": peer_server_name(b.node_id)},
            )

        assert (await get(10, None)).status_code == 401
        assert (await get(10, blob)).status_code == 403
        assert (await get(10, other)).status_code == 403
        assert (await get(8 * MIB + 1, good)).status_code == 400
        largest = await get(8 * MIB, good)  # the cap itself is served
        assert largest.status_code == 200 and len(largest.content) == 8 * MIB
        assert not any(largest.content)
        ok = await get(123_457, good)
        assert ok.status_code == 200 and ok.content == bytes(123_457)
        assert ok.headers["content-length"] == "123457"
        empty = await get(0, good)
        assert empty.status_code == 200 and empty.content == b""
    assert b.peer.stats.refused.get("probe_missing") == 1
    assert b.peer.stats.refused.get("probe_malformed") == 1
    assert b.peer.stats.refused.get("probe_wrong_holder") == 1
    assert b.peer.stats.refused.get("probe_too_big") == 1


async def test_store_folds_samples_as_ewma_and_restarts_a_stale_link(
    runtime: ControllerRuntime, pair: tuple[ExecAgent, ExecAgent], pg_settings: Any
) -> None:
    a, b = pair
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM network_link"))
        store = NetworkStore(engine)
        now = dt.datetime.now(dt.UTC)
        kw: dict[str, Any] = {"alpha": 0.3, "stale_after_s": 3600.0}
        await store.record(
            ProbeSample(a.node_id, b.node_id, 10.0, 100.0), now=now, **kw
        )
        await store.record(
            ProbeSample(a.node_id, b.node_id, 20.0, 200.0),
            now=now + dt.timedelta(seconds=5),
            **kw,
        )
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT rtt_ms, bw_bytes_s, samples FROM network_link "
                        "WHERE src_node_id = :s AND dst_node_id = :d"
                    ),
                    {"s": a.node_id, "d": b.node_id},
                )
            ).one()
        assert row[0] == pytest.approx(13.0) and row[1] == pytest.approx(130.0)
        assert row[2] == 2
        # an hour and more later the old value is not averaged in
        await store.record(
            ProbeSample(a.node_id, b.node_id, 50.0, 500.0),
            now=now + dt.timedelta(hours=2),
            **kw,
        )
        links, _ctl = await store.matrix(min_bytes=1, per_node=5)
        (link,) = [x for x in links if x.src_node_id == a.node_id]
        assert (link.rtt_ms, link.bw_bytes_s, link.samples) == (50.0, 500.0, 1)
        # a node that does not exist: dropped, not raised
        assert not await store.record(
            ProbeSample(a.node_id, "node_01J99999999999999999999999", 1.0, 1.0),
            now=now,
            **kw,
        )
    finally:
        await engine.dispose()


async def test_an_unreachable_target_is_reported_as_a_failure() -> None:
    sent: list[agent_pb2.NetProbeResult] = []
    reported = asyncio.Event()

    async def report(name: str, msg: Any) -> str:
        assert name == "net_probe_result"
        sent.append(msg)
        reported.set()
        return "id"

    service = ProbeService(
        ProbeSettings(request_timeout_s=2.0), report, node_id="node_x"
    )
    service.handle(
        agent_pb2.ProbeRequest(
            probe_id="prb1",
            target_node_id="node_y",
            endpoint="127.0.0.1:9",  # discard port: nothing listens
            token="t",
            rtt_samples=3,
            bytes=MIB,
            max_bytes=2 * MIB,
            fast_s=0.5,
        )
    )
    await asyncio.wait_for(reported.wait(), 10)
    await service.close()
    (result,) = sent
    assert not result.ok and result.error in ("unreachable", "timeout")
    assert result.probe_id == "prb1" and result.bytes == 0
