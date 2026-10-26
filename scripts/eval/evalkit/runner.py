"""The campaign loop (D417/D419): schedule -> jobs -> raw data, resumable.

For each planned run (:mod:`.schedule`), in order, skipping the runs
``runs.jsonl`` already holds:

1. if the run's controller profile is not the current one, apply it (the
   :class:`~.fabric.ProfileSwitcher`) and wait until every node of the
   session's starting inventory is ONLINE again;
2. build the job folder (``meshforge.bench.workloads.build_job``, the run's
   own workload seed), submit ``jobs_per_run`` copies with the arm's
   placement and idempotency keys derived from (spec digest, run id, copy) -
   a resumed run finds its jobs again instead of submitting new ones;
3. poll until every job is terminal or ``run_timeout_s`` passed (then cancel
   what is left: status ``timeout``);
4. measure every job from the controller's own record (:mod:`.measure`) and
   append the run's line to ``runs.jsonl``.

A run whose submission fails is recorded ``submit_error`` with the error -
never silently dropped. A profile hook that fails, or a fabric that does not
come back, stops the campaign (exit code 3); starting it again resumes.
"""

from __future__ import annotations

import datetime as dt
import platform
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from evalkit.arms import ARMS, PROFILES, profile_toml
from evalkit.canonical import sha256_bytes, write_text
from evalkit.fabric import Fabric, FabricError, ProfileSwitcher, wait_ready
from evalkit.measure import TERMINAL, measure_job
from evalkit.raw import (
    FORMAT,
    RUNS_FILE,
    SESSIONS_FILE,
    RawError,
    append_line,
    read_raw,
    write_campaign,
)
from evalkit.schedule import PlannedRun, schedule, schedule_digest
from evalkit.spec import CampaignSpec, spec_digest

__all__ = ["Options", "RunnerError", "run_campaign"]

_MAX_NAME: Final = 60


class RunnerError(RuntimeError):
    """The campaign cannot continue (typed by ``kind``; resumable)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind


@dataclass(slots=True)
class Options:
    raw_dir: Path
    origin_node_id: str
    build: Mapping[str, Any]
    """git SHA, package version, python, platform (recorded per session)."""
    switcher: ProfileSwitcher | None = None
    poll_s: float = 1.0
    ready_timeout_s: float = 600.0
    max_runs: int | None = None
    """Stop after this many runs this session (tests; a partial campaign)."""
    log: Callable[[str], None] = field(
        default=lambda text: print(text, file=sys.stderr)
    )
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], dt.datetime] = field(default=lambda: dt.datetime.now(dt.UTC))


def _idempotency_key(digest: str, run_id: str, copy: int) -> str:
    return "eval-" + sha256_bytes(f"{digest}|{run_id}|{copy}".encode())[:40]


def _job_name(run: PlannedRun, copy: int) -> str:
    from meshforge.common.manifest import sanitize_name

    text = f"eval-{run.arm}-{run.family}-{run.size}-b{run.block}-c{copy}"
    return sanitize_name(text[:_MAX_NAME])


def _build(spec: CampaignSpec, run: PlannedRun, dest: Path) -> Any:
    """The run's job folder (the smoke rule lifts the 5 s window only)."""
    from meshforge.bench.workloads import base, build_job

    params = spec.params(run.family, run.size)
    if dest.exists():
        shutil.rmtree(dest)
    if not spec.smoke:
        return build_job(run.family, params, run.workload_seed, dest)
    saved = base.MIN_RUNTIME_S
    base.MIN_RUNTIME_S = 0.0  # type: ignore[misc]
    try:
        return build_job(run.family, params, run.workload_seed, dest)
    finally:
        base.MIN_RUNTIME_S = saved  # type: ignore[misc]


def _prepare_folder(
    spec: CampaignSpec, runs: list[PlannedRun], options: Options
) -> set[str]:
    """Create or check the raw folder; the run ids already done."""
    folder = options.raw_dir
    folder.mkdir(parents=True, exist_ok=True)
    digest = spec_digest(spec)
    sched = schedule_digest(runs)
    campaign_file = folder / "campaign.json"
    if not campaign_file.exists():
        write_campaign(
            folder,
            {
                "format": FORMAT,
                "spec": spec.model_dump(mode="json"),
                "spec_digest": digest,
                "schedule_digest": sched,
                "runs_planned": len(runs),
                "origin_node_id": options.origin_node_id,
                "created_at": options.now().isoformat(),
            },
        )
    data = read_raw(folder)
    if data.campaign.get("spec_digest") != digest:
        raise RawError(
            f"{folder} holds campaign {data.campaign.get('spec', {}).get('name')!r} "
            f"({data.campaign.get('spec_digest')}), not this file ({digest}); "
            "use a new --raw folder for a different campaign"
        )
    if data.campaign.get("schedule_digest") != sched:
        raise RawError("the schedule changed for the same spec (a harness change)")
    if data.campaign.get("origin_node_id") != options.origin_node_id:
        raise RawError(
            f"the campaign's origin is {data.campaign.get('origin_node_id')}, "
            f"this session's is {options.origin_node_id}"
        )
    profiles = folder / "profiles"
    for name in sorted({ARMS[a].profile for a in spec.arms}):
        write_text(profiles / f"{name}.toml", profile_toml(PROFILES[name]))
    if data.partial_lines:
        options.log(f"ignoring {data.partial_lines} line(s) cut short by a crash")
    return {str(r["run_id"]) for r in data.runs}


