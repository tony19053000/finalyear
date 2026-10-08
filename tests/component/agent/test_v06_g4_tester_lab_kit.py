"""V0.6 G4 tester round 1: the 0.6.13 lab kit run for real on one machine.

A real controller (PostgreSQL, gRPC, REST) and two real executing agents; the
lab scripts are the ones a human runs: ``lab/sched_check.py submit`` uploads the
ledger workload and submits mixed jobs, ``wait`` polls, ``check`` reads the
ledger the jobs left in ``/tmp/mf-lab/v06-ledger``. Then the ledger is
tampered with and the verdict must change.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_exec_harness import ExecAgent
from mf_fabric import ca_file
from mf_jobs_harness import token_of

from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "lab" / "sched_check.py"
LEDGER = Path("/tmp/mf-lab/v06-ledger")


@pytest.fixture
def clean_ledger() -> Iterator[None]:
    if sys.platform == "win32":
        pytest.skip("the Linux ledger path")
    shutil.rmtree(LEDGER, ignore_errors=True)
    yield
    shutil.rmtree(LEDGER, ignore_errors=True)


@pytest_asyncio.fixture
async def pair(
    grpc_target: str, tmp_path: Path, clean_ledger: None
) -> AsyncIterator[tuple[ExecAgent, ExecAgent]]:
    a = await ExecAgent.start(grpc_target, tmp_path / "a")
    b = await ExecAgent.start(grpc_target, tmp_path / "b")
    try:
        yield a, b
    finally:
        await a.stop()
        await b.stop()


def _truncate_ledgers() -> None:
    for f in LEDGER.glob("*.txt"):
        f.write_text("")


def tool(runtime: ControllerRuntime, tmp_path: Path, *args: str) -> Any:
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")},
        "MESHFORGE_CLI__CONTROLLER_URL": f"https://127.0.0.1:{runtime.http_port}",
        "MESHFORGE_CLI__CA_FILE": str(ca_file()),
        "MESHFORGE_CLI__TOKEN": token_of(runtime),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=200,
        check=False,
        cwd=ROOT,
    )


async def test_the_kit_submits_waits_and_checks_a_real_run(
    runtime: ControllerRuntime, pair: tuple[ExecAgent, ExecAgent], tmp_path: Path
) -> None:
    run = tmp_path / "run.json"
    sub = await asyncio.to_thread(
        tool, runtime, tmp_path, "submit", "--n", "6", "--sleep", "1-2",
        "--no-fail", "--seed", "3", "--out", str(run),
    )  # fmt: skip
    assert sub.returncode == 0, sub.stdout + sub.stderr
    assert len(json.loads(run.read_text())["jobs"]) == 6
    done = await asyncio.to_thread(
        tool, runtime, tmp_path, "wait", str(run), "--timeout", "150"
    )
    assert done.returncode == 0, done.stdout + done.stderr
    report_path = tmp_path / "report.json"
    check = await asyncio.to_thread(
        tool, runtime, tmp_path, "check", str(run), "--ledger", str(LEDGER),
        "--out", str(report_path),
    )  # fmt: skip
    assert check.returncode == 0, check.stdout + check.stderr
    report = json.loads(report_path.read_text())
    assert report["criterion_1"] == "PASS" or report["criterion_1"] is True
    assert report["states"] == {"succeeded": 6}
    assert not report["lost"] and not report["duplicate_commits"]
    assert not report["duplicate_executions_under_kills"]
    assert not report["committed_without_ledger_evidence"]

    # tamper: drop every ledger line -> a job that succeeded has no evidence
    await asyncio.to_thread(_truncate_ledgers)
    bad = await asyncio.to_thread(
        tool, runtime, tmp_path, "check", str(run), "--ledger", str(LEDGER)
    )
    verdict = bad.stdout
    assert "committed_without_ledger_evidence" in verdict
    assert json.loads(verdict)["committed_without_ledger_evidence"], verdict
    # fix round 1 (D132, T-12): missing evidence is INCONCLUSIVE, never a pass
    assert json.loads(verdict)["criterion_1"] == "INCONCLUSIVE", verdict
    assert bad.returncode == 4, bad.stdout + bad.stderr
