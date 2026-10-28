"""V1.0 G1 (roadmap 1.0.2): the soak CLIs end to end on files - ``soak.py
plan`` / ``report`` exit codes and the load generator's idempotent resume."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import soak_world as w

import soak
import soak_load
from _soak import jsonl
from _soak.ledger import reduce
from _soak.plan import PLAN_FILE, SCHEDULE_FILE, VERDICT_FILE, write_json

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def monotonic(self) -> float:
        return self.t

    def wall(self) -> Any:
        return w.T0 + timedelta(seconds=self.t)

    def sleep(self, s: float) -> None:
        self.t += s


def _targets(tmp_path: Path) -> Path:
    path = tmp_path / "targets.json"
    path.write_text(
        json.dumps({"targets": [t.as_dict() for t in w.plan().targets]}),
        encoding="utf-8",
    )
    return path


def test_plan_writes_plan_and_schedule_and_refuses_overwrite(
    tmp_path: Path, capsys: Any
) -> None:
    run = tmp_path / "run"
    argv = ["plan", "--out", str(run), "--targets", str(_targets(tmp_path)), "--seed", "3",
            "--start", w.T0.isoformat(), "--hours", "1", "--quiet-h", "0.1", "--tail-h", "0.2",
            "--warmup-min", "1", "--chaos-gap-min", "5-8", "--duration-scale", "0.2"]  # fmt: skip
    assert soak.main(argv, clock=Clock()) == soak.EXIT_OK
    plan = json.loads((run / PLAN_FILE).read_text())
    schedule = json.loads((run / SCHEDULE_FILE).read_text())
    assert plan["nonce"] == schedule["nonce"]
    assert len(schedule["actions"]) >= 6
    assert "chaos:" in capsys.readouterr().out
    assert soak.main(argv, clock=Clock()) == soak.EXIT_USAGE
    bad = [*argv[:-2], "--kinds", "kill_agent,unplug_moon", "--force"]
    assert soak.main(bad, clock=Clock()) == soak.EXIT_USAGE


def _write_run(run: Path, world: Any) -> None:
    write_json(run / PLAN_FILE, world.plan.as_dict())
    for label, records in world.samples.items():
        for r in records:
            jsonl.append(run / f"samples.{label.lower()}.jsonl", r)
    for r in w.ledger_records(
        [(s.seq, f"job{s.seq}", s.offset_s) for s in world.specs]
    ):
        jsonl.append(run / "ledger.jsonl", r)
    write_json(run / "jobs-final.json", world.final)
    for i in range(2):
        start = w.T0 + timedelta(seconds=1000 + 600 * i)
        jsonl.append(
            run / "chaos-log.jsonl",
            {"type": "end", "id": f"A{i}", "kind": "kill_job_tree", "target": "L2",
             "started_at": start.isoformat(), "effect_until": start.isoformat(),
             "outcome": "done"},
        )  # fmt: skip


def test_report_exit_codes_follow_the_verdict(tmp_path: Path, capsys: Any) -> None:
    run = tmp_path / "run"
    world = w.world()
    _write_run(run, world)
    md = tmp_path / "report.md"
    # no DB samples, no fabric record: INCONCLUSIVE - never a pass
    assert (
        soak.main(["report", str(run), "--md", str(md)], clock=Clock())
        == soak.EXIT_INCONCLUSIVE
    )
    verdict = json.loads((run / VERDICT_FILE).read_text())
    assert verdict["verdict"] == "INCONCLUSIVE"
    assert "VERDICT: INCONCLUSIVE" in capsys.readouterr().out
    assert md.read_text().startswith("# V1.0 soak")
    # a lost job turns it into FAIL
    final = dict(world.final)
    final["jobs"] = {**final["jobs"], "job0": None}
    write_json(run / "jobs-final.json", final)
    assert (
        soak.main(["report", str(run), "--md", str(md)], clock=Clock())
        == soak.EXIT_FAIL
    )


def test_report_on_a_folder_without_plan_is_an_io_error(tmp_path: Path) -> None:
    assert soak.main(["report", str(tmp_path)], clock=Clock()) == soak.EXIT_IO


# -------------------------------------------------------- load generator
@dataclass
class FakeResponse:
    status_code: int
    body: dict[str, Any]

    def json(self) -> dict[str, Any]:
        return self.body


@dataclass
class FakeLab:
    """``POST /v1/jobs`` with the controller's idempotency semantics."""

    jobs_by_key: dict[str, str] = field(default_factory=dict)
    posts: list[str] = field(default_factory=list)
    cancels: list[str] = field(default_factory=list)
    down: bool = False
    crash_after_post: int | None = None

    def ledger_bundle(self) -> str:
        return "sha256:" + "b" * 64

    def family_tree(
        self, family: str, params: dict[str, Any], seed: int
    ) -> tuple[str, dict[str, Any]]:
        return "sha256:" + "c" * 64, {"name": family, "entrypoint": ["python", "x.py"]}

    def call(self, method: str, path: str, **kw: Any) -> FakeResponse:
        if self.down:
            raise soak_load.TransientError("ConnectError")
        if path.endswith("/cancel"):
            self.cancels.append(path.split("/")[3])
            return FakeResponse(202, {})
        key = kw["headers"]["idempotency-key"]
        self.posts.append(key)
        replay = key in self.jobs_by_key
        job_id = self.jobs_by_key.setdefault(key, f"job_{len(self.jobs_by_key)}")
        if (
            self.crash_after_post is not None
            and len(self.posts) == self.crash_after_post
        ):
            raise KeyboardInterrupt  # the generator dies before it logs the answer
        return FakeResponse(200 if replay else 201, {"job_id": job_id})


