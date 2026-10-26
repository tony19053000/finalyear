"""Raw campaign data -> the analysis document (D417/D418/D419). Pure.

**Groups.** A group is one ``(link, family, size)``: the same work measured
under every arm. Per arm the group keeps the ``ok`` runs whose decisions match
the arm's profile (:attr:`~.arms.Arm.expect`; a mismatch - e.g. B6 planned
without any ML model - is counted and left out, never mixed in) and
summarises them with :func:`meshforge.bench.stats.summarize` (median and its
distribution-free CI). An arm with fewer than ``min_runs`` such runs in a
group is ``insufficient`` there.

**Metric.** ``makespan_ms`` of a run = last job finished - first job created
(controller clock); with one job per run it is that job's end-to-end time.
End-to-end, queue wait and overhead (end-to-end - ``run`` phase, RQ4) are
summarised alongside.

**Regret vs the oracle** (D315/D416), with the offline evaluator's own
functions (``planner_eval.core.best_median`` / ``group_regret``): each group
becomes an :class:`~meshforge.controller.planner_eval.model.EvalGroup` whose
configurations are the arms; the oracle is the best measured arm of the
group (ties: equal medians). Per arm: the mean regret over its measured
groups with the seeded bootstrap CI (``bench.stats.bootstrap_mean_ci``).

**Paired comparisons** (D417, verdict rules of D341/D357). For an arm vs a
reference over the groups where both are measured: ``d = ln(median(arm) /
median(reference))`` (< 0 = the arm is faster). The paired bootstrap of the
mean ``d`` (``ml.train.metrics.paired_verdict`` - the V0.15 helper - 2 000
resamples, fixed seed, 95 %): ``better`` iff at least ``min_groups`` groups
and the CI's upper end < ``-min_effect_log`` (0.02, ~2 %); ``worse`` iff its
lower end > 0; otherwise ``not_separable`` (reason ``too_few_groups`` /
``ci_not_beyond_min_effect``). The effect size is the geometric-mean
speedup ``exp(-mean d)`` with its CI ``[exp(-high), exp(-low)]``, plus the
median per-group speedup.

Comparisons made: every arm vs B5 (calibrated planner v2) and vs B0 (Local
only) when present, A5 / A6 vs B6 (live ablations), each pooled over every
link and per link; A7 = per arm, Ethernet vs Wi-Fi paired over (family,
size) when both links were run.

Deterministic: groups, arms and comparisons in sorted order, every bootstrap
seeded from (settings seed, comparison name), every float rounded
(:func:`.canonical.num`). The same raw files give the same bytes.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Final

from evalkit.arms import ARMS
from evalkit.canonical import num
from evalkit.raw import RawData
from evalkit.schedule import derive_seed

__all__ = [
    "ANALYSIS_VERSION",
    "AnalysisSettings",
    "analyze",
    "paired",
]

ANALYSIS_VERSION: Final = 1
_REFERENCES: Final = ("B5", "B0")
_ABLATIONS: Final = (("A5", "B6"), ("A6", "B6"))
_LINK_A7: Final = ("ethernet", "wifi")


@dataclass(frozen=True, slots=True)
class AnalysisSettings:
    seed: int = 20261027
    resamples: int = 2000
    level: float = 0.95
    min_effect_log: float = 0.02
    """D357 ``ml_min_effect_log``: ``better`` needs the CI below -2 %."""
    min_groups: int = 5
    """D357: fewer paired groups -> ``not_separable`` (too_few_groups)."""
    min_runs: int = 3
    """An arm needs this many usable runs in a group to be measured there."""
    regret_threshold_pct: float = 10.0
    """Share of groups within this regret (reported, as V0.14 criterion 1)."""


# ------------------------------------------------------------------ runs


def _when(text: Any) -> dt.datetime | None:
    if not isinstance(text, str):
        return None
    out = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return out if out.tzinfo is not None else out.replace(tzinfo=dt.UTC)


def _makespan_ms(jobs: Sequence[Mapping[str, Any]]) -> float | None:
    starts = [_when(j.get("created_at")) for j in jobs]
    ends = [_when(j.get("finished_at")) for j in jobs]
    if not jobs or any(s is None for s in starts) or any(e is None for e in ends):
        return None
    first = min(s for s in starts if s is not None)
    last = max(e for e in ends if e is not None)
    return (last - first).total_seconds() * 1000.0


def _mismatch(arm_id: str, jobs: Sequence[Mapping[str, Any]]) -> str | None:
    """Why the run's decisions do not match the arm's profile (None = they do)."""
    expect = ARMS[arm_id].expect
    for job in jobs:
        if "calibrated" in expect and (
            job.get("calibration_version") is not None
        ) != bool(expect["calibrated"]):
            return "calibration_" + ("missing" if expect["calibrated"] else "present")
        if "ml" in expect and (job.get("ml_model_version") is not None) != bool(
            expect["ml"]
        ):
            return "ml_" + ("inactive" if expect["ml"] else "present")
    return None


@dataclass(frozen=True, slots=True)
class _Run:
    link: str
    family: str
    size: str
    arm: str
    block: int
    makespan_ms: float
    jobs: tuple[Mapping[str, Any], ...]


def _usable(
    raws: Sequence[RawData],
) -> tuple[list[_Run], dict[str, Counter[str]], dict[str, dict[str, set[str]]]]:
    """Usable runs; per arm the status counts (``ok`` / ``failed`` /
    ``timeout`` / ``submit_error`` / ``mismatch:<why>``); per arm the
    calibration and ML versions its decisions used."""
    runs: list[_Run] = []
    statuses: dict[str, Counter[str]] = {}
    versions: dict[str, dict[str, set[str]]] = {}
    for raw in raws:
        link = str(raw.campaign["spec"]["link"])
        for record in raw.runs:
            arm = str(record["arm"])
            counter = statuses.setdefault(arm, Counter())
            jobs = tuple(record.get("jobs") or ())
            seen = versions.setdefault(arm, {"calibration": set(), "ml": set()})
            for job in jobs:
                if job.get("calibration_version") is not None:
                    seen["calibration"].add(str(job["calibration_version"]))
                if job.get("ml_model_version") is not None:
                    seen["ml"].add(str(job["ml_model_version"]))
            status = str(record.get("status"))
            if status != "ok":
                counter[status] += 1
                continue
            why = _mismatch(arm, jobs)
            if why is not None:
                counter["mismatch:" + why] += 1
                continue
            makespan = _makespan_ms(jobs)
            if makespan is None:
                counter["no_timestamps"] += 1
                continue
            counter["ok"] += 1
            runs.append(
                _Run(
                    link=link,
                    family=str(record["family"]),
                    size=str(record["size"]),
                    arm=arm,
                    block=int(record["block"]),
                    makespan_ms=makespan,
                    jobs=jobs,
                )
            )
    return runs, statuses, versions


# ------------------------------------------------------------------ stats


def _summary(values: Sequence[float]) -> dict[str, Any] | None:
    from meshforge.bench.stats import summarize

    summary = summarize(values)
    if summary is None:
        return None
    return {
        "n": summary.n,
        "median": num(summary.median),
        "mean": num(summary.mean),
        "ci_low": num(summary.ci_low),
        "ci_high": num(summary.ci_high),
        "ci_coverage": num(summary.ci_coverage),
        "cv": num(summary.cv),
        "outliers": summary.outliers,
    }


def _job_values(runs: Sequence[_Run], key: str) -> list[float]:
    return [
        float(job[key])
        for run in runs
        for job in run.jobs
        if isinstance(job.get(key), int | float)
    ]


def paired(
    deltas: Mapping[str, float],
    *,
    seed: int,
    settings: AnalysisSettings,
) -> dict[str, Any]:
    """The D357 paired verdict on per-group log ratios (module docstring)."""
    from meshforge.controller.ml.train.metrics import paired_verdict

    values = [deltas[k] for k in sorted(deltas)]
    result = paired_verdict(
        values,
        seed=seed,
        resamples=settings.resamples,
        min_effect=settings.min_effect_log,
        min_families=settings.min_groups,
        level=settings.level,
    )
    reason = str(result["reason"]).replace("families", "groups")
    mean, low, high = result["mean"], result["low"], result["high"]
    speedups = sorted(math.exp(-d) for d in values)
    median_speedup = None
    if speedups:
        mid = len(speedups) // 2
        median_speedup = (
            speedups[mid]
            if len(speedups) % 2
            else (speedups[mid - 1] + speedups[mid]) / 2.0
        )
    return {
        "groups": len(values),
        "mean_log_ratio": num(mean),
        "ci_low": num(low),
        "ci_high": num(high),
        "level": settings.level,
        "speedup_geomean": None if mean is None else num(math.exp(-mean)),
        "speedup_ci": None
        if low is None or high is None
        else [num(math.exp(-high)), num(math.exp(-low))],
        "speedup_median": num(median_speedup),
        "verdict": result["verdict"],
        "reason": reason,
        "per_group": {k: num(deltas[k]) for k in sorted(deltas)},
    }


# ------------------------------------------------------------------ groups


def _group_key(link: str, family: str, size: str) -> str:
    return f"{link}/{family}/{size}"


def _groups(
    runs: Sequence[_Run], arms: Sequence[str], settings: AnalysisSettings
) -> list[dict[str, Any]]:
    from meshforge.controller.planner_eval.core import best_median, group_regret
    from meshforge.controller.planner_eval.model import ConfigRef, EvalGroup

    by_group: dict[tuple[str, str, str], dict[str, list[_Run]]] = {}
    for run in runs:
        cell = by_group.setdefault((run.link, run.family, run.size), {})
        cell.setdefault(run.arm, []).append(run)
    out: list[dict[str, Any]] = []
    for (link, family, size), cell in sorted(by_group.items()):
        key = _group_key(link, family, size)
        configs = []
        rows: dict[str, dict[str, Any]] = {}
        for arm in arms:
            arm_runs = sorted(cell.get(arm, []), key=lambda r: r.block)
            makespans = [r.makespan_ms for r in arm_runs]
            measured = len(arm_runs) >= settings.min_runs
            summary = _summary(makespans)
            median = None if summary is None else summary["median"]
            configs.append(
                ConfigRef(
                    config_id=arm,
                    strategy=arm,
                    node_id="",
                    origin_node_id="",
                    state="measured"
                    if measured and median is not None
                    else "unsupported",
                    median_ms=median,
                    repetitions=len(arm_runs),
                )
            )
            modes = Counter(str(job.get("mode")) for r in arm_runs for job in r.jobs)
            rows[arm] = {
                "state": "measured" if measured else "insufficient",
                "runs": len(arm_runs),
                "makespan_ms": summary,
                "end_to_end_ms": _summary(_job_values(arm_runs, "end_to_end_ms")),
                "queue_ms": _summary(_job_values(arm_runs, "queue_ms")),
                "overhead_ms": _summary(_job_values(arm_runs, "overhead_ms")),
                "transfer_bytes": _summary(_job_values(arm_runs, "transfer_bytes")),
                "modes": dict(sorted(modes.items())),
                "regret_pct": None,
                "regret_ms": None,
            }
        best_ms = best_median(configs)
        measured_configs = [c for c in configs if c.state == "measured"]
        oracle: str | None = None
        tied: tuple[str, ...] = ()
        if best_ms is not None:
            winners = sorted(
                c.config_id for c in measured_configs if c.median_ms == best_ms
            )
            oracle, tied = winners[0], tuple(winners[1:])
        group = EvalGroup(
            campaign_id="live",
            group_id=key,
            family=family,
            condition=link,
            label="tie" if tied else "best",
            best_config_id=oracle,
            tied_config_ids=tied,
            configs=tuple(configs),
        )
        if best_ms is not None:
            for config in measured_configs:
                regret = group_regret(group, config.config_id, best_ms)
                if regret is not None:
                    rows[config.config_id]["regret_pct"] = num(regret[0])
                    rows[config.config_id]["regret_ms"] = num(regret[1])
        out.append(
            {
                "key": key,
                "link": link,
                "family": family,
                "size": size,
                "oracle": oracle,
                "oracle_ties": list(tied),
                "oracle_ms": num(best_ms),
                "arms": rows,
            }
        )
    return out


def _per_arm(
    groups: Sequence[Mapping[str, Any]],
    runs: Sequence[_Run],
    arms: Sequence[str],
    statuses: Mapping[str, Counter[str]],
    versions: Mapping[str, Mapping[str, set[str]]],
    settings: AnalysisSettings,
) -> dict[str, Any]:
    from meshforge.bench.stats import bootstrap_mean_ci

    out: dict[str, Any] = {}
    for arm in arms:
        regrets = [
            float(g["arms"][arm]["regret_pct"])
            for g in groups
            if g["arms"][arm]["regret_pct"] is not None
        ]
        ci = (
            bootstrap_mean_ci(
                regrets,
                seed=derive_seed(settings.seed, "regret", arm),
                resamples=settings.resamples,
                level=settings.level,
            )
            if regrets
            else None
        )
        arm_runs = [r for r in runs if r.arm == arm]
        modes = Counter(str(job.get("mode")) for r in arm_runs for job in r.jobs)
        by_mode: dict[str, Any] = {}
        for mode in sorted(modes):
            values = [
                float(job["overhead_ms"])
                for r in arm_runs
                for job in r.jobs
                if job.get("mode") == mode
                and isinstance(job.get("overhead_ms"), int | float)
            ]
            by_mode[mode] = _summary(values)
        definition = ARMS[arm]
        out[arm] = {
            "label": definition.label,
            "profile": definition.profile,
            "statuses": dict(sorted(statuses.get(arm, Counter()).items())),
            "groups_measured": len(regrets),
            "mean_regret_pct": None if ci is None else num(ci.mean),
            "regret_ci": None if ci is None else [num(ci.low), num(ci.high)],
            "within_threshold_pct": None
            if not regrets
            else num(
                100.0
                * sum(1 for r in regrets if r <= settings.regret_threshold_pct)
                / len(regrets)
            ),
            "zero_regret_groups": sum(1 for r in regrets if r == 0.0),
            "modes": dict(sorted(modes.items())),
            "overhead_ms_by_mode": by_mode,
            "calibration_versions": sorted(
                versions.get(arm, {}).get("calibration", set())
            ),
            "ml_versions": sorted(versions.get(arm, {}).get("ml", set())),
        }
    return out


def _deltas(
    groups: Sequence[Mapping[str, Any]], arm: str, ref: str, link: str | None
) -> dict[str, float]:
    out: dict[str, float] = {}
    for group in groups:
        if link is not None and group["link"] != link:
            continue
        a, b = group["arms"].get(arm), group["arms"].get(ref)
        if a is None or b is None:
            continue
        if a["state"] != "measured" or b["state"] != "measured":
            continue
        ma, mb = a["makespan_ms"]["median"], b["makespan_ms"]["median"]
        if ma is None or mb is None or ma <= 0 or mb <= 0:
            continue
        out[group["key"]] = math.log(ma / mb)
    return out


def _comparisons(
    groups: Sequence[Mapping[str, Any]],
    arms: Sequence[str],
    links: Sequence[str],
    settings: AnalysisSettings,
) -> list[dict[str, Any]]:
    pairs: list[tuple[str, str, str]] = []
    for ref in _REFERENCES:
        if ref in arms:
            pairs += [(arm, ref, "baseline") for arm in arms if arm != ref]
    for arm, ref in _ABLATIONS:
        if arm in arms and ref in arms:
            pairs.append((arm, ref, "ablation"))
    out: list[dict[str, Any]] = []
    scopes: list[str | None] = [None, *links] if len(links) > 1 else [None]
    for arm, ref, kind in pairs:
        for link in scopes:
            deltas = _deltas(groups, arm, ref, link)
            scope = "all" if link is None else link
            seed = derive_seed(settings.seed, "paired", arm, ref, scope)
            out.append(
                {
                    "arm": arm,
                    "reference": ref,
                    "kind": kind,
                    "scope": scope,
                    **paired(deltas, seed=seed, settings=settings),
                }
            )
    return out


def _a7(
    groups: Sequence[Mapping[str, Any]],
    arms: Sequence[str],
    links: Sequence[str],
    settings: AnalysisSettings,
) -> dict[str, Any]:
    """Ethernet vs Wi-Fi per arm, paired over (family, size)."""
    if not all(link in links for link in _LINK_A7):
        return {
            "status": "not_run",
            "reason": "needs one campaign on each of ethernet and wifi",
            "links": list(links),
            "arms": {},
        }
    eth, wifi = _LINK_A7
    by_key = {(g["link"], g["family"], g["size"]): g for g in groups}
    out: dict[str, Any] = {}
    for arm in arms:
        deltas: dict[str, float] = {}
        for (link, family, size), group in sorted(by_key.items()):
            if link != eth:
                continue
            other = by_key.get((wifi, family, size))
            if other is None:
                continue
            a, b = group["arms"].get(arm), other["arms"].get(arm)
            if a is None or b is None:
                continue
            if a["state"] != "measured" or b["state"] != "measured":
                continue
            ma, mb = a["makespan_ms"]["median"], b["makespan_ms"]["median"]
            if ma and mb and ma > 0 and mb > 0:
                deltas[f"{family}/{size}"] = math.log(ma / mb)
        out[arm] = paired(
            deltas, seed=derive_seed(settings.seed, "a7", arm), settings=settings
        )
    return {"status": "evaluated", "reason": None, "links": list(links), "arms": out}


# ------------------------------------------------------------------ inputs


def _builds(raw: RawData) -> list[dict[str, Any]]:
    """The distinct builds the campaign's sessions ran on (D406)."""
    seen: dict[str, dict[str, Any]] = {}
    for session in raw.sessions:
        build = session.get("build") or {}
        env = session.get("environment") or {}
        item = {
            "git_sha": build.get("git_sha"),
            "package_version": build.get("package_version"),
            "source": build.get("source"),
            "controller_version": env.get("controller_version"),
        }
        seen["|".join(str(item[k]) for k in sorted(item))] = item
    return [seen[k] for k in sorted(seen)]


