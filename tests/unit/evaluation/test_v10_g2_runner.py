"""V1.0 G2 (D417/D419): the campaign loop against an in-memory fabric - every
planned run recorded once, resume after a crash without a duplicate job,
failures recorded (never dropped), timeouts cancelled, profile switches only
where the schedule needs them, and the per-job measurement from the
controller's own records."""

from __future__ import annotations

import datetime as dt
import itertools
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from mf_eval_kit import load

load()

from evalkit.arms import Profile  # noqa: E402
from evalkit.fabric import FabricError, wait_ready  # noqa: E402
from evalkit.measure import measure_job  # noqa: E402
from evalkit.raw import RawError, append_line, read_raw  # noqa: E402
from evalkit.runner import Options, RunnerError, run_campaign  # noqa: E402
from evalkit.schedule import schedule  # noqa: E402
from evalkit.spec import CampaignSpec, parse_spec  # noqa: E402

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 27, 9, 0, tzinfo=dt.UTC)


def _iso(when: dt.datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


class FakeFabric:
    """Jobs finish ``finish_after`` polls after submission; idempotency keys
    return the existing job (the controller's D-rule)."""

    def __init__(
        self,
        *,
        finish_after: int = 1,
        fail_submit: set[str] | None = None,
        never_finish: set[str] | None = None,
        state: str = "succeeded",
    ) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, str] = {}
        self.polls: dict[str, int] = {}
        self.submits: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.finish_after = finish_after
        self.fail_submit = fail_submit or set()
        self.never_finish = never_finish or set()
        self.state = state
        self.counter = itertools.count()

    def describe(self) -> dict[str, Any]:
        return {
            "controller_version": "1.0.0rc1",
            "nodes": [
                {"node_id": "node_a", "state": "online"},
                {"node_id": "node_b", "state": "online"},
            ],
        }

    def online_nodes(self) -> set[str]:
        return {"node_a", "node_b"}

    def submit(
        self,
        folder: Path,
        manifest: Mapping[str, Any],
        placement: Mapping[str, Any],
        *,
        submitter_node_id: str,
        idempotency_key: str,
    ) -> str:
        assert (folder / "compute.yaml").is_file()
        name = str(manifest["name"])
        if any(tag in name for tag in self.fail_submit):
            raise RuntimeError("controller said 503")
        if idempotency_key in self.keys:
            return self.keys[idempotency_key]
        n = next(self.counter)
        job_id = f"job_{n:026d}"
        self.keys[idempotency_key] = job_id
        created = T0 + dt.timedelta(minutes=n)
        self.jobs[job_id] = {
            "job_id": job_id,
            "name": name,
            "state": "queued",
            "created_at": _iso(created),
            "transitions": [],
            "attempts": [],
            "failure_type": None,
        }
        self.polls[job_id] = 0
        self.submits.append(
            {
                "job_id": job_id,
                "name": name,
                "placement": dict(placement),
                "submitter": submitter_node_id,
            }
        )
        return job_id

    def job(self, job_id: str) -> Mapping[str, Any]:
        job = self.jobs[job_id]
        self.polls[job_id] += 1
        stuck = any(tag in job["name"] for tag in self.never_finish)
        ready = self.polls[job_id] >= self.finish_after
        if job["state"] == "queued" and not stuck and ready:
            created = dt.datetime.fromisoformat(job["created_at"])
            job["state"] = self.state
            job["failure_type"] = None if self.state == "succeeded" else "exit"
            job["transitions"] = [
                {"to_state": "preparing", "at": _iso(created + dt.timedelta(0, 1))},
                {"to_state": "running", "at": _iso(created + dt.timedelta(0, 2))},
                {"to_state": self.state, "at": _iso(created + dt.timedelta(0, 12))},
            ]
            job["attempts"] = [
                {
                    "attempt_id": job_id + "-a1",
                    "role": "run",
                    "state": "done",
                    "transfers": [
                        {"source": "peer", "bytes": 500},
                        {"source": "cache", "bytes": 9000},
                    ],
                }
            ]
        return json.loads(json.dumps(job))

    def decisions(self, job_id: str) -> Sequence[Mapping[str, Any]]:
        return [
            {
                "outcome": "dispatched",
                "policy": "cost_model",
                "calibration_version": 3,
                "ml_model_version": None,
                "candidates": [
                    {"rank": 2, "mode": "local", "node_id": "node_a"},
                    {"rank": 1, "mode": "offload", "node_id": "node_b"},
                ],
                "actual": {"attempt_id": job_id + "-a1", "phases_ms": {"run": 9000}},
            }
        ]

    def cancel(self, job_id: str) -> None:
        self.cancelled.append(job_id)
        self.jobs[job_id]["state"] = "cancelled"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def _spec(**over: Any) -> CampaignSpec:
    raw: dict[str, Any] = {
        "name": "unit",
        "seed": 5,
        "link": "loopback",
        "repetitions": 2,
        "smoke": True,
        "run_timeout_s": 30,
        "arms": ["B0", "B1", "B5"],
        "families": [
            {
                "family": "cpu.primes",
                "sizes": {"s": {"limit": 10_000_000, "repeats": 1}},
            },
            {
                "family": "split.hash_files",
                "sizes": {"s": {"files": 4, "size_kb": 4, "rounds": 1}},
            },
        ],
    }
    raw.update(over)
    return parse_spec(raw)


