"""G7-A (0.3.4, item 7): the agent's background ScoreService and its runtime hook.

A fake session (state + wait_for_state) and a fake runner: no child process, no
controller, sleeps <= 50 ms. The real child and a real session are in
tests/component/agent/test_benchmarks_*.py.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from mf_bench_fakes import make_facts, make_gpu, make_scoreset
from structlog.testing import capture_logs

from meshforge.agent import runtime
from meshforge.agent.benchmarks.cache import (
    fingerprint,
    fingerprint_inputs,
    save_cache,
    scorer_lock,
)
from meshforge.agent.benchmarks.model import ScoreSet
from meshforge.agent.benchmarks.runner import HostFacts, RunnerConfig
from meshforge.agent.benchmarks.service import ScoreService
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import AgentSection, AgentSettings, ScoreSettings

pytestmark = pytest.mark.unit

FACTS = make_facts(gpus=(make_gpu(0),))
BACKENDS: dict[str, str | None] = {"numpy": "2.5.3", "torch": None}
FP = fingerprint(fingerprint_inputs(FACTS, BACKENDS))
FAST = ScoreSettings(start_delay_s=0.0, cache_poll_s=0.05, budget_s=5, case_timeout_s=1)


class FakeSession:
    def __init__(self, state: AgentState = AgentState.ONLINE) -> None:
        self.state = state
        self.node_id = "node_01J8Z3K5Q9V6W2X4Y7A0B1C2D3"
        self.session_id = "sess_01J8Z3K5Q9V6W2X4Y7A0B1C2D3"
        self.changed = asyncio.Event()

    def set(self, state: AgentState) -> None:
        self.state = state
        self.changed.set()

    async def wait_for_state(self, *states: AgentState, timeout_s: float) -> AgentState:
        while self.state not in states:
            self.changed.clear()
            await asyncio.wait_for(self.changed.wait(), timeout_s)
        return self.state


class FakeRunner:
    runs = 0
    hang = False

    def __init__(
        self,
        config: RunnerConfig,
        host: HostFacts,
        *,
        fingerprint: str,
        log_fields: Any,
    ) -> None:
        self.fingerprint = fingerprint

    async def run(self) -> ScoreSet:
        type(self).runs += 1
        if type(self).hang:
            await asyncio.Event().wait()
        return make_scoreset(fingerprint=self.fingerprint)


@pytest.fixture(autouse=True)
def _reset() -> None:
    FakeRunner.runs = 0
    FakeRunner.hang = False


def _service(
    data_dir: Path,
    session: FakeSession,
    reports: list[ScoreSet] | None = None,
    **kw: Any,
) -> ScoreService:
    async def report(scores: ScoreSet) -> None:
        assert reports is not None
        reports.append(scores)

    return ScoreService(
        kw.pop("settings", FAST),
        data_dir,
        session,
        kw.pop("facts", FACTS),
        report=report if reports is not None else None,
        runner_factory=FakeRunner,
        backends=lambda: dict(BACKENDS),
        **kw,
    )


async def _until(predicate: Any, timeout_s: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        assert loop.time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_nothing_happens_before_the_first_online(tmp_path: Path) -> None:
    session = FakeSession(AgentState.CONNECTING)
    reports: list[ScoreSet] = []
    task = asyncio.create_task(_service(tmp_path, session, reports).run())
    await asyncio.sleep(0.05)
    assert FakeRunner.runs == 0 and reports == []
    session.set(AgentState.ONLINE)
    await _until(lambda: len(reports) == 1)
    assert FakeRunner.runs == 1
    assert reports[0].fingerprint == FP
    assert (tmp_path / "scores.json").is_file()
    await _stop(task)


@pytest.mark.asyncio
async def test_a_valid_cache_means_no_run_and_no_duplicate_report(
    tmp_path: Path,
) -> None:
    save_cache(tmp_path, make_scoreset(fingerprint=FP), {})
    reports: list[ScoreSet] = []
    task = asyncio.create_task(_service(tmp_path, FakeSession(), reports).run())
    await asyncio.sleep(0.15)  # three poll periods
    assert FakeRunner.runs == 0 and reports == []
    await _stop(task)


@pytest.mark.asyncio
async def test_a_stale_cache_is_rescored_once(tmp_path: Path) -> None:
    save_cache(tmp_path, make_scoreset(fingerprint="b" * 64), {})
    reports: list[ScoreSet] = []
    task = asyncio.create_task(_service(tmp_path, FakeSession(), reports).run())
    await _until(lambda: len(reports) == 1)
    await asyncio.sleep(0.15)  # the poller must not report our own write again
    assert FakeRunner.runs == 1 and len(reports) == 1
    await _stop(task)


@pytest.mark.asyncio
async def test_busy_lock_skips_and_the_poller_picks_up_the_other_run(
    tmp_path: Path,
) -> None:
    reports: list[ScoreSet] = []
    with scorer_lock(tmp_path):
        task = asyncio.create_task(_service(tmp_path, FakeSession(), reports).run())
        await asyncio.sleep(0.05)
        assert FakeRunner.runs == 0  # skipped, not waiting
        other = make_scoreset(fingerprint=FP)
        save_cache(tmp_path, other, {})  # what the CLI would write
    await _until(lambda: len(reports) == 1)
    assert reports[0].run_id == other.run_id
    await _stop(task)


@pytest.mark.asyncio
async def test_poller_ignores_a_stale_or_corrupt_newer_cache(tmp_path: Path) -> None:
    save_cache(tmp_path, make_scoreset(fingerprint=FP), {})
    reports: list[ScoreSet] = []
    task = asyncio.create_task(_service(tmp_path, FakeSession(), reports).run())
    await asyncio.sleep(0.06)
    with capture_logs() as logs:
        save_cache(tmp_path, make_scoreset(fingerprint="c" * 64), {})
        await _until(
            lambda: any(e["event"] == "agent.scores.cache_ignored" for e in logs)
        )
        (tmp_path / "scores.json").write_text("{broken", encoding="utf-8")
        await _until(
            lambda: any(e["event"] == "agent.scores.cache_corrupt" for e in logs)
        )
    assert reports == [] and FakeRunner.runs == 0
    newer = make_scoreset(fingerprint=FP)
    save_cache(tmp_path, newer, {})
    await _until(lambda: len(reports) == 1)
    assert reports[0].run_id == newer.run_id
    await _stop(task)


@pytest.mark.asyncio
async def test_default_report_logs_ready_with_statuses_and_no_numbers(
    tmp_path: Path,
) -> None:
    with capture_logs() as logs:
        task = asyncio.create_task(_service(tmp_path, FakeSession()).run())
        await _until(lambda: any(e["event"] == "agent.scores.ready" for e in logs))
        await _stop(task)
    ready = next(e for e in logs if e["event"] == "agent.scores.ready")
    assert ready["node_id"].startswith("node_")
    assert ready["session_id"].startswith("sess_")
    assert ready["run_id"].startswith("score_")
    assert (
        ready["statuses"]["gpu0.gpu_fp32_tflops"] == "unavailable:torch_not_installed"
    )
    assert "97.3" not in repr(ready)  # statuses only, no numbers


@pytest.mark.asyncio
async def test_unknown_hardware_skips_scoring(tmp_path: Path) -> None:
    with capture_logs() as logs:
        await _service(tmp_path, FakeSession(), facts=None).run()
    assert any(e["event"] == "agent.scores.skipped" for e in logs)
    assert FakeRunner.runs == 0


@pytest.mark.asyncio
async def test_cancel_during_a_run_propagates_cleanly(tmp_path: Path) -> None:
    FakeRunner.hang = True
    task = asyncio.create_task(_service(tmp_path, FakeSession(), []).run())
    await _until(lambda: FakeRunner.runs == 1)
    await _stop(task)
    assert task.cancelled()
    assert not (tmp_path / "scores.json").exists()
    with scorer_lock(tmp_path):  # the lock was released on the way out
        pass


# ---------------------------------------------------------------- runtime hook
def _settings(tmp_path: Path, **scores: Any) -> AgentSettings:
    return AgentSettings(
        data_dir=tmp_path,
        agent=AgentSection(
            controller_address="127.0.0.1:1", scores=ScoreSettings(**scores)
        ),
    )


@pytest.mark.asyncio
async def test_start_scores_respects_enabled(tmp_path: Path) -> None:
    session: Any = FakeSession()
    assert (
        runtime.start_scores(_settings(tmp_path, enabled=False), session, FACTS) is None
    )
    task = runtime.start_scores(
        _settings(tmp_path),
        session,
        FACTS,
        service=_service(tmp_path, FakeSession(AgentState.CONNECTING), []),
    )
    assert task is not None and not task.done()
    await runtime.stop_task(task)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_a_crashing_score_task_is_logged_not_lost(tmp_path: Path) -> None:
    class Boom(ScoreService):
        async def run(self) -> None:
            raise RuntimeError("bug in scoring")

    session: Any = FakeSession()
    with capture_logs() as logs:
        task = runtime.start_scores(
            _settings(tmp_path),
            session,
            FACTS,
            service=Boom(FAST, tmp_path, session, FACTS),
        )
        assert task is not None
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)  # let the done-callback run
    failed = [e for e in logs if e["event"] == "agent.scores.task_failed"]
    assert failed and failed[0]["cause"] == "RuntimeError"


def test_the_agent_process_never_imports_torch_or_numpy() -> None:
    """D32b: only the child imports them. Imports every module the agent loads."""
    code = (
        "import sys\n"
        "import meshforge.agent.main, meshforge.agent.runtime\n"
        "import meshforge.agent.benchmarks.service, meshforge.agent.benchmarks.view\n"
        "import meshforge.agent.benchmarks.runner, meshforge.agent.benchmarks.cache\n"
        "bad = sorted(m for m in ('torch', 'numpy') if m in sys.modules)\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert out.stdout.strip() == ""
