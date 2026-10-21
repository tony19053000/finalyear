"""V0.14 G3 item 0 - the G1 reviewer's non-blocking items.

* **N1** the dispatcher builds a decision's replay only when the store will
  write the row (a dispatch, a failure, a ``waiting`` row whose reasons
  changed), and logs an over-cap replay once per (job, reason key);
* **N2** ``from_replay`` refuses an integer field that is not an ``int`` (a
  string, a float, a ``bool``) and a boolean field that is not a ``bool``;
* **N4** ``to_replay`` refuses a naive ``now``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from types import SimpleNamespace
from typing import Any

import pytest
from mf_planner_fabrics import synthetic_fabric
from structlog.testing import capture_logs

from meshforge.common.ids import new_job_id
from meshforge.common.owner_policy import OPEN_POLICY
from meshforge.common.states import JobState
from meshforge.controller.jobs import planning as planning_module
from meshforge.controller.jobs.execution_store import Dispatch, Wait
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.queue_reads import FabricRows, NodeRow
from meshforge.controller.jobs.store import JobRecord, JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner.replay import (
    ReplayError,
    canonical_json,
    from_replay,
    to_replay,
)
from meshforge.controller.settings import JobApiSettings, PlannerSettings

pytestmark = pytest.mark.unit

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


def _planner(connected: list[str], max_bytes: int = 65_536) -> DispatchPlanner:
    return DispatchPlanner(
        manager=_Manager(connected),  # type: ignore[arg-type]
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
        scheduling=JobScheduling(input_bytes=10**6),
    )


def _fabric(ids: list[str]) -> FabricRows:
    return FabricRows(
        nodes={
            n: NodeRow(
                node_id=n,
                state="online",
                hardware={"capabilities": ["exec", "exec-subprocess"]},
                hardware_scores=None,
                cpu_slots=2,
                gpu_slots=0,
            )
            for n in ids
        }
    )


class _Counting:
    """Counts the dispatcher's replay builds (wraps the real codec)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        real = planning_module.bounded_replay

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(planning_module, "bounded_replay", counted)


# ---------------------------------------------------------------- N1


def test_a_waiting_job_with_unchanged_reasons_builds_no_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _ids(3)
    counting = _Counting(monkeypatch)
    planner = _planner(connected=[])  # no node has a session: the job waits
    job = _job()
    fabric = _fabric(ids)
    first = planner.decide(job, fabric)
    assert isinstance(first, Wait) and first.record is not None
    assert first.record.replay is not None and counting.calls == 1
    # the store wrote it: the job's latest reason is now this one
    fabric.last_reason[job.id] = first.record.reason_key
    for _ in range(5):  # every later tick with the same reasons
        again = planner.decide(job, fabric)
        assert isinstance(again, Wait) and again.record is not None
        assert again.record.replay is None and again.record.replay_version is None
        assert again.record.reason_key == first.record.reason_key
    assert counting.calls == 1


def test_a_waiting_job_whose_reasons_changed_gets_its_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _ids(3)
    counting = _Counting(monkeypatch)
    job = _job()
    fabric = _fabric(ids)
    fabric.last_reason[job.id] = "0" * 64  # an older, different reason
    decision = _planner(connected=[]).decide(job, fabric)
    assert isinstance(decision, Wait) and decision.record is not None
    assert decision.record.replay is not None and counting.calls == 1


def test_a_dispatch_always_gets_its_replay_even_with_an_equal_reason_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = _ids(2)
    counting = _Counting(monkeypatch)
    job = _job()
    probe = _planner(connected=ids).decide(job, _fabric(ids))
    assert isinstance(probe, Dispatch) and probe.record is not None
    fabric = _fabric(ids)
    fabric.last_reason[job.id] = probe.record.reason_key
    decision = _planner(connected=ids).decide(job, fabric)
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert decision.record.replay is not None
    assert counting.calls == 2


def test_an_over_cap_replay_is_logged_once_per_job_and_reason() -> None:
    ids = _ids(400)  # far over the smallest cap
    planner = _planner(connected=ids, max_bytes=1024)
    job, other = _job(), _job()
    with capture_logs() as logs:
        for _ in range(3):
            planner.decide(job, _fabric(ids))
        planner.decide(other, _fabric(ids))
    events = [e for e in logs if e["event"] == "controller.jobs.replay_over_cap"]
    assert [e["job_id"] for e in events] == [job.id, other.id]
    assert planner.replays_over_cap == 4  # counted every time, logged once


# ---------------------------------------------------------------- N2


