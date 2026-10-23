"""V0.15 G5a tester round 1: reviewer findings B1/B2, written expecting the fix.

Tests named *_pending_B1 / *_pending_B2 FAIL until the implementer lands them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
from test_v015_g5a_tester_ml_inputs import _make

from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit


def test_pending_B2_setting_ml_eval_timeout_s_exists() -> None:  # noqa: N802
    assert "ml_eval_timeout_s" in PlannerSettings.model_fields


def test_pending_B2_total_fold_budget_stops_remaining_folds() -> None:  # noqa: N802
    """Every fold child hangs to its own timeout; a total budget must cap the
    whole evaluation and mark the unrun folds eval_budget_exhausted."""
    calls: list[int] = []

    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        calls.append(1)
        await asyncio.sleep(0.2)
        raise TimeoutError

    inputs = _make(child)
    settings: Any = inputs._settings
    try:
        inputs._settings = settings.model_copy(update={"ml_eval_timeout_s": 0.3})
    except Exception:  # noqa: BLE001
        pytest.fail("ml_eval_timeout_s not a setting (B2)")
    fams = {f"fam0{i}": frozenset({f"g{i}"}) for i in range(4)}
    out = asyncio.run(inputs.prepare(fams, allow_leak=False))
    assert len(calls) < 4, "total budget did not stop later folds"
    reasons = " ".join(str(f.get("reason")) for f in out.folds)
    assert "eval_budget_exhausted" in reasons


def test_pending_B1_fold_and_retrain_share_a_training_guard() -> None:  # noqa: N802
    """Behavioural: a holder of the module guard blocks a real train_fold call
    (the same guard the retrainer uses), FIFO, until it releases."""
    from meshforge.controller.ml.child import TRAINING_GUARD
    from meshforge.controller.ml.folds import train_fold

    started: list[str] = []

    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        started.append("child")
        return 1, "x"

    async def main() -> None:
        import tempfile
        from pathlib import Path

        from meshforge.controller.settings import PlannerSettings as Settings

        with tempfile.TemporaryDirectory() as d:
            async with TRAINING_GUARD.held():
                task = asyncio.create_task(
                    train_fold(
                        python="python",
                        dataset=Path(d) / "d",
                        work=Path(d),
                        family="f",
                        fingerprints=(),
                        until=dt.datetime.now(dt.UTC),
                        settings=Settings(),
                        child=child,
                    )
                )
                await asyncio.sleep(0.2)
                assert started == [], "fold child ran while the guard was held"
            await task
            assert started == ["child"]

    asyncio.run(main())


_ = dt
