"""V0.5 G4 tester round 2: the [agent.cache] enabled switch (fix round 1, #1).

The switch must default to ON, and the only test that turns it off must be the
exact-sequence resource-replay scenario - cache reports stay on everywhere else
(otherwise the G4 cluster/component evidence silently stops covering them).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from meshforge.agent.settings import AgentSettings

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_cache_reports_default_on(tmp_path: Path) -> None:
    s = AgentSettings(data_dir=tmp_path, agent={"controller_address": "127.0.0.1:1"})
    assert s.agent.cache.enabled is True


def test_only_the_resource_replay_scenario_disables_cache_reports() -> None:
    offenders: dict[str, list[int]] = {}
    pat = re.compile(r"""["']cache["']\s*:\s*\{\s*["']enabled["']\s*:\s*False""")
    for path in ROOT.rglob("*.py"):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text("utf-8", errors="replace")
        hits = [i + 1 for i, ln in enumerate(text.splitlines()) if pat.search(ln)]
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits
    # test_cache_inventory proves the switch itself; test_fabric_resource_replay
    # uses it for test_f ONLY (cache_reports=False at exactly one call site).
    assert set(offenders) == {
        "unit/agent/test_cache_inventory.py",
        "cluster/test_fabric_resource_replay.py",
    }, offenders
    replay = (ROOT / "cluster/test_fabric_resource_replay.py").read_text("utf-8")
    assert replay.count("cache_reports=False") == 1
    before = replay.split("cache_reports=False")[0]
    assert before.rsplit("\ndef ", 1)[-1].startswith(
        "test_f_replayed_reports_are_never_counted_twice"
    )
