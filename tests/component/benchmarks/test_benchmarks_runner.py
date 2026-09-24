"""G7-A (0.3.4, D32b): the runner against a REAL benchmark child process.

Every failure path is triggered for real through the child's test-only hooks
(``MESHFORGE_BENCH_FAULT``: hang / hang with a grandchild / ``os._exit`` / SIGSEGV)
and every test checks by pid that no child - and no grandchild - survives.
torch is blocked in the child (``MESHFORGE_BENCH_DISABLE=torch``), so nothing here
needs torch or a GPU; the numpy tests skip with a reason when numpy is absent.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from mf_bench_fakes import make_facts, make_gpu

from meshforge.agent.benchmarks.cache import (
    CacheStatus,
    ScorerBusyError,
    load_cache,
    scorer_lock,
)
from meshforge.agent.benchmarks.model import (
    GPU_METRICS,
    HOST_METRICS,
    MetricName,
    MetricReason,
    MetricStatus,
    ScoreSet,
)
from meshforge.agent.benchmarks.plan import GpuRef, Targets
from meshforge.agent.benchmarks.runner import (
    ChildProcess,
    HostFacts,
    RunnerConfig,
    ScoreRunner,
)
from meshforge.agent.benchmarks.service import current_fingerprint, score_and_cache

psutil = pytest.importorskip("psutil", reason="needs the agent extra (pid checks)")

pytestmark = pytest.mark.timeout(180)

HAS_NUMPY = importlib.util.find_spec("numpy") is not None
needs_numpy = pytest.mark.skipif(
    not HAS_NUMPY, reason="numpy is not installed (uv sync --extra agent)"
)
TINY = Targets(cpu_target_s=0.01, mem_target_s=0.01, gpu_target_s=0.01)
HOST = HostFacts(cpu_threads=4, gpus=(GpuRef(0, "Fictional GPU"),))
FP = "f" * 64


async def _no_conditions() -> tuple[bool | None, float | None]:
    return None, None


def _runner(
    *,
    fault: str = "",
    pidfile: Path | None = None,
    budget_s: float = 60.0,
    case_timeout_s: float = 30.0,
    targets: Targets = TINY,
    disable: str = "torch",
    lower_priority: bool = True,
    host: HostFacts = HOST,
) -> ScoreRunner:
    env = {"MESHFORGE_BENCH_DISABLE": disable}
    if fault:
        env["MESHFORGE_BENCH_FAULT"] = fault
    if pidfile is not None:
        env["MESHFORGE_BENCH_FAULT_PIDFILE"] = str(pidfile)
    return ScoreRunner(
        RunnerConfig(
            budget_s=budget_s,
            case_timeout_s=case_timeout_s,
            targets=targets,
            extra_env=env,
            lower_priority=lower_priority,
        ),
        host,
        fingerprint=FP,
        conditions=_no_conditions,
    )


def _alive(pid: int) -> bool:
    try:
        status: str = psutil.Process(pid).status()
    except psutil.NoSuchProcess:
        return False
    return bool(status != psutil.STATUS_ZOMBIE)


def _assert_no_survivor(pids: list[int], timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while any(_alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert [p for p in pids if _alive(p)] == [], "a benchmark process survived"


def _metric(scores: ScoreSet, name: MetricName, gpu: int | None = None) -> Any:
    found = scores.metric(name, gpu)
    assert found is not None, name
    return found


def _assert_ok(scores: ScoreSet, *names: MetricName) -> None:
    for name in names:
        m = _metric(scores, name)
        assert m.status is MetricStatus.OK, (name, m.reason, m.detail)
        assert m.n >= 5
        assert m.mean is not None and math.isfinite(m.mean) and m.mean > 0
        assert m.stdev is not None and math.isfinite(m.stdev)


def _read_pid(pidfile: Path, timeout_s: float = 20.0) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pidfile.exists() and pidfile.read_text(encoding="utf-8").strip():
            return int(pidfile.read_text(encoding="utf-8"))
        time.sleep(0.02)
    raise AssertionError("the faulting child never wrote its grandchild pid")


# ---------------------------------------------------------------- happy path
@needs_numpy
def test_a_real_numpy_run_is_ok_and_gpu_is_torch_not_installed() -> None:
    runner = _runner()
    scores = runner.run_sync()
    _assert_ok(scores, *HOST_METRICS)
    assert _metric(scores, MetricName.CPU_FP32_GFLOPS_1T).backend == "numpy"
    assert _metric(scores, MetricName.CPU_FP32_GFLOPS_1T).threads_effective == 1
    mem = _metric(scores, MetricName.MEM_TRIAD_GB_S)
    assert mem.lower_bound is True  # numpy STREAM is single-threaded (D31)
    for name in GPU_METRICS:
        gpu = _metric(scores, name, 0)
        assert gpu.status is MetricStatus.UNAVAILABLE
        assert gpu.reason is MetricReason.TORCH_NOT_INSTALLED
    assert scores.conditions.priority_lowered is True
    assert scores.fingerprint == FP
    assert ScoreSet.from_json(scores.to_json()) == scores
    assert len(runner.spawned_pids) >= 2  # probe/1T child, all-threads child
    _assert_no_survivor(runner.spawned_pids)


def test_no_blas_gives_unavailable_rows_not_an_error() -> None:
    runner = _runner(disable="torch,numpy")
    scores = runner.run_sync()
    for name in HOST_METRICS:
        m = _metric(scores, name)
        assert m.status is MetricStatus.UNAVAILABLE
        assert m.reason is MetricReason.NO_BLAS_BACKEND
    _assert_no_survivor(runner.spawned_pids)


def test_child_environment_pins_device_order_threads_and_priority() -> None:
    async def probe() -> dict[str, Any]:
        pids: list[int] = []
        child = ChildProcess(
            threads=3,
            lower_priority=True,
            python=sys.executable,
            extra_env={"MESHFORGE_BENCH_DISABLE": "torch,numpy"},
            on_spawn=pids.append,
        )
        await child.start()
        try:
            return await child.call({"op": "probe"}, "info", 30.0)
        finally:
            await child.close()
            _assert_no_survivor(pids)

    info = asyncio.run(probe())
    assert info["cuda_device_order"] == "PCI_BUS_ID"
    assert info["priority"]["lowered"] is True
    if sys.platform != "win32":
        assert info["priority"]["nice"] >= 10


# --------------------------------------------------------------- failure paths
@needs_numpy
def test_a_hanging_case_is_killed_at_case_timeout_with_its_tree(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    runner = _runner(
        fault="cpu_fp32_gflops_1t:setup:hangtree", pidfile=pidfile, case_timeout_s=3.0
    )
    started = time.monotonic()
    scores = runner.run_sync()
    elapsed = time.monotonic() - started
    hung = _metric(scores, MetricName.CPU_FP32_GFLOPS_1T)
    assert hung.status is MetricStatus.FAILED and hung.reason is MetricReason.TIMEOUT
    _assert_ok(
        scores,
        MetricName.CPU_FP32_GFLOPS_ALL,
        MetricName.CPU_FP64_GFLOPS_ALL,
        MetricName.MEM_TRIAD_GB_S,
    )
    assert elapsed < 3.0 + 30.0
    _assert_no_survivor([*runner.spawned_pids, _read_pid(pidfile)])


@needs_numpy
@pytest.mark.parametrize("action", ["exit", "segv"])
def test_a_child_that_dies_mid_case_fails_that_case_only(action: str) -> None:
    runner = _runner(fault=f"cpu_fp32_gflops_all:rep:{action}")
    scores = runner.run_sync()
    dead = _metric(scores, MetricName.CPU_FP32_GFLOPS_ALL)
    assert dead.status is MetricStatus.FAILED
    assert dead.reason is MetricReason.CHILD_CRASHED
    assert "exited" in dead.detail
    # The next all-threads case got a fresh child and ran.
    _assert_ok(scores, MetricName.CPU_FP64_GFLOPS_ALL, MetricName.MEM_TRIAD_GB_S)
    _assert_no_survivor(runner.spawned_pids)


@needs_numpy
def test_the_run_budget_fails_the_rest_as_budget_exhausted() -> None:
    runner = _runner(
        fault="cpu_fp32_gflops_all:setup:hang", budget_s=4.0, case_timeout_s=4.0
    )
    scores = runner.run_sync()
    cut = _metric(scores, MetricName.CPU_FP32_GFLOPS_ALL)
    assert cut.reason is MetricReason.BUDGET_EXHAUSTED
    for name in (MetricName.CPU_FP64_GFLOPS_ALL, MetricName.MEM_TRIAD_GB_S):
        assert _metric(scores, name).reason is MetricReason.BUDGET_EXHAUSTED
    assert scores.conditions.budget_exhausted is True
    _assert_no_survivor(runner.spawned_pids)


@needs_numpy
def test_cancelling_mid_case_kills_the_child_tree_promptly(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    runner = _runner(fault="*:setup:hangtree", pidfile=pidfile, case_timeout_s=60.0)

    async def scenario() -> float:
        task = asyncio.create_task(runner.run())
        await asyncio.to_thread(_read_pid, pidfile)  # the case is hung now
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - started

    took = asyncio.run(scenario())
    grandchild = _read_pid(pidfile)
    assert took < 2.0, f"cancel took {took:.2f} s"
    _assert_no_survivor([*runner.spawned_pids, grandchild], timeout_s=0.5)


# ------------------------------------------------------------- two scorers
_HOLDER = """
import sys
from pathlib import Path
from meshforge.agent.benchmarks.cache import scorer_lock
with scorer_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.read()
"""


def test_a_scorer_in_another_process_makes_this_one_busy(tmp_path: Path) -> None:
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(ScorerBusyError):
            asyncio.run(
                score_and_cache(tmp_path, make_facts(), RunnerConfig(targets=TINY))
            )
        assert not (tmp_path / "scores.json").exists()
    finally:
        holder.communicate("", timeout=10)
    with scorer_lock(tmp_path):  # released with the holder
        pass


@needs_numpy
def test_two_concurrent_scorers_one_runs_one_is_busy_cache_never_torn(
    tmp_path: Path,
) -> None:
    facts = make_facts(threads=2, gpus=(make_gpu(0),))
    fp, _ = current_fingerprint(facts)
    config = RunnerConfig(targets=TINY, extra_env={"MESHFORGE_BENCH_DISABLE": "torch"})
    seen: list[CacheStatus] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            seen.append(load_cache(tmp_path, fp).status)
            time.sleep(0.001)

    async def both() -> list[Any]:
        return list(
            await asyncio.gather(
                score_and_cache(tmp_path, facts, config),
                score_and_cache(tmp_path, facts, config),
                return_exceptions=True,
            )
        )

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        first = asyncio.run(both())
        second = asyncio.run(both())  # overwrite an existing cache under readers
    finally:
        stop.set()
        thread.join(10)
    for outcome in (first, second):
        assert sum(isinstance(o, ScoreSet) for o in outcome) == 1
        assert sum(isinstance(o, ScorerBusyError) for o in outcome) == 1
    assert CacheStatus.CORRUPT not in seen
    assert CacheStatus.VALID in seen
    final = load_cache(tmp_path, fp)
    winner = next(o for o in second if isinstance(o, ScoreSet))
    assert final.valid and final.scores == winner
    doc = json.loads((tmp_path / "scores.json").read_text(encoding="utf-8"))
    assert doc["format"] == 1
