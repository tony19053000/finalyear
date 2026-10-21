"""V0.14 G1 (D291/D292): the unified candidate generator changed **nothing**
a stored decision says, and a replay reproduces a decision exactly.

The corpus ``data/v014_g1_golden.json.gz`` holds 397 planner calls recorded
from the existing planner suites (unit: V0.6 G2-G4, V0.7 G7, V0.9 G3, V0.10
G3/G4/G8 planner tests; component: the real dispatch ticks of V0.6 G2-G4 and
V0.10 G3) with the planner **before** this group's refactor (commit
3782c30e, ``tests/_support/mf_planner_record.py``): each entry is the call's
inputs as an unprojected replay v1 and its decision as
:func:`mf_planner_golden.decision_view` - the stored candidate JSON, split
JSON, chosen node, digest, reason key and a hash of every Decision field.

Every entry is decoded and planned by today's planner: the view must be
byte-identical (whole jobs and split jobs, every policy the suites use,
pinned, waiting, failed, split too small, forced sets, stale links,
non-finite inputs); an entry that raised then must raise the same error type.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest
from mf_planner_golden import decision_view

from meshforge.controller.planner import plan
from meshforge.controller.planner.replay import from_replay, project, to_replay

pytestmark = pytest.mark.unit

CORPUS = Path(__file__).parent / "data" / "v014_g1_golden.json.gz"


def _entries() -> list[dict[str, Any]]:
    with gzip.open(CORPUS, "rt", encoding="utf-8") as fh:
        entries: list[dict[str, Any]] = json.load(fh)["entries"]
    return entries


ENTRIES = _entries()


def test_the_corpus_covers_what_the_brief_names() -> None:
    assert len(ENTRIES) >= 390
    policies = {e["policy"] for e in ENTRIES}
    assert {
        "random",
        "round_robin",
        "least_loaded",
        "strongest_suitable",
        "resource_score",
        "cache_affinity",
        "cost_model",
    } <= policies
    requests = [e["replay"]["request"] for e in ENTRIES]
    assert sum(1 for r in requests if r["split"] is not None) >= 100
    assert any(r["pinned_node_id"] for r in requests)
    assert any(r["node_set"] for r in requests)
    assert any(r["excluded"] for r in requests)
    views = [e["expected"] for e in ENTRIES if e["expected"] is not None]
    codes = "".join(v["candidates"] for v in views)
    for code in ("split_too_small", "split_too_few_nodes", "slots_full", "revoked"):
        assert f'"reject_code":"{code}"' in codes
    assert "stale_links_ignored" in codes
    assert any(v["chosen"] is None for v in views)  # waiting / failed decisions
    assert any(v["split"] is not None and '"split":true' in v["split"] for v in views)


@pytest.mark.parametrize(
    "entry", ENTRIES, ids=[f"{i:03d}" for i in range(len(ENTRIES))]
)
def test_todays_planner_matches_the_recorded_decision(entry: dict[str, Any]) -> None:
    request, snapshot, now = from_replay(entry["replay"])
    if entry["error"] is not None:
        with pytest.raises(Exception) as caught:
            plan(request, snapshot, entry["policy"], now)
        assert type(caught.value).__name__ == entry["error"]
        return
    decision = plan(request, snapshot, entry["policy"], now)
    assert decision_view(decision) == entry["expected"], entry["source"]


@pytest.mark.parametrize(
    "entry",
    [e for e in ENTRIES if e["error"] is None],
    ids=[f"{i:03d}" for i, e in enumerate(ENTRIES) if e["error"] is None],
)
def test_projection_changes_nothing_but_the_digest(entry: dict[str, Any]) -> None:
    request, snapshot, now = from_replay(entry["replay"])
    full = decision_view(plan(request, snapshot, entry["policy"], now))
    cut = project(request, snapshot)
    projected = decision_view(plan(request, cut, entry["policy"], now))
    for key in ("policy", "chosen", "reason_key", "candidates", "split"):
        assert projected[key] == full[key], key
    # ... and the projected replay round-trips to the projected decision exactly
    again = from_replay(json.loads(json.dumps(to_replay(request, snapshot, now))))
    assert decision_view(plan(again[0], again[1], entry["policy"], again[2])) == (
        projected
    )
