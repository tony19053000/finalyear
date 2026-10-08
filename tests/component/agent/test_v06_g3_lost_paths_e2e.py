"""V0.6 G3 deliverable 2: every LOST path retries (D110) - real controller,
real agents (client + executor in this loop), real PostgreSQL.

* **OFFLINE** (lease expiry): the agent running the job dies without a
  Goodbye -> SUSPECT -> OFFLINE -> attempt ``LOST(node_offline)`` -> the job
  is retried on the other agent and succeeds;
* **acked-not-held Reconcile**: the agent dies and comes straight back (same
  identity) before its lease ends -> its Reconcile holds nothing -> ``LOST
  (agent_lost_attempt)`` -> retried; the only node was excluded but nothing
  else could ever run the job, so the exclusion is lifted and it runs there;
* **orphan sweep**: an attempt on a node that never connects to this
  controller -> ``LOST(node_not_connected)`` after ``reconnect_grace_s`` ->
  retried on a real agent, the dead node excluded (``retry_excluded``);
* a **log follower** (the WebSocket ``compute run -f`` uses) stays across the
  retry: attempt 1's output, one ``retry`` notice frame, attempt 2's output
  from offset 0, then the end frame with the job's final state.

A dying agent here = its session task cancelled (no Goodbye) and its executor
closed (workloads killed); the first run of each job sleeps long, a retry
finds the marker file and finishes at once.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_jobs_harness import token_of

from meshforge.common.ids import new_attempt_id, new_node_id, new_session_id
from meshforge.controller.jobs.execution_store import Dispatch, ExecutionStore
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings, LivenessSettings
from meshforge.controller.store.engine import create_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

# OFFLINE 3 s after the last heartbeat: quick, but far above a loaded
# machine's loop stall (the agents run in this event loop).
LIVENESS = LivenessSettings(
    heartbeat_interval_s=0.2, miss_allowance_s=1.0, lease_multiplier=15
)
WORKLOAD = """\
import os, sys, time
marker, counter = sys.argv[1], sys.argv[2]
with open(counter, "a") as fh:
    fh.write(os.environ["MESHFORGE_ATTEMPT_ID"] + "\\n")
print("attempt", os.environ["MESHFORGE_ATTEMPT_ID"], flush=True)
if not os.path.exists(marker):
    open(marker, "w").close()
    time.sleep(60)
