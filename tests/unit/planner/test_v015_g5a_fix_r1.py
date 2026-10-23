"""V0.15 G5a fix round 1 (reviewer B1 / B2 + non-blocking items).

* **B1** - one training child at a time for the whole controller: an
  evaluator fold and a retrain started together never run their children
  at the same time (the shared :data:`~meshforge.controller.ml.child.
  TRAINING_GUARD`); mutation "a caller skips the guard" makes the overlap 2;
* **B2** - ``ml_eval_timeout_s`` caps the evaluation's fold training: the
  fold running when it is spent is stopped (its child cancelled), the
  families left get no model, reason ``eval_budget_exhausted``; the wait for
  the guard counts; the CLI's timeout is the budget plus the replay margin;
* ``run_child`` reaps a cancelled child; ``scratch_dir`` creates and removes
  its directory off the event loop;
* the report's ``ml`` block records the fold cut-off (``until``) and the
  Markdown says the ML counts are per chosen candidate.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import shutil
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("fastapi", reason="needs the controller extra")

from mf_eval_world import fabric, group, records

from meshforge.common.ml_eval import (
    DEFAULT_ML_EVAL_TIMEOUT_S,
    EVAL_REPLAY_MARGIN_S,
    EVAL_WAIT_DEFAULT_S,
)
from meshforge.controller import ml_retrain_task
from meshforge.controller.api.planner_eval_models import EvalReportOut
from meshforge.controller.ml import child as child_mod
from meshforge.controller.ml.child import TRAINING_GUARD, run_child, scratch_dir
from meshforge.controller.ml.export import BuiltDataset
from meshforge.controller.ml.folds import train_fold
from meshforge.controller.ml_retrain_task import MlRetrainer
from meshforge.controller.planner_eval.core import evaluate
from meshforge.controller.planner_eval.ml_inputs import (
    FOLD_BUDGET_EPSILON_S,
    FoldMlInputs,
)
from meshforge.controller.planner_eval.model import (
    ML_EVALUATED,
    EvalConfig,
    EvalMl,
    default_policies,
)
from meshforge.controller.planner_eval.report import render_markdown
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 12, 1, tzinfo=dt.UTC)


class _Overlap:
    """A fake training child that records how many run at once."""

    def __init__(self, hold_s: float = 0.05) -> None:
        self.active = 0
        self.peak = 0
        self.calls: list[str] = []
        self.hold_s = hold_s

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        self.calls.append("fold" if "--exclude-family" in argv else "retrain")
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.hold_s)
        finally:
            self.active -= 1
        return 1, "fake child"


class _Models:
    mode = "auto"

    async def serving_version(self) -> tuple[None, None]:
        return (None, None)


class _Loader:
    async def load(self, version: str) -> Any:
        return None


def _built() -> BuiltDataset:
    class _D:
        rows: tuple[Any, ...] = ()
        parquet = b""

    return BuiltDataset(_D(), 0, False)  # type: ignore[arg-type]


def _inputs(child: Any, settings: PlannerSettings, **kw: Any) -> FoldMlInputs:
    async def build() -> BuiltDataset:
        return _built()

    return FoldMlInputs(
        settings,
        _Models(),  # type: ignore[arg-type]
        _Loader(),  # type: ignore[arg-type]
        build,
        child=child,
        missing=list,
        clock=lambda: NOW,
        **kw,
    )


def _retrainer(child: Any, monkeypatch: pytest.MonkeyPatch) -> MlRetrainer:
    """A retrainer whose database reads are stubbed: ``_retrain`` goes
    straight to the real child call site."""
    retrainer = MlRetrainer(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        PlannerSettings(),
        child=child,
        set_loader=object(),  # type: ignore[arg-type]
    )

    async def key() -> dict[str, Any]:
        return {}

    async def unchanged(_key: Any) -> bool:
        return False

    async def reference(_dir: Path) -> None:
        return None

    async def build(_engine: Any, *, max_runs: int) -> BuiltDataset:
        return _built()

    monkeypatch.setattr(retrainer, "_key", key)
    monkeypatch.setattr(retrainer, "_unchanged", unchanged)
    monkeypatch.setattr(retrainer, "_write_reference", reference)
    monkeypatch.setattr(ml_retrain_task.export, "build_dataset", build)
    return retrainer


# ---------------------------------------------------------------------- B1


def test_a_fold_and_a_retrain_never_train_at_the_same_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _Overlap()
    retrainer = _retrainer(fake, monkeypatch)
    inputs = _inputs(fake, PlannerSettings())
    families = {"fam_a": frozenset({"g1"}), "fam_b": frozenset({"g2"})}

    async def main() -> tuple[Any, Any]:
        return await asyncio.gather(
            inputs.prepare(families, allow_leak=False),
            retrainer._retrain("test", NOW),
        )

    ml, result = asyncio.run(main())
    assert sorted(fake.calls) == ["fold", "fold", "retrain"]
    assert fake.peak == 1, "two training children ran at once"
    assert result.error == "child_failed"
    assert [f["reason"].split(":")[0] for f in ml.folds] == ["child_failed"] * 2


def test_without_the_guard_they_would_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation guard for the test above: the fake really can overlap."""

    class _NoGuard:
        def busy(self) -> bool:
            return False

        def held(self) -> Any:
            return _Null()

    class _Null:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(ml_retrain_task, "TRAINING_GUARD", _NoGuard())
    fake = _Overlap()
    retrainer = _retrainer(fake, monkeypatch)

    async def main(tmp: Path) -> None:
        dataset = tmp / "d.parquet"
        dataset.write_bytes(b"")
        work = tmp / "w"
        work.mkdir()
        await asyncio.gather(
            train_fold(
                python=sys.executable,
                dataset=dataset,
                work=work,
                family="fam_a",
                fingerprints=(),
                until=NOW,
                settings=PlannerSettings(),
                child=fake,
                guard=_NoGuard(),  # type: ignore[arg-type]
            ),
            retrainer._retrain("test", NOW),
        )

    async def run() -> None:
        async with scratch_dir("mf-test-") as tmp:
            await main(tmp)

    asyncio.run(run())
    assert fake.peak == 2


