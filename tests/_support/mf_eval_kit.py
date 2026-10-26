"""V1.0 G2 test support: load the ``scripts/eval`` harness and write
synthetic raw campaign folders (the exact on-disk format the runner writes).

``load()`` puts ``scripts/eval`` on ``sys.path`` (the scripts do the same for
themselves) so ``import evalkit`` works; :func:`write_raw` writes
``campaign.json`` + ``runs.jsonl`` + ``sessions.jsonl`` for a matrix whose
makespans come from a caller's function - pure noise, a real effect, failures.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

__all__ = ["EVAL_DIR", "SAMPLE_DIR", "job_record", "load", "write_raw"]

EVAL_DIR: Final = Path(__file__).resolve().parents[2] / "scripts" / "eval"
SAMPLE_DIR: Final = EVAL_DIR / "sample"
_T0: Final = dt.datetime(2026, 10, 27, 9, 0, tzinfo=dt.UTC)


def load() -> None:
    if str(EVAL_DIR) not in sys.path:
        sys.path.insert(0, str(EVAL_DIR))


def job_record(
    job_id: str,
    created: dt.datetime,
    makespan_ms: float,
    *,
    arm: str,
    state: str = "succeeded",
    mode: str = "offload",
    calibration: int | None = 3,
    ml: str | None = None,
) -> dict[str, Any]:
    """One measured job as the runner records it (``measure.measure_job``)."""
    finished = created + dt.timedelta(milliseconds=makespan_ms)
    run_ms = None if mode == "split" else round(makespan_ms * 0.8)
    return {
        "job_id": job_id,
        "state": state,
        "failure_type": None if state == "succeeded" else "exit_nonzero",
        "created_at": created.isoformat(),
        "finished_at": finished.isoformat(),
        "end_to_end_ms": round(makespan_ms),
        "queue_ms": 120,
        "run_ms": run_ms,
        "overhead_ms": None if run_ms is None else round(makespan_ms) - run_ms,
        "overhead_basis": "split_job" if run_ms is None else "end_to_end_minus_run",
        "mode": "local" if arm == "B0" else mode,
        "nodes": ["node_a"],
        "policy": "cost_model",
        "calibration_version": None if arm == "B4" else calibration,
        "ml_model_version": ml if arm in ("B6", "A5", "A6") else None,
        "attempts": 1,
        "transfer_bytes": 1000,
    }


def write_raw(
    folder: Path,
    *,
    name: str,
    arms: Sequence[str],
    families: Mapping[str, Sequence[str]],
    repetitions: int,
    makespan: Callable[[str, str, str, int], float],
    link: str = "wifi",
    smoke: bool = False,
    status: Callable[[str, str, str, int], str] | None = None,
    ml_version: str | None = "sha256:" + "a" * 64,
    builds: Sequence[Mapping[str, Any]] = (
        {"git_sha": "f" * 40, "package_version": "1.0.0rc1", "source": "bundle"},
    ),
) -> Path:
    """A raw folder: ``makespan(family, size, arm, block)`` -> ms per run."""
    load()
    from evalkit.raw import FORMAT
    from evalkit.schedule import schedule, schedule_digest
    from evalkit.spec import parse_spec, spec_digest

    spec = parse_spec(
        {
            "name": name,
            "seed": 7,
            "link": link,
            "repetitions": repetitions,
            "smoke": smoke,
            "arms": list(arms),
            "families": [
                {"family": family, "sizes": {s: _params(family) for s in sizes}}
                for family, sizes in families.items()
            ],
        }
    )
    runs = schedule(spec)
    folder.mkdir(parents=True, exist_ok=True)
    campaign = {
        "format": FORMAT,
        "spec": spec.model_dump(mode="json"),
        "spec_digest": spec_digest(spec),
        "schedule_digest": schedule_digest(runs),
        "runs_planned": len(runs),
        "origin_node_id": "node_a",
        "created_at": _T0.isoformat(),
    }
    (folder / "campaign.json").write_text(json.dumps(campaign, indent=2), "utf-8")
    lines = []
    for run in runs:
        created = _T0 + dt.timedelta(minutes=run.order)
        ms = makespan(run.family, run.size, run.arm, run.block)
        state = "ok" if status is None else status(run.family, run.size, run.arm, run.block)
        job = job_record(
            f"job_{run.order:026d}",
            created,
            ms,
            arm=run.arm,
            state="succeeded" if state == "ok" else "failed",
            mode="split" if run.family.startswith("split.") else "offload",
            ml=ml_version,
        )
        lines.append(
            json.dumps(
                {
                    "run_id": run.run_id,
                    "order": run.order,
                    "block": run.block,
                    "family": run.family,
                    "size": run.size,
                    "arm": run.arm,
                    "profile": run.profile,
                    "workload_seed": run.workload_seed,
                    "jobs": [job],
                    "error": None,
                    "status": state,
                    "recorded_at": (created + dt.timedelta(seconds=30)).isoformat(),
                },
                sort_keys=True,
            )
        )
    (folder / "runs.jsonl").write_text("\n".join(lines) + "\n", "utf-8")
    sessions = [
        json.dumps(
            {
                "started_at": _T0.isoformat(),
                "build": dict(build),
                "environment": {"controller_version": build.get("package_version")},
            },
            sort_keys=True,
        )
        for build in builds
    ]
    (folder / "sessions.jsonl").write_text("\n".join(sessions) + "\n", "utf-8")
    return folder


def _params(family: str) -> dict[str, Any]:
    if family == "cpu.primes":
        return {"limit": 100_000_000, "repeats": 4}
    if family == "split.hash_files":
        return {"files": 256, "size_kb": 256, "rounds": 100}
    if family == "cpu.sort":
        return {"n": 10_000_000}
    raise KeyError(family)
