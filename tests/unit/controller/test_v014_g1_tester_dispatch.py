"""V0.14 G1 tester r1 - a replay is never a condition of a dispatch (D292).

Over the cap (exactly at, one under, a huge fabric), a codec error of any
kind (ValueError, TypeError, RecursionError, MemoryError-like, KeyError), a
failing canonical encoder: the job still gets its decision, the record has no
replay and no replay_version, the right counter moves, the log names the
event - and the *next* decision is unaffected.
"""

from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from typing import Any

import pytest
from structlog.testing import capture_logs

from meshforge.common.ids import new_job_id
from meshforge.common.states import JobState
from meshforge.controller.jobs import planning as planning_module
from meshforge.controller.jobs.execution_store import Dispatch, DispatchFailure, Wait
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.queue_reads import FabricRows, NodeRow
from meshforge.controller.jobs.store import JobRecord, JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner import replay as replay_module
from meshforge.controller.planner.replay import canonical_json, replay_size
from meshforge.controller.settings import JobApiSettings, PlannerSettings

pytestmark = pytest.mark.unit

A = "node_01AAAAAAAAAAAAAAAAAAAAAAAA"
TREE = "sha256:" + "c" * 64
WALL = dt.datetime(2026, 10, 21, 9, 30, tzinfo=dt.UTC)


def _ids(n: int) -> list[str]:
    return [f"node_01{i:024d}" for i in range(n)]


class _Manager:
    def __init__(self, nodes: list[str]) -> None:
        self.nodes = set(nodes)

    def get(self, node_id: str) -> Any:
        if node_id not in self.nodes:
            return None
        return SimpleNamespace(
            state=NodeState.ONLINE,
            rtt_ewma_ms=None,
            session_id="s",
            policy=None,
            policy_slots=None,
            clock=SimpleNamespace(latest=None),
        )

    def get_lingering(self, _node_id: str) -> Any:
        return None


def _planner(nodes: list[str], max_bytes: int = 65_536) -> DispatchPlanner:
    return DispatchPlanner(
        manager=_Manager(nodes),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        planner=PlannerSettings(replay_max_bytes=max_bytes),
        clock=lambda: 1000.0,
        wall_clock=lambda: WALL,
        started_at=0.0,
    )


def _job() -> JobRecord:
    return JobRecord(
        id=str(new_job_id()),
        name="demo",
        state=JobState.QUEUED,
        manifest={"name": "d", "entrypoint": ["python", "m.py"]},
        bundle_digest=None,
        tree_id=TREE,
        placement={"mode": "auto"},
        idempotency_key=None,
        request_hash="h" * 64,
        attempt_epoch=0,
        failure_type=None,
        failure_detail=None,
        created_at=WALL,
        updated_at=WALL,
        scheduling=JobScheduling(input_bytes=10**9, submitter_node_id=A),
    )


def _fabric(ids: list[str], caps: list[str] | None = None) -> FabricRows:
    caps = caps if caps is not None else ["exec", "exec-subprocess"]
    return FabricRows(
        nodes={
            n: NodeRow(
                node_id=n,
                state="online",
                hardware={"capabilities": caps},
                hardware_scores=None,
                cpu_slots=2,
                gpu_slots=0,
            )
            for n in ids
        },
        holdings={ids[0]: frozenset({TREE})},
    )


def test_exact_cap_boundary_through_the_dispatcher() -> None:
    ids = _ids(3)
    job = _job()
    first = _planner(ids).decide(job, _fabric(ids))
    assert isinstance(first, Dispatch) and first.record is not None
    assert first.record.replay is not None
    size = replay_size(first.record.replay)

    at = _planner(ids, max_bytes=size)
    dec_at = at.decide(job, _fabric(ids))
    assert isinstance(dec_at, Dispatch) and dec_at.record is not None
    assert dec_at.record.replay is not None and at.replays_over_cap == 0

    under = _planner(ids, max_bytes=size - 1)
    dec_under = under.decide(job, _fabric(ids))
    assert isinstance(dec_under, Dispatch) and dec_under.record is not None
    assert dec_under.record.replay is None and dec_under.record.replay_version is None
    assert under.replays_over_cap == 1
    # the decision itself is the same with or without the replay
    assert dec_under.record.candidates == dec_at.record.candidates
    assert dec_under.record.snapshot_digest == dec_at.record.snapshot_digest
    assert dec_under.record.chosen_node_id == dec_at.record.chosen_node_id


