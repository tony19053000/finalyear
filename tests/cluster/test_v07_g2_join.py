"""V0.7 G2 cluster (0.7.3/0.7.4): two real agent processes join with two codes.

A real controller process (CA made by `meshforge-controller fabric create`), two
agents that are **not** enrolled and not started (V0.7 G3: an agent needs its
certificate to connect at all), two invites over REST, then `meshforge-agent
join -` as real subprocesses with the code on stdin; then the agents start and
connect over mutual TLS. The controller lists both nodes with their own
certificate, each bound to that agent's key; the code appears in no log of any
process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")

from mf_cluster import MakeFabric

pytestmark = [pytest.mark.docker]


def test_two_agents_join_with_two_codes(make_fabric: MakeFabric) -> None:
    fabric = make_fabric(
        agents=2, fabric_ca=True, enroll_agents=False, start_agents=False
    )
    assert fabric.enroll_target, "no enrolment listener"
    codes: list[str] = []
    for _ in fabric.nodes:
        made = fabric.call("POST", "/v1/fabric/invites", json={"ttl_s": 300})
        assert made.status_code == 201, made.text
        codes.append(made.json()["code"])
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env["PYTHONUTF8"] = "1"
    for node, code in zip(fabric.nodes, codes, strict=True):
        done = subprocess.run(
            [sys.executable, "-m", "meshforge.agent", "join", "-",
             "--config", str(node.config)],
            input=code + "\n", env=env, capture_output=True, text=True,
            timeout=60, check=False,
        )  # fmt: skip
        assert done.returncode == 0, done.stderr
        assert code not in done.stdout + done.stderr
        shown = subprocess.run(
            [sys.executable, "-m", "meshforge.agent", "identity", "show", "--json",
             "--config", str(node.config)],
            env=env, capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip
        facts = json.loads(shown.stdout)
        assert facts["enrolled"] is True
        node.node_id = facts["node_id"]
        node.key_fingerprint = facts["key_fingerprint_sha256"]  # type: ignore[attr-defined]
    joined = {node.node_id for node in fabric.nodes}
    fabric.start_agents()  # mutual TLS with the certificates just issued
    assert {node.node_id for node in fabric.nodes} == joined
    nodes = fabric.nodes_by_id()
    assert nodes is not None
    certs = []
    for node in fabric.nodes:
        view = nodes[node.node_id]
        assert view["connected"] is True
        cert = view["certificate"]
        assert cert is not None, view
        assert cert["public_key_fingerprint_sha256"] == node.key_fingerprint  # type: ignore[attr-defined]
        certs.append(cert["serial"])
    assert len(set(certs)) == 2
    invites = fabric.call("GET", "/v1/fabric/invites").json()["invites"]
    assert {i["used_by_node"] for i in invites} == {n.node_id for n in fabric.nodes}
    logs = fabric.workdir / "logs"
    text = "".join(
        p.read_text("utf-8", "replace") for p in logs.iterdir() if p.is_file()
    )
    for code in codes:
        for group in code.split("-")[1:-1]:
            assert group not in text