def _options(tmp_path: Path, clock: Clock, **over: Any) -> Options:
    values: dict[str, Any] = {
        "raw_dir": tmp_path / "raw",
        "origin_node_id": "node_a",
        "build": {
            "git_sha": "f" * 40,
            "package_version": "1.0.0rc1",
            "source": "bundle",
        },
        "poll_s": 1.0,
        "log": lambda text: None,
        "clock": clock,
        "sleep": clock.sleep,
        "now": lambda: T0,
    }
    values.update(over)
    return Options(**values)


def test_every_planned_run_is_recorded_once_with_its_placement(tmp_path: Path) -> None:
    spec = _spec()
    fabric = FakeFabric()
    summary = run_campaign(fabric, spec, _options(tmp_path, Clock()))
    planned = schedule(spec)
    assert summary == {
        "executed": len(planned),
        "statuses": {"ok": len(planned)},
        "remaining": 0,
        "planned": len(planned),
    }
    raw = read_raw(tmp_path / "raw")
    assert [r["run_id"] for r in raw.runs] == [r.run_id for r in planned]
    assert len(fabric.submits) == len(planned)
    by_arm = {s["name"].split("-")[1]: s["placement"] for s in fabric.submits}
    assert by_arm["B0"] == {"node_id": "node_a", "local": True}
    assert by_arm["B1"] == {"policy": "round_robin"}
    assert by_arm["B5"] == {"policy": "cost_model", "objective": "min_time"}
    assert {s["submitter"] for s in fabric.submits} == {"node_a"}
    # the session records what ran (D406)
    assert raw.sessions[0]["build"]["source"] == "bundle"
    assert raw.sessions[0]["environment"]["controller_version"] == "1.0.0rc1"
    assert (tmp_path / "raw" / "profiles" / "calibrated.toml").exists()
    assert (tmp_path / "raw" / "profiles" / "calibrated.env").exists()
    # the job folders are not left behind
    assert not any((tmp_path / "raw" / "work").glob("*"))


def test_resume_skips_done_runs_and_finds_cut_short_runs_jobs_again(
    tmp_path: Path,
) -> None:
    spec = _spec()
    fabric = FakeFabric()
    clock = Clock()
    first = run_campaign(fabric, spec, _options(tmp_path, clock, max_runs=4))
    assert (first["executed"], first["remaining"]) == (4, len(schedule(spec)) - 4)
    runs_file = tmp_path / "raw" / "runs.jsonl"
    lines = runs_file.read_text("utf-8").splitlines(keepends=True)
    # a crash while writing the 4th line: it is cut short (no newline)
    runs_file.write_text("".join(lines[:3]) + lines[3][:40], "utf-8")
    submitted = len(fabric.submits)
    second = run_campaign(fabric, spec, _options(tmp_path, clock))
    assert second["remaining"] == 0
    assert second["executed"] == len(schedule(spec)) - 3
    # the cut-short run was done again, but its job was found by its key
    assert len(fabric.submits) == submitted + len(schedule(spec)) - 4
    raw = read_raw(tmp_path / "raw")
    assert len(raw.runs) == len(schedule(spec))
    assert raw.duplicates == ()
    assert len(raw.sessions) == 2
    assert raw.sessions[1]["already_done"] == 3
    assert raw.partial_lines == 0
    # the cut-off bytes are kept for the record
    cut = (tmp_path / "raw" / "runs.jsonl.cut").read_text("utf-8")
    assert cut == lines[3][:40] + "\n"