def test_a_real_big_fabric_exceeds_the_default_cap_and_still_dispatches() -> None:
    ids = _ids(600)
    planner = _planner(ids)  # default 64 KiB
    with capture_logs() as logs:
        decision = planner.decide(_job(), _fabric(ids))
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert decision.record.replay is None and decision.record.replay_version is None
    assert planner.replays_over_cap == 1 and planner.replay_failures == 0
    assert any(e["event"] == "controller.jobs.replay_over_cap" for e in logs)


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("Out of range float values are not JSON compliant"),
        TypeError("not JSON serializable"),
        KeyError("x"),
        RecursionError("deep"),
        OverflowError("too big"),
        UnicodeEncodeError("utf-8", "x", 0, 1, "lone surrogate"),
        AttributeError("nope"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_codec_errors_of_any_kind_never_fail_the_dispatch(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    ids = _ids(2)

    def broken(*_a: Any, **_k: Any) -> Any:
        raise exc

    monkeypatch.setattr(planning_module, "bounded_replay", broken)
    planner = _planner(ids)
    job = _job()
    with capture_logs() as logs:
        a = planner.decide(job, _fabric(ids))
        b = planner.decide(job, _fabric(ids, caps=["docker"]))  # failed decision
        c = _planner([]).decide(job, _fabric(ids))  # waiting (own planner)
    assert isinstance(a, Dispatch) and a.record is not None
    assert isinstance(b, DispatchFailure) and b.record is not None
    assert isinstance(c, Wait) and c.record is not None
    assert a.record.replay is None and b.record.replay is None
    assert a.record.replay_version is None and b.record.replay_version is None
    assert planner.replay_failures == 2 and planner.replays_over_cap == 0
    assert planner.planning_errors == 0
    events = [e for e in logs if e["event"] == "controller.jobs.replay_failed"]
    assert len(events) == 3  # a, b and the second planner's c
    assert {e["error_type"] for e in events} == {type(exc).__name__}


def test_the_canonical_encoder_failing_inside_the_real_codec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure is inside bounded_replay (not a stub of it)."""
    ids = _ids(2)

    def boom(_replay: Any) -> str:
        raise ValueError("Out of range float values are not JSON compliant")

    monkeypatch.setattr(replay_module, "canonical_json", boom)
    planner = _planner(ids)
    decision = planner.decide(_job(), _fabric(ids))
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert decision.record.replay is None and planner.replay_failures == 1


def test_a_failure_does_not_poison_the_next_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _ids(2)
    real = planning_module.bounded_replay
    calls = {"n": 0}

    def flaky(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(planning_module, "bounded_replay", flaky)
    planner = _planner(ids)
    first = planner.decide(_job(), _fabric(ids))
    second = planner.decide(_job(), _fabric(ids))
    assert isinstance(first, Dispatch) and first.record is not None
    assert isinstance(second, Dispatch) and second.record is not None
    assert first.record.replay is None and second.record.replay is not None
    assert planner.replay_failures == 1


def test_every_stored_replay_is_pure_json_with_no_nan_and_is_a_dict() -> None:
    ids = _ids(3)
    decision = _planner(ids).decide(_job(), _fabric(ids))
    assert isinstance(decision, Dispatch) and decision.record is not None
    replay = decision.record.replay
    assert isinstance(replay, dict)
    text = canonical_json(replay)
    assert json.loads(text) == replay
    assert "NaN" not in text and "Infinity" not in text
    assert "\x00" not in text  # PostgreSQL jsonb refuses NUL


def test_replay_max_bytes_bounds() -> None:
    for bad in (-1, 0, 1, 1023, 2**40):
        with pytest.raises(ValueError, match="replay_max_bytes"):
            PlannerSettings(replay_max_bytes=bad)
    assert PlannerSettings(replay_max_bytes=1024).replay_max_bytes == 1024
