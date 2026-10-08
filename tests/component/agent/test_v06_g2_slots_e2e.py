"""V0.6 G2 (D105) end to end: concurrent automatic jobs never exceed any node's
CPU or GPU slots - also across a controller restart.

A real controller (PostgreSQL, gRPC, REST, dispatcher + planner) and three real
agents (subprocess runtime) in this loop:

* A - 2 CPU slots;  B - 1 CPU slot;  C - 1 CPU slot + 1 GPU slot (one *fake*
  4 GiB GPU: the subprocess runtime only sets CUDA_VISIBLE_DEVICES, so GPU
  placement runs on a CPU-only machine).

Ten jobs at once (8 CPU, 2 ``gpu: required``), each sleeping ~1 s. A monitor
reads the live attempts per (node, slot) from PostgreSQL every 25 ms - the
same truth the dispatcher counts - and records the peak. The controller is
stopped and a fresh one started on the same ports and database while the first
wave runs: the new process derives slot use from the attempts it finds, so it
must not over-book the nodes whose jobs are still running.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter
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
from sqlalchemy import text

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

SLEEPER = """\
import sys, time
time.sleep(float(sys.argv[1]))
print("slept", flush=True)
"""


def _settings(pg: DatabaseSettings, **binds: str) -> ControllerSettings:
    return make_controller_settings(
        pg,
        jobs={"dispatch_interval_s": 0.05},
        planner={"default_policy": "least_loaded"},
        **tolerant_liveness(),
        **binds,
    )


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return _settings(pg_settings)


def _http(runtime: ControllerRuntime) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    )


@pytest_asyncio.fixture
async def fabric(
    grpc_target: str, tmp_path: Path
) -> AsyncIterator[dict[str, ExecAgent]]:
    agents = {
        "A": await ExecAgent.start(
            grpc_target, tmp_path / "a", execution={"max_cpu_jobs": 2}
        ),
        "B": await ExecAgent.start(
            grpc_target, tmp_path / "b", execution={"max_cpu_jobs": 1}
        ),
        "C": await ExecAgent.start(
            grpc_target,
            tmp_path / "c",
            execution={"max_cpu_jobs": 1, "max_gpu_jobs": 1},
            fake_gpus=1,
        ),
    }
    yield agents
    for agent in agents.values():
        await agent.stop()


class Monitor:
    """Peak live attempts per (node, slot), read from PostgreSQL."""

    def __init__(self, pg: DatabaseSettings) -> None:
        self.engine = create_engine(pg)
        self.peak: Counter[tuple[str, str]] = Counter()
        self.samples = 0
        self._stop = asyncio.Event()
        self.task = asyncio.create_task(self._run(), name="slot-monitor")

    async def _run(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):  # a sample lost to a restart
                async with self.engine.connect() as conn:
                    rows = await conn.execute(
                        text(
                            "SELECT node_id, coalesce(slot, 'cpu'), count(*) "
                            "FROM attempt WHERE state IN ('assigned', 'staging', "
                            "'env_ready', 'running', 'uploading') "
                            "GROUP BY node_id, coalesce(slot, 'cpu')"
                        )
                    )
                    for node_id, slot, count in rows.all():
                        key = (str(node_id), str(slot))
                        self.peak[key] = max(self.peak[key], int(count))
                    self.samples += 1
            await asyncio.sleep(0.025)

    async def close(self) -> None:
        self._stop.set()
        await self.task
        await self.engine.dispose()


async def test_concurrent_auto_jobs_never_exceed_slots_across_a_restart(
    runtime: ControllerRuntime,
    fabric: dict[str, ExecAgent],
    pg_settings: DatabaseSettings,
    tmp_path: Path,
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(SLEEPER)
    caps = {
        (fabric["A"].node_id, "cpu"): 2,
        (fabric["B"].node_id, "cpu"): 1,
        (fabric["C"].node_id, "cpu"): 1,
        (fabric["C"].node_id, "gpu"): 1,
    }
    monitor = Monitor(pg_settings)
    jobs: list[tuple[str, bool]] = []
    try:
        async with _http(runtime) as http:
            for i in range(10):
                gpu = i in (3, 7)
                manifest: dict[str, Any] = {
                    "name": f"slot-{i}",
                    "entrypoint": ["python", "main.py", "1.0"],
                }
                if gpu:
                    manifest["resources"] = {"gpu": "required"}
                jobs.append((await submit(http, project, None, manifest), gpu))

            async def running() -> int:
                listed = (await http.get("/v1/jobs?state=running")).json()
                return int(listed["count"])

            for _ in range(400):
                if await running() >= 3:
                    break
                await asyncio.sleep(0.025)
        # Restart while the first wave runs.
        grpc_port, http_port = runtime.grpc_port, runtime.http_port
        await runtime.stop()
        await asyncio.sleep(0.3)
        fresh_settings = _settings(
            pg_settings,
            grpc_bind=f"127.0.0.1:{grpc_port}",
            http_bind=f"127.0.0.1:{http_port}",
        )
        async with (
            ControllerRuntime.start(fresh_settings) as fresh,
            _http(fresh) as http,
        ):
            finals = [await wait_job(http, job, timeout_s=90) for job, _ in jobs]
    finally:
        await monitor.close()
    for (job, gpu), final in zip(jobs, finals, strict=True):
        assert final["state"] == "succeeded", final
        (attempt,) = final["attempts"]
        if gpu:
            assert attempt["node_id"] == fabric["C"].node_id, (job, attempt)
    assert monitor.samples > 20
    for key, peak in monitor.peak.items():
        assert key in caps, f"an attempt held an unexpected slot: {key}"
        assert peak <= caps[key], (key, peak, dict(monitor.peak))
    # the slots were really used concurrently, not one job at a time
    assert monitor.peak[(fabric["A"].node_id, "cpu")] == 2
    assert monitor.peak[(fabric["C"].node_id, "gpu")] == 1