def test_a_different_campaign_or_origin_in_the_same_folder_is_refused(
    tmp_path: Path,
) -> None:
    clock = Clock()
    run_campaign(FakeFabric(), _spec(), _options(tmp_path, clock, max_runs=1))
    with pytest.raises(RawError, match="use a new --raw folder"):
        run_campaign(FakeFabric(), _spec(seed=6), _options(tmp_path, clock))
    with pytest.raises(RawError, match="origin"):
        run_campaign(
            FakeFabric(), _spec(), _options(tmp_path, clock, origin_node_id="node_b")
        )


def test_a_failed_submission_is_recorded_and_the_campaign_goes_on(
    tmp_path: Path,
) -> None:
    spec = _spec()
    fabric = FakeFabric(fail_submit={"-B1-"})
    summary = run_campaign(fabric, spec, _options(tmp_path, Clock()))
    raw = read_raw(tmp_path / "raw")
    errors = [r for r in raw.runs if r["status"] == "submit_error"]
    assert {r["arm"] for r in errors} == {"B1"}
    assert all("controller said 503" in r["error"] for r in errors)
    assert summary["statuses"]["submit_error"] == len(errors) == 4
    assert summary["remaining"] == 0


def test_a_run_past_its_timeout_is_cancelled_and_recorded(tmp_path: Path) -> None:
    spec = _spec(run_timeout_s=5)
    fabric = FakeFabric(never_finish={"-B5-"})
    run_campaign(fabric, spec, _options(tmp_path, Clock()))
    raw = read_raw(tmp_path / "raw")
    timed = [r for r in raw.runs if r["status"] == "timeout"]
    assert {r["arm"] for r in timed} == {"B5"} and len(timed) == 4
    assert len(fabric.cancelled) == 4
    assert all(r["jobs"][0]["end_to_end_ms"] is None for r in timed)


def test_failed_jobs_make_a_failed_run(tmp_path: Path) -> None:
    run_campaign(FakeFabric(state="failed"), _spec(), _options(tmp_path, Clock()))
    raw = read_raw(tmp_path / "raw")
    assert {r["status"] for r in raw.runs} == {"failed"}


def test_several_jobs_per_run_get_distinct_keys(tmp_path: Path) -> None:
    fabric = FakeFabric()
    run_campaign(fabric, _spec(jobs_per_run=3), _options(tmp_path, Clock()))
    raw = read_raw(tmp_path / "raw")
    assert all(len(r["jobs"]) == 3 for r in raw.runs)
    assert len(fabric.keys) == 3 * len(raw.runs)


class Switcher:
    def __init__(self, fail: bool = False) -> None:
        self.applied: list[str] = []
        self.fail = fail

    def apply(self, profile: Profile, toml_path: Path) -> None:
        assert toml_path.read_text("utf-8").startswith("# MeshForge evaluation")
        if self.fail:
            raise FabricError("profile_hook", "exit 1")
        self.applied.append(profile.name)


def test_profiles_switch_once_per_profile_per_block(tmp_path: Path) -> None:
    spec = _spec(arms=["B4", "B5"], repetitions=3)
    switcher = Switcher()
    run_campaign(FakeFabric(), spec, _options(tmp_path, Clock(), switcher=switcher))
    # 2 profiles x 3 blocks, minus the joins where consecutive blocks start
    # with the profile the previous one ended on
    expected = []
    for run in schedule(spec):
        if not expected or expected[-1] != run.profile:
            expected.append(run.profile)
    assert switcher.applied == expected
    assert set(expected) == {"uncalibrated", "calibrated"}


def test_more_than_one_profile_needs_a_hook_and_a_failing_hook_stops(
    tmp_path: Path,
) -> None:
    spec = _spec(arms=["B4", "B5"])
    with pytest.raises(RunnerError) as info:
        run_campaign(FakeFabric(), spec, _options(tmp_path, Clock()))
    assert info.value.kind == "profile_hook_needed"
    with pytest.raises(RunnerError) as info:
        run_campaign(
            FakeFabric(),
            spec,
            _options(tmp_path, Clock(), switcher=Switcher(fail=True)),
        )
    assert info.value.kind == "profile_hook"


def test_wait_ready_times_out_with_the_missing_nodes() -> None:
    class Half(FakeFabric):
        def online_nodes(self) -> set[str]:
            return {"node_a"}

    clock = Clock()
    with pytest.raises(FabricError, match="node_b") as info:
        wait_ready(
            Half(), {"node_a", "node_b"}, timeout_s=5, clock=clock, sleep=clock.sleep
        )
    assert info.value.kind == "not_ready"