def _generator(tmp_path: Path, lab: FakeLab, start_s: float = 0.0, **kw: Any) -> Any:
    clock = Clock(start_s)
    plan = w.plan(hours=0.25, quiet_h=0.05, tail_h=0.05, job_gap_s=20.0)
    gen = soak_load.Generator(
        lab, plan, tmp_path / "ledger.jsonl", catchup_s=kw.get("catchup_s", 1800.0),
        retry_for_s=kw.get("retry_for_s", 1800.0), now=clock.wall, sleep=clock.sleep,
    )  # fmt: skip
    return gen, clock


def test_generator_submits_every_job_and_cancels_planned_ones(tmp_path: Path) -> None:
    lab = FakeLab()
    gen, _ = _generator(tmp_path, lab)
    assert gen.run() == soak_load.EXIT_OK
    state = reduce(jsonl.load(tmp_path / "ledger.jsonl").records)
    assert len(state.job_ids()) == len(gen.specs)
    assert len(set(lab.posts)) == len(lab.posts)
    planned = {s.seq for s in gen.specs if s.cancel_after_s is not None}
    assert {s for s, e in state.entries.items() if e.cancel_status} == planned


def test_crash_between_post_and_answer_never_duplicates_a_job(tmp_path: Path) -> None:
    lab = FakeLab(crash_after_post=3)
    gen, clock = _generator(tmp_path, lab)
    with pytest.raises(KeyboardInterrupt):
        gen.run()
    lab.crash_after_post = None
    again, _ = _generator(tmp_path, lab, start_s=clock.t)
    assert again.run() == soak_load.EXIT_OK
    state = reduce(jsonl.load(tmp_path / "ledger.jsonl").records)
    assert not state.problems
    assert not state.unresolved()
    assert len(state.job_ids()) == len(again.specs) == len(lab.jobs_by_key)
    third_key = lab.posts[2]
    assert lab.posts.count(third_key) == 2  # re-sent with the same key: a replay


def test_late_jobs_are_skipped_not_burst(tmp_path: Path) -> None:
    lab = FakeLab()
    gen, _ = _generator(tmp_path, lab, start_s=600.0, catchup_s=60.0)
    gen.run()
    state = reduce(jsonl.load(tmp_path / "ledger.jsonl").records)
    skipped = [s for s, e in state.entries.items() if e.final_failure == "skipped_late"]
    assert skipped
    assert all(gen.specs[s].offset_s < 540 for s in skipped)


def test_controller_down_too_long_gives_up_the_job(tmp_path: Path) -> None:
    lab = FakeLab(down=True)
    gen, _ = _generator(tmp_path, lab, retry_for_s=30.0, catchup_s=5.0)
    assert gen.run() == soak_load.EXIT_REFUSED
    state = reduce(jsonl.load(tmp_path / "ledger.jsonl").records)
    assert all(e.final_failure for e in state.entries.values())
