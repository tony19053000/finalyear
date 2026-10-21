"""pytest plugin (V0.14 G1): record planner calls into a golden corpus.

Not loaded by default. It wraps :func:`meshforge.controller.planner.plan` and
writes, per distinct call (at most ``MF_RECORD_PER_TEST`` per test, default
6), the inputs as an **unprojected** replay v1 and the decision as
:func:`mf_planner_golden.decision_view`. How the V0.14 G1 corpus
(``tests/unit/planner/data/v014_g1_golden.json.gz``) was made, at the commit
before the candidate-generator refactor (3782c30e)::

    MF_RECORD_OUT=unit.json uv run pytest -p mf_planner_record \\
        tests/unit/controller tests/unit/cli -m unit -p no:randomly
    MF_RECORD_OUT=comp.json uv run pytest -p mf_planner_record \\
        tests/component/controller/test_v06_g2_dispatch_queue.py ...

then both entry lists were merged, sorted by (source, entry) and gzipped
(``mtime=0``). The summary goes to ``MF_RECORD_OUT + ".log"``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

_entries: dict[str, dict[str, Any]] = {}
_per_test: dict[str, int] = {}
_skipped = {"encode": 0, "cap": 0}
_current = {"test": ""}
_state: dict[str, Any] = {}


def _make(original: Callable[..., Any]) -> Callable[..., Any]:
    from mf_planner_golden import decision_view

    from meshforge.controller.planner.replay import canonical_json, to_replay

    limit = int(os.environ.get("MF_RECORD_PER_TEST", "6"))

    def recording(request: Any, snapshot: Any, policy: str, now: Any = None) -> Any:
        error: Exception | None = None
        decision = None
        try:
            decision = original(request, snapshot, policy, now)
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            error = exc
        test = _current["test"]
        try:
            text = canonical_json(to_replay(request, snapshot, now, projected=False))
        except Exception:  # noqa: BLE001 - counted: an input the codec cannot hold
            _skipped["encode"] += 1
        else:
            key = hashlib.sha256(f"{text}\0{policy}".encode()).hexdigest()
            if key not in _entries:
                if _per_test.get(test, 0) >= limit:
                    _skipped["cap"] += 1
                else:
                    _per_test[test] = _per_test.get(test, 0) + 1
                    _entries[key] = {
                        "source": test,
                        "policy": policy,
                        "replay": json.loads(text),
                        "expected": None
                        if decision is None
                        else decision_view(decision),
                        "error": None if error is None else type(error).__name__,
                    }
        if error is not None:
            raise error
        return decision

    return recording


def _patch_all() -> None:
    original = _state["original"]
    for name in list(sys.modules):
        if not name.startswith(("meshforge", "test_")):
            continue
        module = sys.modules[name]
        if getattr(module, "plan", None) is original:
            module.plan = _state["recording"]  # type: ignore[attr-defined]


def pytest_configure(config: pytest.Config) -> None:
    import meshforge.controller.planner  # noqa: F401 - loads the plan module

    module = sys.modules["meshforge.controller.planner.plan"]
    _state["original"] = module.plan
    _state["recording"] = _make(module.plan)
    _patch_all()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    _current["test"] = item.nodeid.split("[")[0]
    _patch_all()


def pytest_unconfigure(config: pytest.Config) -> None:
    out = Path(os.environ["MF_RECORD_OUT"])
    entries = sorted(
        _entries.values(), key=lambda e: (e["source"], json.dumps(e, sort_keys=True))
    )
    out.write_text(
        json.dumps({"entries": entries}, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    Path(f"{out}.log").write_text(
        f"recorded {len(_entries)} skipped={_skipped} tests={len(_per_test)}\n",
        encoding="utf-8",
    )