def test_the_guard_works_across_event_loops() -> None:
    """A module-level guard used by several ``asyncio.run`` (tests, a
    restarted loop) never trips over a lock bound to a dead loop."""

    async def contend() -> int:
        order: list[int] = []

        async def one(i: int) -> None:
            async with TRAINING_GUARD.held():
                order.append(i)
                await asyncio.sleep(0.01)

        await asyncio.gather(one(1), one(2))
        return len(order)

    assert asyncio.run(contend()) == 2
    assert asyncio.run(contend()) == 2


# ---------------------------------------------------------------------- B2


def test_the_budget_default_and_the_cli_timeout_derive_from_it() -> None:
    assert PlannerSettings().ml_eval_timeout_s == DEFAULT_ML_EVAL_TIMEOUT_S
    assert EVAL_WAIT_DEFAULT_S == DEFAULT_ML_EVAL_TIMEOUT_S + EVAL_REPLAY_MARGIN_S
    with pytest.raises(ValueError, match="ml_eval_timeout_s"):
        PlannerSettings(ml_eval_timeout_s=0.0)


def test_a_spent_budget_leaves_the_remaining_families_untrained() -> None:
    """Deterministic clock: the first fold spends the whole budget; the
    other families never start a child."""
    now = [0.0]
    fake = _Overlap(hold_s=0.0)

    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        now[0] += 120.0
        return await fake(argv, timeout_s)

    settings = PlannerSettings(ml_eval_timeout_s=100.0)
    inputs = _inputs(child, settings, monotonic=lambda: now[0])
    families = {f"fam_{c}": frozenset({c}) for c in "abc"}
    ml = asyncio.run(inputs.prepare(families, allow_leak=False))
    assert ml.status == ML_EVALUATED
    assert len(fake.calls) == 1
    reasons = [f["reason"] for f in ml.folds]
    assert reasons[0].startswith("child_failed")
    assert (
        reasons[1:]
        == ["eval_budget_exhausted: not trained, ml_eval_timeout_s = 100 s"] * 2
    )
    assert [f["source"] for f in ml.folds] == ["none"] * 3
    assert all(ml.inputs[f] is None for f in families)  # -> no_fold_model rows