def _stored() -> dict[str, Any]:
    request, snapshot, now = synthetic_fabric(3, split=True)
    replay: dict[str, Any] = json.loads(
        canonical_json(to_replay(request, snapshot, now))
    )
    return replay


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        (("request", "attempt_no"), "1"),
        (("request", "attempt_no"), True),
        (("request", "attempt_no"), 1.0),
        (("request", "vram_bytes"), "8"),
        (("request", "vram_bytes"), False),
        (("request", "ram_bytes"), 2.5),
        (("request", "split", "items"), True),
        (("request", "split", "item_bytes"), "100"),
        (("request", "split", "chunk_items"), 3.0),
        (("request", "split", "max_nodes"), True),
        (("request", "split", "min_nodes"), "2"),
        (("request", "history", "output_samples"), True),
        (("request", "seeded"), 1),
        (("request", "seeded"), "true"),
        (("request", "network"), 0),
        (("request", "benchmark"), None),
        (("snapshot", "nodes", 0, "cpu_slots"), True),
        (("snapshot", "nodes", 0, "cpu_slots"), "2"),
        (("snapshot", "nodes", 0, "ram_bytes"), 1.5),
        (("snapshot", "nodes", 0, "cpu_threads"), False),
        (("snapshot", "nodes", 0, "disk_free_bytes"), "9"),
        (("snapshot", "nodes", 0, "ram_free_bytes"), True),
        (("snapshot", "nodes", 0, "live_attempts"), 0.0),
        (("snapshot", "nodes", 0, "gpu_vram_bytes"), [True]),
        (("snapshot", "nodes", 0, "busy_gpus"), ["0"]),
        (("snapshot", "nodes", 0, "connected"), 1),
        (("snapshot", "nodes", 0, "owner_active"), "no"),
        (("snapshot", "nodes", 0, "cpu_score"), True),
        (("snapshot", "config", "max_attempts_per_node"), 8.0),
        (("snapshot", "config", "split", "max_chunks"), True),
    ],
    ids=lambda v: repr(v),
)
def test_an_int_or_bool_field_of_the_wrong_type_is_a_replay_error(
    path: tuple[Any, ...], bad: Any
) -> None:
    replay = _stored()
    target: Any = replay
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = bad
    with pytest.raises(ReplayError):
        from_replay(replay)


def test_integer_fields_still_accept_none_and_ints() -> None:
    replay = _stored()
    replay["request"]["vram_bytes"] = None
    replay["request"]["ram_bytes"] = 3 * 1024**3
    replay["snapshot"]["nodes"][0]["ram_bytes"] = None
    request, snapshot, _ = from_replay(replay)
    assert request.vram_bytes is None and request.ram_bytes == 3 * 1024**3
    assert snapshot.nodes[0].ram_bytes is None


def test_a_policy_with_a_bool_for_an_int_is_refused() -> None:
    request, snapshot, now = synthetic_fabric(2, split=False)
    node = snapshot.nodes[0]
    snap = dataclasses.replace(
        snapshot,
        nodes=(dataclasses.replace(node, policy=OPEN_POLICY), *snapshot.nodes[1:]),
    )
    replay = json.loads(canonical_json(to_replay(request, snap, now)))
    replay["snapshot"]["nodes"][0]["policy"]["revision"] = True
    with pytest.raises(ReplayError):
        from_replay(replay)


# ---------------------------------------------------------------- N4


def test_a_naive_now_is_never_written() -> None:
    request, snapshot, _ = synthetic_fabric(2, split=False)
    with pytest.raises(ReplayError):
        to_replay(request, snapshot, dt.datetime(2026, 10, 21, 12, 0))  # noqa: DTZ001
    assert isinstance(ReplayError("x"), ValueError)  # callers catching ValueError


def test_an_aware_now_in_any_zone_and_no_now_are_written() -> None:
    request, snapshot, _ = synthetic_fabric(2, split=False)
    zone = dt.timezone(dt.timedelta(hours=5, minutes=30))
    for now in (None, dt.datetime(2026, 10, 21, 12, 0, tzinfo=zone)):
        replay = to_replay(request, snapshot, now)
        assert from_replay(json.loads(canonical_json(replay)))[2] == now


def test_the_dispatcher_counts_a_naive_wall_clock_as_a_replay_failure() -> None:
    ids = _ids(2)
    planner = DispatchPlanner(
        manager=_Manager(ids),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        planner=PlannerSettings(),
        clock=lambda: 1000.0,
        wall_clock=lambda: WALL.replace(tzinfo=None),
        started_at=0.0,
    )
    decision = planner.decide(_job(), _fabric(ids))
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert decision.record.replay is None and planner.replay_failures == 1