def _input(raw: RawData) -> dict[str, Any]:
    spec = raw.campaign["spec"]
    builds = _builds(raw)
    statuses = Counter(str(r.get("status")) for r in raw.runs)
    smoke = bool(spec.get("smoke"))
    reps = int(spec["repetitions"])
    return {
        "name": spec["name"],
        "link": spec["link"],
        "spec_digest": raw.campaign["spec_digest"],
        "schedule_digest": raw.campaign["schedule_digest"],
        "repetitions": reps,
        "jobs_per_run": spec.get("jobs_per_run", 1),
        "arms": list(spec["arms"]),
        "families": [f["family"] for f in spec["families"]],
        "smoke": smoke,
        "evidence": not smoke and reps >= 10,
        "runs_planned": raw.campaign["runs_planned"],
        "runs_recorded": len(raw.runs),
        "complete": len(raw.runs) == raw.campaign["runs_planned"],
        "statuses": dict(sorted(statuses.items())),
        "partial_lines": raw.partial_lines,
        "duplicate_runs": list(raw.duplicates),
        "sessions": len(raw.sessions),
        "builds": builds,
        "mixed_builds": len(builds) > 1,
    }


def analyze(
    raws: Sequence[RawData],
    *,
    settings: AnalysisSettings | None = None,
    offline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The analysis document (module docstring). ``offline`` = the A1-A4 /
    RQ2 block from :func:`.offline.offline_block` (None = not given)."""
    settings = settings or AnalysisSettings()
    if not raws:
        raise ValueError("no raw campaign to analyse")
    names = [str(r.campaign["spec"]["name"]) for r in raws]
    if len(set(names)) != len(names):
        raise ValueError(f"a campaign is given twice: {sorted(names)}")
    order = sorted(range(len(raws)), key=lambda i: names[i])
    raws = [raws[i] for i in order]
    arms = sorted(
        {str(a) for r in raws for a in r.campaign["spec"]["arms"]},
        key=lambda a: (a[0] != "B", a),
    )
    links = sorted({str(r.campaign["spec"]["link"]) for r in raws})
    runs, statuses, versions = _usable(raws)
    groups = _groups(runs, arms, settings)
    inputs = [_input(r) for r in raws]
    return {
        "analysis_version": ANALYSIS_VERSION,
        "settings": asdict(settings),
        "inputs": inputs,
        "evidence": all(i["evidence"] and i["complete"] for i in inputs)
        and not any(i["mixed_builds"] for i in inputs),
        "arms": arms,
        "links": links,
        "per_arm": _per_arm(groups, runs, arms, statuses, versions, settings),
        "groups": groups,
        "comparisons": _comparisons(groups, arms, links, settings),
        "a7": _a7(groups, arms, links, settings),
        "offline": dict(offline) if offline is not None else {"status": "not_given"},
    }
