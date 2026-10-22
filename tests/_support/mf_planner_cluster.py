"""Helpers for the V0.14 planner harness (G6, D325-D327) on the cluster harness.

Imported as ``mf_planner_cluster`` (``tests/cluster/test_v014_planner_e2e.py``
and any later planner scenario). Built on :mod:`mf_cluster` /
:mod:`mf_split_cluster` (real controller + agent processes; the real
``compute`` CLI as a subprocess) - nothing here fakes a component:

* :func:`cli_json` - ``compute <args> --json`` as the laptop whose agent keeps
  ``data_dir`` (:func:`mf_split_cluster.compute`), the document parsed;
* :func:`create_campaign`, :func:`start_campaign`, :func:`wait_campaigns` -
  the operator's ``compute bench create / start / status`` path;
* the **independent recomputations** D326 asks for - criterion 1's share and
  every paired criterion-2 difference from a report's own ``groups[]``, with
  code that shares nothing with the evaluator (:func:`criterion1_from_groups`,
  :func:`paired_from_groups`, :func:`criterion2_status`), and the decision
  answer's normalised leaf set (:func:`answer_leaves`, criterion 4).
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import pytest
from mf_cluster import Fabric
from mf_split_cluster import compute

__all__ = [
    "CRITERION_KEY",
    "RULES",
    "answer_leaves",
    "cli_json",
    "create_campaign",
    "criterion1_from_groups",
    "criterion2_status",
    "paired_from_groups",
    "start_campaign",
    "wait_campaigns",
]

CRITERION_KEY: Final = "cost_model:calibrated:min_time"
RULES: Final = (
    "random",
    "round_robin",
    "least_loaded",
    "strongest_suitable",
    "resource_score",
    "cache_affinity",
)
"""The rule policies criterion 2 compares against (every placement policy
but ``cost_model``), written out here - not imported from the evaluator."""
_DICT_KEYED: Final = ("terms", "phases_ms")


def cli_json(
    fabric: Fabric, data_dir: Path, *args: str, cwd: Path, timeout_s: float = 300
) -> Any:
    """``compute <args> --json``: exit 0 and its parsed stdout."""
    done = compute(fabric, data_dir, *args, "--json", cwd=cwd, timeout_s=timeout_s)
    assert done.returncode == 0, (args, done.stdout[-3000:], done.stderr[-3000:])
    return json.loads(done.stdout)


def create_campaign(
    fabric: Fabric, data_dir: Path, spec: str, name: str, *, cwd: Path
) -> tuple[str, int]:
    """``compute bench create SPEC`` -> (campaign id, run count)."""
    path = cwd / f"{name}.yaml"
    path.write_text(spec, encoding="utf-8")
    doc = cli_json(fabric, data_dir, "bench", "create", str(path), cwd=cwd)
    return str(doc["campaign_id"]), int(doc["run_count"])


def start_campaign(
    fabric: Fabric,
    data_dir: Path,
    campaign_id: str,
    *,
    cwd: Path,
    per_node: int = 1,
    cooldown_s: float = 0.0,
) -> None:
    """``compute bench start`` (the controller's runner owns it from now)."""
    doc = cli_json(
        fabric,
        data_dir,
        "bench",
        "start",
        campaign_id,
        "--per-node",
        str(per_node),
        "--cooldown",
        str(cooldown_s),
        cwd=cwd,
    )
    assert doc["runner_owned"] is True and doc["state"] == "active", doc


def wait_campaigns(
    fabric: Fabric,
    data_dir: Path,
    campaign_ids: Sequence[str],
    *,
    cwd: Path,
    within: float,
) -> dict[str, dict[str, Any]]:
    """Poll ``compute bench status`` until every campaign is ``done``
    (bounded); each must finish ``complete``."""
    deadline = time.monotonic() + within
    last: dict[str, dict[str, Any]] = {}
    while True:
        for cid in campaign_ids:
            last[cid] = dict(
                cli_json(fabric, data_dir, "bench", "status", cid, cwd=cwd)
            )
        if all(doc["state"] == "done" for doc in last.values()):
            for cid, doc in last.items():
                assert doc["finished_reason"] == "complete", (cid, doc)
            return last
        if time.monotonic() > deadline:
            pytest.fail(
                f"campaigns not done in {within:g} s: "
                + json.dumps(
                    {c: (d["state"], d["counts"]) for c, d in last.items()},
                    sort_keys=True,
                )
                + "\n"
                + fabric.describe()
            )
        time.sleep(2.0)


# ------------------------------------------------------------ recomputations


def _counted(group: Mapping[str, Any], key: str) -> float | None:
    """The group's counted regret % for policy ``key`` (None = not counted)."""
    for pick in group.get("picks") or ():
        if pick["key"] == key and pick["status"] == "counted":
            return float(pick["regret_pct"])
    return None


def criterion1_from_groups(report: Mapping[str, Any]) -> tuple[int, float | None]:
    """(counted groups, % within the threshold) of the criterion policy,
    straight from ``groups[]``."""
    threshold = float(report["settings"]["regret_threshold_pct"])
    regrets = [
        r for g in report["groups"] if (r := _counted(g, CRITERION_KEY)) is not None
    ]
    if not regrets:
        return 0, None
    within = sum(1 for r in regrets if r <= threshold)
    return len(regrets), round(within / len(regrets) * 100.0, 6)


def paired_from_groups(
    report: Mapping[str, Any],
) -> dict[str, tuple[int, float | None]]:
    """rule policy -> (groups counted for both it and the criterion policy,
    mean of ``regret(rule) - regret(criterion)`` over them)."""
    out: dict[str, tuple[int, float | None]] = {}
    for rule in RULES:
        diffs: list[float] = []
        for group in report["groups"]:
            mine, crit = _counted(group, rule), _counted(group, CRITERION_KEY)
            if mine is not None and crit is not None:
                diffs.append(round(mine - crit, 6))
        mean = round(math.fsum(diffs) / len(diffs), 6) if diffs else None
        out[rule] = (len(diffs), mean)
    return out


def criterion2_status(comparisons: Sequence[Mapping[str, Any]], min_groups: int) -> str:
    """Criterion 2's rule (D322) restated from the comparisons' CIs."""
    if not comparisons or any(c["common_groups"] < min_groups for c in comparisons):
        return "too_few_groups"
    if all(float(c["ci_low"]) > 0.0 for c in comparisons):
        return "PASS"
    if any(float(c["ci_high"]) < 0.0 for c in comparisons):
        return "FAIL"
    return "not_separable"


def answer_leaves(data: Any, path: tuple[str, ...] = ()) -> set[str]:
    """The normalised leaf paths of a decisions answer (``[]`` for a list
    index, ``{}`` for a term / phase name; ``detail`` is one leaf) - the
    criterion-4 fixture's notation."""
    if isinstance(data, Mapping) and (not path or path[-1] != "detail"):
        leaves: set[str] = set()
        for key, value in data.items():
            token = "{}" if path and path[-1] in _DICT_KEYED else str(key)
            leaves |= answer_leaves(value, (*path, token))
        return leaves
    if isinstance(data, list):
        leaves = set()
        for value in data:
            leaves |= answer_leaves(value, (*path, "[]"))
        return leaves  # an empty list has no leaf
    return {".".join(path)}
