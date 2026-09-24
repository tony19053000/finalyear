"""Cluster scenario k: graceful controller stop and restart on real processes.

Not in the brief's a-j list; added because the harness's ``stop_controller``
path (SIGINT to the controller) must itself be exercised for real, and because
it proves D19's other clean end on processes: a graceful controller shutdown
writes every live node OFFLINE with ``controller_shutdown`` (not left ONLINE for
the start-up sweep), while the agents keep running and come back on restart.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")

from mf_cluster import FAST, START_TIMEOUT_S, Fabric, MakeFabric

pytestmark = [pytest.mark.docker]

RecordTiming = Callable[[str, float, "float | None", str], None]


def test_k_controller_sigint_writes_offline_then_restart_brings_all_back(
    make_fabric: MakeFabric, record_timing: RecordTiming
) -> None:
    fabric: Fabric = make_fabric(agents=5, timing=FAST)
    pids = [n.pid for n in fabric.nodes]
    baseline = fabric.transition_counts()
    sessions = {}
    for node in fabric.nodes:
        detail = fabric.node_detail(node)
        assert detail is not None
        sessions[node.node_id] = detail["last_session_id"]

    fabric.stop_controller()
    assert fabric.controller is not None and fabric.controller.returncode == 0
    for node in fabric.nodes:
        # Written by the stopping controller itself, before it exited.
        assert fabric.arrows(node, baseline) == [
            ("online", "offline", "stream_closed")
        ], fabric.arrows(node, baseline)

    t1 = fabric.restart_controller()
    back_s = fabric.wait_all_connected(within=START_TIMEOUT_S, since=t1)
    for node in fabric.nodes:
        old = fabric.session(node, sessions[node.node_id])
        assert old["end_reason"] == "controller_shutdown", old
    assert [n.pid for n in fabric.nodes] == pids
    assert all(n.restarts == 0 for n in fabric.nodes)
    record_timing("k controller SIGINT+start -> all ONLINE", back_s, None, FAST.name)