# ------------------------------------------------------------------ measure


def test_measure_uses_the_controller_clock_and_the_run_phase() -> None:
    created = T0
    job = {
        "job_id": "j1",
        "state": "succeeded",
        "created_at": _iso(created),
        "transitions": [
            {"to_state": "preparing", "at": _iso(created + dt.timedelta(seconds=2))},
            {"to_state": "succeeded", "at": _iso(created + dt.timedelta(seconds=20))},
        ],
        "attempts": [
            {
                "attempt_id": "a1",
                "role": "run",
                "state": "done",
                "transfers": [{"source": "controller", "bytes": 10}],
            }
        ],
    }
    decisions = [
        {
            "outcome": "dispatched",
            "policy": "cost_model",
            "calibration_version": 4,
            "ml_model_version": "sha256:x",
            "candidates": [{"rank": 1, "mode": "offload", "node_id": "n2"}],
            "actual": {"attempt_id": "a1", "phases_ms": {"run": 15000}},
        }
    ]
    got = measure_job(job, decisions)
    assert got["end_to_end_ms"] == 20000
    assert got["queue_ms"] == 2000
    assert (got["run_ms"], got["overhead_ms"]) == (15000, 5000)
    assert got["overhead_basis"] == "end_to_end_minus_run"
    assert (got["mode"], got["nodes"]) == ("offload", ["n2"])
    assert (got["calibration_version"], got["ml_model_version"]) == (4, "sha256:x")
    assert got["transfer_bytes"] == 10


def test_measure_a_split_job_has_no_overhead_and_a_set() -> None:
    job = {
        "job_id": "j2",
        "state": "succeeded",
        "created_at": _iso(T0),
        "transitions": [{"to_state": "succeeded", "at": _iso(T0 + dt.timedelta(0, 9))}],
        "attempts": [{"attempt_id": "l1", "role": "lane", "state": "done"}],
    }
    decisions = [
        {
            "outcome": "dispatched",
            "candidates": [{"rank": 1, "mode": "split", "node_set": ["n1", "n2"]}],
        }
    ]
    got = measure_job(job, decisions)
    assert (got["overhead_ms"], got["overhead_basis"]) == (None, "split_job")
    assert got["nodes"] == ["n1", "n2"]
    assert got["end_to_end_ms"] == 9000


def test_measure_a_split_job_on_one_lane_is_still_a_split_job() -> None:
    job = {
        "job_id": "j4",
        "state": "succeeded",
        "created_at": _iso(T0),
        "transitions": [{"to_state": "succeeded", "at": _iso(T0 + dt.timedelta(0, 5))}],
        "attempts": [
            {"attempt_id": "l1", "role": "lane", "state": "done"},
            {"attempt_id": "c1", "role": "combine", "state": "done"},
        ],
    }
    decisions = [
        {
            "outcome": "dispatched",
            "candidates": [{"rank": 1, "mode": "local", "node_id": "n1"}],
        }
    ]
    got = measure_job(job, decisions)
    assert (got["mode"], got["overhead_basis"]) == ("local", "split_job")
    assert got["overhead_ms"] is None


def test_measure_an_unfinished_job_or_no_decision() -> None:
    got = measure_job({"job_id": "j3", "state": "running", "created_at": _iso(T0)}, [])
    assert got["end_to_end_ms"] is None
    assert (got["mode"], got["policy"]) == (None, None)
    assert got["overhead_basis"] == "no_run_phase"


# ------------------------------------------------------------------ raw


def test_raw_reader_rejects_corruption_but_tolerates_a_cut_short_tail(
    tmp_path: Path,
) -> None:
    run_campaign(FakeFabric(), _spec(), _options(tmp_path, Clock(), max_runs=2))
    folder = tmp_path / "raw"
    runs = folder / "runs.jsonl"
    append_line(runs, {"run_id": "b000-x", "arm": "B0"})
    text = runs.read_text("utf-8")
    runs.write_text(text + text.splitlines()[0] + "\n", "utf-8")  # a duplicate
    data = read_raw(folder)
    assert len(data.runs) == 3 and len(data.duplicates) == 1
    runs.write_text("not json\n" + text, "utf-8")
    with pytest.raises(RawError, match="line 1 is not JSON"):
        read_raw(folder)
    (folder / "campaign.json").write_text('{"format": "other"}', "utf-8")
    with pytest.raises(RawError, match="is not a"):
        read_raw(folder)
    with pytest.raises(RawError, match="not a raw campaign folder"):
        read_raw(tmp_path)