"""


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "reconnect_grace_s": 1.0},
        liveness=LIVENESS,
    )


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=10,
    ) as client:
        yield client


class Agents:
    def __init__(self, target: str, root: Path) -> None:
        self.target, self.root = target, root
        self.running: list[ExecAgent] = []

    async def start(self, name: str) -> ExecAgent:
        agent = await ExecAgent.start(self.target, self.root / name)
        self.running.append(agent)
        return agent

    async def crash(self, agent: ExecAgent) -> None:
        """Gone without a Goodbye: the session dies, then the executor (and
        every workload it runs)."""
        agent.task.cancel()
        await asyncio.gather(agent.task, return_exceptions=True)
        await agent.executor.close()
        for extra in (agent.peer, agent.preparer, agent.gc, agent.cache):
            if extra is not None:
                with contextlib.suppress(Exception):
                    await extra.close()
        self.running.remove(agent)


@pytest_asyncio.fixture
async def agents(grpc_target: str, tmp_path: Path) -> AsyncIterator[Agents]:
    made = Agents(grpc_target, tmp_path)
    yield made
    for agent in list(made.running):
        await agent.stop()


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "main.py").write_text(WORKLOAD)
    return project


def _manifest(tmp_path: Path) -> dict[str, Any]:
    return {
        "name": "g3-lost",
        "entrypoint": [
            "python",
            "main.py",
            str(tmp_path / "marker"),
            str(tmp_path / "count.txt"),
        ],
        "retry": {"backoff_s": 0},
    }


async def _decisions(http: httpx.AsyncClient, job_id: str) -> list[dict[str, Any]]:
    resp = await http.get(f"/v1/jobs/{job_id}/decisions")
    assert resp.status_code == 200, resp.text
    return list(resp.json()["decisions"])


def _arrows(job: dict[str, Any]) -> list[tuple[str | None, str]]:
    return [(t["from_state"], t["to_state"]) for t in job["transitions"]]


RETRY = [("running", "retrying"), ("retrying", "planning"), ("planning", "queued")]


async def test_offline_by_lease_expiry_retries_on_the_other_node(
    http: httpx.AsyncClient, agents: Agents, tmp_path: Path
) -> None:
    a = await agents.start("a")
    b = await agents.start("b")
    job_id = await submit(http, _project(tmp_path), None, _manifest(tmp_path))
    running = await wait_job(http, job_id, states=frozenset({"running"}))
    first_node = running["attempts"][0]["node_id"]
    victim, survivor = (a, b) if first_node == a.node_id else (b, a)
    await agents.crash(victim)
    job = await wait_job(http, job_id, timeout_s=30)
    assert job["state"] == "succeeded", job
    first, second = job["attempts"]
    assert (first["node_id"], first["state"], first["failure_type"]) == (
        victim.node_id,
        "lost",
        "node_lost",
    )
    assert first["detail"] == "node_offline"
    assert (second["node_id"], second["state"]) == (survivor.node_id, "done")
    assert second["fencing_token"] == 2
    arrows = _arrows(job)
    assert arrows[arrows.index(RETRY[0]) : arrows.index(RETRY[0]) + 3] == RETRY
    assert (tmp_path / "count.txt").read_text().split() == [
        first["attempt_id"],
        second["attempt_id"],
    ]


async def test_acked_but_not_held_at_reconcile_retries_on_the_same_lone_node(
    http: httpx.AsyncClient,
    agents: Agents,
    tmp_path: Path,
    runtime: ControllerRuntime,
) -> None:
    a = await agents.start("a")
    job_id = await submit(http, _project(tmp_path), None, _manifest(tmp_path))
    await wait_job(http, job_id, states=frozenset({"running"}))
    await agents.crash(a)
    back = await agents.start("a")  # same data dir: same node, new process
    assert back.node_id == a.node_id
    job = await wait_job(http, job_id, timeout_s=30)
    assert job["state"] == "succeeded", job
    first, second = job["attempts"]
    assert (first["state"], first["detail"]) == ("lost", "agent_lost_attempt")
    assert (second["node_id"], second["state"]) == (a.node_id, "done")
    # the only node was excluded for node_lost, but no other node could ever
    # run the job: the exclusion was lifted (soft, D110) and it ran there
    (dispatched,) = [
        d
        for d in await _decisions(http, job_id)
        if d["outcome"] == "dispatched" and d["attempt_no"] == 2
    ]
    assert dispatched["chosen_node_id"] == a.node_id
    assert runtime.dispatcher is not None
    assert runtime.dispatcher.stats.retries_scheduled >= 1


async def test_orphan_sweep_loses_an_unconnected_node_s_attempt_and_retries_elsewhere(
    http: httpx.AsyncClient,
    agents: Agents,
    tmp_path: Path,
    runtime: ControllerRuntime,
    pg_settings: Any,
) -> None:
    engine = create_engine(pg_settings)
    try:
        ghost = str(new_node_id())  # enrolled, then never connects to us
        await Registry(engine).upsert_node_on_hello(
            node_id=ghost,
            hostname="ghost",
            os="linux",
            platform="x86_64",
            hardware={"capabilities": ["exec", "exec-subprocess"]},
            session_epoch=1,
            session_id=str(new_session_id()),
            state=NodeState.ONLINE,
            cpu_slots=1,
            gpu_slots=0,
        )
        (tmp_path / "marker").touch()  # the ghost never ran it: no long first run
        job_id = await submit(http, _project(tmp_path), None, _manifest(tmp_path))
        store = ExecutionStore(engine)
        # place it on the ghost ourselves (the runtime's planner never would:
        # the ghost has no session) - as if a previous controller had
        results = await store.dispatch_planned(
            lambda job, _fabric: Dispatch(
                attempt_id=str(new_attempt_id()), start_snapshot={}, node_id=ghost
            ),
            limit=10,
            max_per_node=8,
            now=dt.datetime.now(dt.UTC),
            aging_s=600.0,
        )
        assert [r.job.id for r in results] == [job_id]
        b = await agents.start("b")
        job = await wait_job(http, job_id, timeout_s=30)
    finally:
        await engine.dispose()
    assert job["state"] == "succeeded", job
    first, second = job["attempts"]
    assert (first["node_id"], first["state"], first["detail"]) == (
        ghost,
        "lost",
        "node_not_connected",
    )
    assert (second["node_id"], second["state"]) == (b.node_id, "done")
    assert ("preparing", "retrying") in _arrows(job)  # it never started
    (dispatched,) = [
        d
        for d in await _decisions(http, job_id)
        if d["outcome"] == "dispatched" and d["attempt_no"] == 2
    ]
    ghost_row = next(c for c in dispatched["candidates"] if c["node_id"] == ghost)
    # the ghost is ONLINE in the database but has no session; its exclusion
    # or its missing session - either way never chosen, and said why
    assert ghost_row["reject_code"] in ("retry_excluded", "no_session")


async def _frames(url: str, token: str) -> list[dict[str, Any]]:
    from websockets.asyncio.client import connect

    out: list[dict[str, Any]] = []
    async with connect(
        url, additional_headers={"Authorization": f"Bearer {token}"}
    ) as ws:
        async for raw in ws:
            frame = json.loads(raw)
            out.append(frame)
            if frame.get("end"):
                return out
    raise AssertionError(f"closed without an end frame: {out}")


async def test_a_log_follower_stays_across_a_retry(
    http: httpx.AsyncClient,
    agents: Agents,
    tmp_path: Path,
    runtime: ControllerRuntime,
) -> None:
    a = await agents.start("a")
    job_id = await submit(http, _project(tmp_path), None, _manifest(tmp_path))
    url = f"wss://127.0.0.1:{runtime.http_port}/v1/jobs/{job_id}/logs/ws?stream=stdout"
    follower = asyncio.create_task(_frames(url, token_of(runtime)))
    job = await wait_job(http, job_id, states=frozenset({"running"}))
    first = job["attempts"][0]["attempt_id"]
    for _ in range(100):  # attempt 1's line has reached the controller
        logs = await http.get(f"/v1/jobs/{job_id}/logs", params={"stream": "stdout"})
        if first.encode() in logs.content:
            break
        await asyncio.sleep(0.05)
    await agents.crash(a)
    await agents.start("a")
    frames = await asyncio.wait_for(follower, 40)
    end = frames[-1]
    assert end["end"] is True and end["state"] == "succeeded", end
    notices = [f for f in frames if f.get("retry")]
    assert len(notices) == 1, frames
    (notice,) = notices
    assert notice["previous_attempt_id"] == first
    assert notice["previous_state"] == "lost"
    assert notice["previous_failure_type"] == "node_lost"
    assert notice["fencing_token"] == 2
    split = frames.index(notice)
    before = b"".join(
        base64.b64decode(f["data"]) for f in frames[:split] if "data" in f
    )
    after = [f for f in frames[split + 1 :] if "data" in f]
    assert first.encode() in before
    assert after and after[0]["offset"] == 0  # the new attempt's log from its start
    assert notice["attempt_id"].encode() in b"".join(
        base64.b64decode(f["data"]) for f in after
    )
