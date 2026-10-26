"""pytest plugin used by ``scripts/release_check.py``: one outcome per test item.

Loaded with ``-p release_outcomes_plugin`` (``scripts/`` on ``PYTHONPATH``). At the
end of the session it writes ``{node_id: {"outcome": ..., "reason": ...}}`` as JSON
to the path in ``$MESHFORGE_RELEASE_OUTCOMES``. The outcome of an item folds its
setup/call/teardown phases: ``failed`` (call failed), ``error`` (setup or teardown
failed), ``skipped`` (skipped or xfailed - neither is evidence), else ``passed``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ENV: str = "MESHFORGE_RELEASE_OUTCOMES"
"""Set by ``scripts/release_check.py`` (``OUTCOMES_ENV`` there)."""
_RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}
_outcomes: dict[str, dict[str, str]] = {}


def _fold(report: pytest.TestReport) -> tuple[str, str]:
    if hasattr(report, "wasxfail"):
        return "skipped", f"xfail: {report.wasxfail}"
    if report.skipped:
        longrepr = report.longrepr
        reason = longrepr[2] if isinstance(longrepr, tuple) else str(longrepr)
        return "skipped", reason
    if report.failed:
        return ("failed" if report.when == "call" else "error"), report.when
    return "passed", ""


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    outcome, reason = _fold(report)
    if report.when == "call" or outcome != "passed":
        previous = _outcomes.get(report.nodeid)
        if previous is None or _RANK[outcome] >= _RANK[previous["outcome"]]:
            _outcomes[report.nodeid] = {"outcome": outcome, "reason": reason}


def pytest_sessionfinish(session: pytest.Session) -> None:
    target = os.environ.get(ENV)
    if target:
        Path(target).write_text(json.dumps(_outcomes, sort_keys=True), encoding="utf-8")
