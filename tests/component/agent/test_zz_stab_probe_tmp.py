"""TEMPORARY probe (not committed): an event-loop stall vs the harness lease."""
from __future__ import annotations
import time
from pathlib import Path
from typing import Any
import httpx, pytest
from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_jobs_harness import token_of
pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

@pytest.mark.parametrize("tolerant", [False, True])
async def test_probe(pg_settings: Any, tmp_path: Path, tolerant: bool) -> None:
    from meshforge.controller.server import ControllerRuntime
    extra = tolerant_liveness() if tolerant else {}
    s = make_controller_settings(pg_settings, jobs={"dispatch_interval_s": 0.1}, **extra)
    async with ControllerRuntime.start(s) as rt:
        agent = await ExecAgent.start(f"127.0.0.1:{rt.grpc_port}", tmp_path / "d")
        try:
            p = tmp_path / "proj"; p.mkdir()
            (p / "main.py").write_text("import time\ntime.sleep(3)\n")
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{rt.http_port}",
                headers={"authorization": f"Bearer {token_of(rt)}"}, timeout=30) as http:
                job_id = await submit(http, p, agent.node_id, {"name": "p", "entrypoint": ["python", "main.py"]})
                await wait_job(http, job_id, states=frozenset({"running"}))
                time.sleep(0.8)  # one descheduled/blocked moment of the shared loop
                job = await wait_job(http, job_id)
                print("RESULT", tolerant, job["state"], job["failure_type"])
                assert job["state"] == "succeeded", job
        finally:
            await agent.stop()
