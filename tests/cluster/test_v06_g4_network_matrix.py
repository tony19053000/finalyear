"""V0.6 G4 (D111) cluster scenario: network probes between agent PROCESSES.

Three agents with peer servers on loopback and a controller whose prober runs
fast (gap 0.2 s, 1-2 MiB probes):

* every ordered pair (6) gets a measured link in ``GET /v1/network`` - real
  processes, real sockets, real probe tokens;
* a SIGKILLed agent's probes are given up (its node goes offline) and nothing
  hangs: the remaining pair keeps being measured;
* the matrix survives a controller SIGKILL + restart (it is in PostgreSQL),
  and the restarted prober folds new samples into it (``samples`` grows).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric, Timing

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(240)]

MIB = 1024 * 1024
TIMING = Timing(
    name="probes",
    heartbeat_interval_s=0.5,
    miss_allowance_s=0.5,
    lease_multiplier=6,
    controller_hello_timeout_s=3.0,
    shutdown_drain_s=2.0,
    agent_reconnect_base_s=0.1,
    agent_reconnect_cap_s=1.0,
    agent_hello_timeout_s=3.0,
)
NETWORK = {
    "probe_gap_s": 0.2,
    "probe_jitter": 0.0,
    "probe_bytes": 1 * MIB,
    "probe_max_bytes": 2 * MIB,
    "probe_budget_bytes_per_min": 1024 * MIB,
    "probe_interval_s": 10.0,
    "probe_timeout_s": 5.0,
}


def _network(fabric: Fabric) -> dict[str, Any] | None:
    payload = fabric.get("/v1/network")  # None while the controller restarts
    return dict(payload) if isinstance(payload, dict) else None


def _links(fabric: Fabric, want: int, within: float = 60) -> dict[tuple[str, str], Any]:
    deadline = time.monotonic() + within
    links: dict[tuple[str, str], Any] = {}
    while time.monotonic() < deadline:
        payload = _network(fabric)
        if payload is not None:
            links = {(x["src_node_id"], x["dst_node_id"]): x for x in payload["links"]}
            if len(links) >= want:
                return links
        time.sleep(0.2)
    fabric.fail(f"only {len(links)} of {want} links measured: {sorted(links)}")


def test_probes_fill_the_matrix_and_survive_kills(
    make_fabric: Callable[..., Fabric],
) -> None:
    fabric = make_fabric(
        agents=3,
        timing=TIMING,
        agent_extra={"peer": {"bind": "127.0.0.1", "port": 0}},
        controller_extra={"network": NETWORK},
    )
    ids = sorted(n.node_id for n in fabric.nodes)
    links = _links(fabric, 6)
    assert set(links) == {(a, b) for a in ids for b in ids if a != b}
    for link in links.values():
        assert link["bw_bytes_s"] > 1e6 and not link["stale"]
        assert link["source"] == "measured" and link["samples"] >= 1
        assert 0 <= link["age_s"] < 120  # measured in this run, aged by REST

    # SIGKILL one agent: its node goes offline, the others keep probing
    victim = fabric.nodes[0]
    victim.kill()
    # only once it is OFFLINE are its probes in flight given up (before that a
    # probe naming it may legitimately still be waiting for its timeout)
    fabric.wait_state(victim, "offline", within=30.0)
    survivors = [n.node_id for n in fabric.nodes[1:]]
    pair = (survivors[0], survivors[1])
    before = links[pair]["samples"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        payload = _network(fabric)
        now = (
            {(x["src_node_id"], x["dst_node_id"]): x for x in payload["links"]}
            if payload
            else {}
        )
        if pair in now and now[pair]["samples"] > before:
            assert now[pair]["measured_at"] > links[pair]["measured_at"]
            break
        time.sleep(0.3)
    else:
        fabric.fail(f"pair {pair} not probed again after the kill")
    payload = _network(fabric)
    assert payload is not None
    assert all(
        victim.node_id not in (f["src_node_id"], f["dst_node_id"])
        for f in payload["prober"]["in_flight"]
    ), payload["prober"]
    assert payload["prober"]["enabled"] is True
    assert (
        payload["prober"]["budget_used_bytes"] <= NETWORK["probe_budget_bytes_per_min"]
    )

    # controller SIGKILL + restart: the matrix is still there and grows
    measured = _links(fabric, 6)
    fabric.kill_controller()
    fabric.restart_controller()
    after_restart = _links(fabric, 6, within=60)
    assert set(after_restart) == set(measured)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        payload = _network(fabric)
        if payload is not None:
            now = {(x["src_node_id"], x["dst_node_id"]): x for x in payload["links"]}
            if now[pair]["samples"] > measured[pair]["samples"]:
                break
        time.sleep(0.3)
    else:
        fabric.fail("the restarted controller never probed again")