def _wait_terminal(
    fabric: Fabric, job_ids: list[str], spec: CampaignSpec, options: Options
) -> tuple[dict[str, Mapping[str, Any]], bool]:
    """Poll the jobs; ``(last detail per job, timed out?)``."""
    deadline = options.clock() + spec.run_timeout_s
    details: dict[str, Mapping[str, Any]] = {}
    while True:
        for job_id in job_ids:
            if str(details.get(job_id, {}).get("state")) in TERMINAL:
                continue
            try:
                details[job_id] = fabric.job(job_id)
            except Exception as exc:  # noqa: BLE001 - transient; retried next poll
                options.log(f"read {job_id}: {exc!r}")
        if all(str(details.get(j, {}).get("state")) in TERMINAL for j in job_ids):
            return details, False
        if options.clock() >= deadline:
            return details, True
        options.sleep(options.poll_s)


def _execute(
    fabric: Fabric, spec: CampaignSpec, run: PlannedRun, options: Options
) -> dict[str, Any]:
    arm = ARMS[run.arm]
    digest = spec_digest(spec)
    work = options.raw_dir / "work" / run.run_id
    record: dict[str, Any] = {
        "run_id": run.run_id,
        "order": run.order,
        "block": run.block,
        "family": run.family,
        "size": run.size,
        "arm": run.arm,
        "profile": run.profile,
        "workload_seed": run.workload_seed,
        "jobs": [],
        "error": None,
    }
    try:
        built = _build(spec, run, work)
        placement = arm.placement(options.origin_node_id, spec.objective)
        job_ids: list[str] = []
        for copy in range(spec.jobs_per_run):
            manifest = built.manifest.canonical_dict()
            manifest["name"] = _job_name(run, copy)
            job_ids.append(
                fabric.submit(
                    built.path,
                    manifest,
                    placement,
                    submitter_node_id=options.origin_node_id,
                    idempotency_key=_idempotency_key(digest, run.run_id, copy),
                )
            )
    except Exception as exc:  # noqa: BLE001 - recorded, the campaign goes on
        record["status"] = "submit_error"
        record["error"] = f"{type(exc).__name__}: {exc}"[:500]
        return record
    finally:
        shutil.rmtree(work, ignore_errors=True)
    details, timed_out = _wait_terminal(fabric, job_ids, spec, options)
    if timed_out:
        for job_id in job_ids:
            if str(details.get(job_id, {}).get("state")) not in TERMINAL:
                try:
                    fabric.cancel(job_id)
                except Exception as exc:  # noqa: BLE001
                    options.log(f"cancel {job_id}: {exc!r}")
    jobs = []
    for job_id in job_ids:
        detail = details.get(job_id) or {"job_id": job_id, "state": "unknown"}
        try:
            decisions = fabric.decisions(job_id)
        except Exception as exc:  # noqa: BLE001
            options.log(f"decisions {job_id}: {exc!r}")
            decisions = []
        jobs.append(measure_job(detail, decisions))
    record["jobs"] = jobs
    if timed_out:
        record["status"] = "timeout"
    elif all(j["state"] == "succeeded" for j in jobs):
        record["status"] = "ok"
    else:
        record["status"] = "failed"
    return record


def _apply_profile(
    fabric: Fabric,
    name: str,
    options: Options,
    expected: set[str],
) -> None:
    if options.switcher is None:
        return
    profile = PROFILES[name]
    toml_path = options.raw_dir / "profiles" / f"{name}.toml"
    options.log(f"profile -> {name}")
    try:
        options.switcher.apply(profile, toml_path)
        wait_ready(
            fabric,
            expected,
            timeout_s=options.ready_timeout_s,
            poll_s=options.poll_s,
            clock=options.clock,
            sleep=options.sleep,
        )
    except FabricError as exc:
        raise RunnerError(exc.kind, str(exc)) from exc


def run_campaign(
    fabric: Fabric, spec: CampaignSpec, options: Options
) -> dict[str, Any]:
    """Run (or resume) the campaign; a summary of this session."""
    runs = schedule(spec)
    needed = sorted({ARMS[a].profile for a in spec.arms})
    if len(needed) > 1 and options.switcher is None:
        raise RunnerError(
            "profile_hook_needed",
            f"the arms need controller profiles {needed}; give --profile-hook "
            "(or run one profile's arms per campaign)",
        )
    done = _prepare_folder(spec, runs, options)
    environment = fabric.describe()
    expected = {
        str(n["node_id"])
        for n in environment.get("nodes", ())
        if n.get("state") == "online"
    }
    session: dict[str, Any] = {
        "started_at": options.now().isoformat(),
        "build": dict(options.build),
        "environment": environment,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "profile_switching": options.switcher is not None,
        "already_done": len(done),
    }
    append_line(options.raw_dir / SESSIONS_FILE, session)
    current: str | None = None
    executed = 0
    statuses: dict[str, int] = {}
    for run in runs:
        if run.run_id in done:
            continue
        if options.max_runs is not None and executed >= options.max_runs:
            break
        if run.profile != current:
            _apply_profile(fabric, run.profile, options, expected)
            current = run.profile
        record = _execute(fabric, spec, run, options)
        record["recorded_at"] = options.now().isoformat()
        append_line(options.raw_dir / RUNS_FILE, record)
        executed += 1
        statuses[record["status"]] = statuses.get(record["status"], 0) + 1
        options.log(f"[{run.order + 1}/{len(runs)}] {run.run_id}: {record['status']}")
    remaining = len(runs) - len(done) - executed
    return {
        "executed": executed,
        "statuses": dict(sorted(statuses.items())),
        "remaining": remaining,
        "planned": len(runs),
    }