def test_the_running_fold_is_stopped_and_its_child_cancelled() -> None:
    cancelled: list[bool] = []
    timeouts: list[float] = []

    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        timeouts.append(timeout_s)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return 0, ""

    settings = PlannerSettings(ml_eval_timeout_s=1.0)
    inputs = _inputs(child, settings)
    families = {"fam_a": frozenset({"a"}), "fam_b": frozenset({"b"})}
    started = time.monotonic()
    ml = asyncio.run(inputs.prepare(families, allow_leak=False))
    assert time.monotonic() - started < 10.0
    assert cancelled == [True]
    # V0.15 G6b (D378): the budget left + epsilon - the evaluation deadline wins
    assert timeouts and timeouts[0] <= 1.0 + FOLD_BUDGET_EPSILON_S
    assert [f["reason"] for f in ml.folds] == [
        "eval_budget_exhausted: stopped, ml_eval_timeout_s = 1 s",
        "eval_budget_exhausted: not trained, ml_eval_timeout_s = 1 s",
    ]


def test_waiting_for_a_retrain_counts_against_the_budget() -> None:
    calls: list[str] = []

    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        calls.append("fold")
        return 1, ""

    async def main() -> EvalMl:
        release = asyncio.Event()
        holding = asyncio.Event()

        async def retrain_holds_the_guard() -> None:
            async with TRAINING_GUARD.held():
                holding.set()
                await release.wait()

        task = asyncio.create_task(retrain_holds_the_guard())
        await holding.wait()
        inputs = _inputs(child, PlannerSettings(ml_eval_timeout_s=1.0))
        try:
            return await inputs.prepare({"fam_a": frozenset()}, allow_leak=False)
        finally:
            release.set()
            await task

    ml = asyncio.run(main())
    assert calls == []
    assert ml.folds[0]["reason"].startswith("eval_budget_exhausted: stopped")


# ------------------------------------------------------------- non-blocking


def test_a_cancelled_child_is_killed_and_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    procs: list[asyncio.subprocess.Process] = []
    real = asyncio.create_subprocess_exec
    spawned: list[asyncio.Event] = []

    async def spy(*args: Any, **kw: Any) -> asyncio.subprocess.Process:
        proc = await real(*args, **kw)
        procs.append(proc)
        spawned[0].set()
        return proc

    monkeypatch.setattr(child_mod.asyncio, "create_subprocess_exec", spy)

    async def main() -> None:
        spawned.append(asyncio.Event())
        task = asyncio.create_task(
            run_child([sys.executable, "-c", "import time; time.sleep(60)"], 60.0)
        )
        await spawned[0].wait()
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # reaped before the cancel reached the caller (no zombie)
        assert procs[0].returncode is not None

    asyncio.run(main())


def test_scratch_dir_is_made_and_removed_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    threads: list[str] = []
    real = shutil.rmtree

    def spy(path: Any, *args: Any, **kw: Any) -> None:
        threads.append(threading.current_thread().name)
        real(path, *args, **kw)

    monkeypatch.setattr(shutil, "rmtree", spy)
    seen: list[Path] = []

    async def main() -> None:
        loop_thread = threading.current_thread().name

        async def use() -> None:
            async with scratch_dir("mf-test-") as path:
                seen.append(path)
                (path / "f").write_text("x")
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await use()
        assert threads and threads[0] != loop_thread

    asyncio.run(main())
    assert seen and not seen[0].exists()


def test_the_ml_block_records_until_and_the_markdown_explains_the_counts() -> None:
    request, snapshot = fabric({"node_a": 10.0, "node_b": 20.0})
    groups = [
        group(f"g{i:02d}", {"node_a": 10.0, "node_b": 20.0}, family="cpu.matmul")
        for i in range(3)
    ]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    report = evaluate(
        groups,
        recs,
        {},
        default_policies(ml=True),
        EvalConfig(bootstrap_resamples=100),
        campaign_ids=["camp_1"],
        ml=EvalMl(ML_EVALUATED, inputs={"cpu.matmul": None}, until=NOW.isoformat()),
    )
    assert report["ml"]["until"] == NOW.isoformat()
    assert EvalReportOut.model_validate(report).ml.until == NOW.isoformat()  # type: ignore[union-attr]
    text = render_markdown(report)
    assert f"fold models trained on runs finished by {NOW.isoformat()}" in text
    assert "on its chosen candidate only" in text
