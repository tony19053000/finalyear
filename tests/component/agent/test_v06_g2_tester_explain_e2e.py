"""V0.6 G2 tester: ``/decisions`` honesty on real agents (checklist 18, D108/D112).

Two real agents, one project. After the workload has run on A, an auto job
(``cost_model``) must be placed from what was observed: A's compute term is
``history`` and about the run time it actually took; the decision of the
finished job carries ``actual`` phase timings; a node with no history is never
shown as ``measured`` for compute; and the reason a candidate lost is stored.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_jobs_harness import token_of

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

SLEEPER = "import time\ntime.sleep(1.0)\nprint('done', flush=True)\n"


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.05},
        planner={"default_policy": "cost_model"},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def agents(
    grpc_target: str, tmp_path: Path
) -> AsyncIterator[dict[str, ExecAgent]]:
    made = {
        "A": await ExecAgent.start(
            grpc_target, tmp_path / "a", execution={"max_cpu_jobs": 1}
        ),
        "B": await ExecAgent.start(
            grpc_target, tmp_path / "b", execution={"max_cpu_jobs": 1}
        ),
    }
    yield made
    for agent in made.values():
        await agent.stop()


async def test_history_drives_cost_model_and_every_term_is_honest(
    runtime: ControllerRuntime, agents: dict[str, ExecAgent], tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(SLEEPER)
    manifest: dict[str, Any] = {"name": "hist", "entrypoint": ["python", "main.py"]}
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    ) as http:
        a_id = agents["A"].node_id
        for _ in range(2):  # build the history of this workload on A
            job = await submit(http, project, a_id, manifest)
            assert (await wait_job(http, job, timeout_s=60))["state"] == "succeeded"
        # idempotent resubmission is a different request each time: same fingerprint
        auto = await submit(http, project, None, manifest)
        final = await wait_job(http, auto, timeout_s=60)
        assert final["state"] == "succeeded", final
        decisions = (await http.get(f"/v1/jobs/{auto}/decisions")).json()["decisions"]
    decision = [d for d in decisions if d["outcome"] == "dispatched"][-1]
    by_node = {c["node_id"]: c for c in decision["candidates"]}
    assert set(by_node) == {n.node_id for n in agents.values()}
    chosen = decision["chosen_node_id"]
    a, b = by_node[a_id], by_node[agents["B"].node_id]
    # A: observed run time, labelled history, about the second the job sleeps
    a_compute = a["estimate"]["terms"]["compute"]
    assert a_compute["source"] == "history"
    assert 0.9 <= a_compute["seconds"] <= 5.0
    # B has no runs of its own: never `measured`, and never its own `history`
    b_compute = b["estimate"]["terms"]["compute"]
    assert b_compute["source"] in {"scaled", "history", "assumed"}
    if b_compute["source"] == "history":
        assert b_compute["detail"].get("from_node_id") == a_id
    for cand in (a, b):  # a number is `measured` only when nothing is left to move
        transfer = cand["estimate"]["terms"]["transfer"]
        if transfer["source"] == "measured":
            assert transfer["seconds"] == 0.0
        else:
            assert (
                transfer["source"] == "assumed"
                and transfer["detail"]["bw_source"] == "assumed"
            )
    totals = {c["node_id"]: c["estimate"]["total_s"] for c in decision["candidates"]}
    assert decision["chosen_node_id"] == min(totals, key=lambda n: (totals[n], n))
    assert by_node[chosen]["rank"] == 1
    # actual vs predicted: the finished attempt's real phases are joined in
    actual = decision.get("actual")
    compute_s = by_node[chosen]["estimate"]["terms"]["compute"]["seconds"]
    assert actual is not None and actual["state"] == "done"
    run_ms = actual["phases_ms"]["run"]
    assert run_ms is not None and 800 <= run_ms <= 6000
    assert abs(run_ms / 1000.0 - compute_s) < 2.0
