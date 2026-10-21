"""V0.14 G1 (D292): the replay format itself - versioned, canonical, bounded,
complete, and refused cleanly when it cannot be read."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Any

import pytest
from mf_planner_fabrics import random_case, synthetic_fabric
from mf_planner_golden import fields_text

from meshforge.common.owner_policy import OwnerPolicy
from meshforge.controller.planner import ClusterSnapshot, NodeSnapshot, plan
from meshforge.controller.planner.replay import (
    REPLAY_FIELDS,
    REPLAY_VERSION,
    ReplayError,
    bounded_replay,
    canonical_json,
    from_replay,
    idle_snapshot,
    project,
    replay_size,
    shadow_from_replay,
    to_replay,
)
from meshforge.controller.planner.types import LinkStat

pytestmark = pytest.mark.unit


def test_every_planner_input_field_is_in_replay_v1() -> None:
    """A new field on a planner input fails here until the replay stores it
    (and ``REPLAY_VERSION`` is bumped if old replays read differently)."""
    for cls, names in REPLAY_FIELDS.items():
        assert {f.name for f in dataclasses.fields(cls)} == names, cls.__name__
    request, snapshot, now = synthetic_fabric(3, split=True)
    replay = to_replay(request, snapshot, now)
    assert set(replay["request"]) == REPLAY_FIELDS[type(request)]
    assert set(replay["snapshot"]) == REPLAY_FIELDS[ClusterSnapshot]
    assert set(replay["snapshot"]["nodes"][0]) == REPLAY_FIELDS[NodeSnapshot]
    assert replay["replay_version"] == REPLAY_VERSION == 2  # V0.14 G2+G3 merge


def test_canonical_json_is_sorted_compact_and_has_no_non_finite_literal() -> None:
    for seed in range(60):
        request, snapshot, now = random_case(seed)
        text = canonical_json(to_replay(request, snapshot, now))
        assert "NaN" not in text and "Infinity" not in text
        parsed = json.loads(text)
        assert text == json.dumps(
            parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        # one canonical text per input: encoding twice gives the same bytes
        assert text == canonical_json(to_replay(request, snapshot, now))


def test_non_finite_and_huge_floats_are_tagged_and_come_back_exactly() -> None:
    request, snapshot, now = synthetic_fabric(2, split=False)
    odd = dataclasses.replace(
        snapshot.nodes[0], cpu_score=float("inf"), cpu_percent=float("nan")
    )
    big = dataclasses.replace(snapshot.nodes[1], cpu_score=3e20, cpu_percent=12)
    snap = dataclasses.replace(snapshot, nodes=(odd, big))
    replay = to_replay(request, snap, now)
    nodes = replay["snapshot"]["nodes"]
    assert nodes[0]["cpu_score"] == {"$f": "inf"}
    assert nodes[0]["cpu_percent"] == {"$f": "nan"}
    assert nodes[1]["cpu_score"] == {"$f": "3e+20"}  # jsonb would hand back an int
    assert nodes[1]["cpu_percent"] == 12  # an int stays an int
    back = from_replay(json.loads(canonical_json(replay)))[1].nodes
    assert back[0].cpu_score == float("inf")
    assert back[0].cpu_percent != back[0].cpu_percent  # NaN
    assert back[1].cpu_score == 3e20 and isinstance(back[1].cpu_score, float)
    assert back[1].cpu_percent == 12 and isinstance(back[1].cpu_percent, int)


def test_datetimes_keep_their_zone_and_microseconds() -> None:
    request, snapshot, _ = synthetic_fabric(2, split=False)
    when = dt.datetime(
        2026,
        1,
        2,
        3,
        4,
        5,
        678901,
        tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)),
    )
    link = LinkStat(bw_bytes_s=1e6, rtt_ms=1.0, samples=1, measured_at=when)
    holder = dataclasses.replace(
        snapshot.nodes[1], cached=frozenset({request.trees[0].key})
    )
    snap = dataclasses.replace(
        snapshot,
        nodes=(snapshot.nodes[0], holder),
        links={(snapshot.nodes[0].node_id, holder.node_id): link},
    )
    _, back, now = from_replay(
        json.loads(canonical_json(to_replay(request, snap, when)))
    )
    assert now == when and now.utcoffset() == when.utcoffset()
    (stored,) = back.links.values()
    assert stored.measured_at == when


def test_now_none_round_trips() -> None:
    request, snapshot, _ = synthetic_fabric(2, split=False)
    assert from_replay(to_replay(request, snapshot, None))[2] is None


def test_owner_policy_round_trips() -> None:
    request, snapshot, now = synthetic_fabric(2, split=False)
    policy = OwnerPolicy(
        accept_jobs=True,
        max_ram_bytes=2**33,
        allowed_users=("alice", "bob"),
        not_enforced=("max_cpu_percent:venv",),
        revision=3,
        pause_benchmarks=True,
    )
    snap = dataclasses.replace(
        snapshot, nodes=(dataclasses.replace(snapshot.nodes[0], policy=policy),)
    )
    back = from_replay(json.loads(canonical_json(to_replay(request, snap, now))))[1]
    assert back.nodes[0].policy == policy


def test_projection_cuts_other_jobs_holdings_and_unused_links() -> None:
    request, snapshot, _now = synthetic_fabric(4, split=False)
    key = request.trees[0].key
    junk = "sha256:" + "9" * 64
    a, b, c, d = snapshot.nodes
    nodes = (
        dataclasses.replace(
            a,
            cached=frozenset({key, junk}),
            known_bad_envs=frozenset({"x" * 64, request.env_key}),
        ),
        dataclasses.replace(b, cached=frozenset({junk}), cached_envs={"y" * 64: 5}),
        dataclasses.replace(c, cached=frozenset()),
        dataclasses.replace(d, cached=frozenset()),
    )
    snap = dataclasses.replace(
        snapshot,
        nodes=nodes,
        controller_links={
            **snapshot.controller_links,
            "node_gone": snapshot.controller_links[a.node_id],
        },
        env_builds={},
    )
    cut = project(request, snap)
    assert cut.nodes[0].cached == {key}
    assert cut.nodes[0].known_bad_envs == {request.env_key}
    assert cut.nodes[1].cached == frozenset() and cut.nodes[1].cached_envs == {}
    # only links from a node holding one of the job's trees (a) are read
    assert {dst for _src, dst in cut.links} == {a.node_id}
    assert "node_gone" not in cut.controller_links
    assert project(request, cut) == cut  # idempotent
    assert [n.node_id for n in cut.nodes] == [n.node_id for n in snap.nodes]


def test_bounded_replay_refuses_an_oversized_replay() -> None:
    request, snapshot, now = synthetic_fabric(16, split=True)
    replay, size = bounded_replay(request, snapshot, now, max_bytes=65_536)
    assert replay is not None and size == replay_size(replay) <= 65_536
    refused, size2 = bounded_replay(request, snapshot, now, max_bytes=size - 1)
    assert refused is None and size2 == size


def test_a_four_node_fabric_is_far_below_the_cap() -> None:
    request, snapshot, now = synthetic_fabric(4, split=True)
    assert replay_size(to_replay(request, snapshot, now)) < 16_384


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(replay_version=3),
        lambda r: r.pop("replay_version"),
        lambda r: r.pop("request"),
        lambda r: r["snapshot"].pop("nodes"),
        lambda r: r["request"].update(trees=[{"key": "k"}]),
        lambda r: r["snapshot"]["nodes"][0].update(cpu_score="fast"),
        lambda r: r["snapshot"]["nodes"][0].update(cpu_score={"$f": 1}),
        lambda r: r.update(now="yesterday"),
    ],
)
def test_a_malformed_replay_is_a_replay_error(mutate: Any) -> None:
    request, snapshot, now = synthetic_fabric(2, split=False)
    replay = json.loads(canonical_json(to_replay(request, snapshot, now)))
    mutate(replay)
    with pytest.raises(ReplayError):
        from_replay(replay)


def test_not_an_object_is_a_replay_error() -> None:
    with pytest.raises(ReplayError):
        from_replay([])  # type: ignore[arg-type]


def test_shadow_replay_reproduces_the_shadow_plan() -> None:
    request, snapshot, now = synthetic_fabric(3, split=False)
    busy = dataclasses.replace(snapshot.nodes[1], used_cpu_slots=2, live_attempts=2)
    snap = dataclasses.replace(
        snapshot, nodes=(snapshot.nodes[0], busy, snapshot.nodes[2])
    )
    pinned = dataclasses.replace(request, pinned_node_id=snapshot.nodes[2].node_id)
    shadow = dataclasses.replace(pinned, pinned_node_id=None, benchmark=False)
    replay = json.loads(canonical_json(to_replay(pinned, snap, now, shadow=shadow)))
    assert shadow_from_replay(replay) is not None
    s_request, s_snapshot, s_now = shadow_from_replay(replay)  # type: ignore[misc]
    expected = plan(shadow, idle_snapshot(project(pinned, snap)), "cost_model", now)
    got = plan(s_request, s_snapshot, "cost_model", s_now)
    assert fields_text(got) == fields_text(expected)
    assert all(n.used_cpu_slots == 0 for n in s_snapshot.nodes)
    no_shadow = to_replay(pinned, snap, now)
    assert shadow_from_replay(no_shadow) is None


def test_idle_snapshot_frees_every_slot_and_gpu() -> None:
    _, snapshot, _ = synthetic_fabric(2, split=False)
    busy = dataclasses.replace(
        snapshot.nodes[0],
        used_cpu_slots=1,
        used_gpu_slots=1,
        live_attempts=2,
        busy_gpus=frozenset({0}),
    )
    idle = idle_snapshot(dataclasses.replace(snapshot, nodes=(busy,)))
    (node,) = idle.nodes
    assert (node.used_cpu_slots, node.used_gpu_slots, node.live_attempts) == (0, 0, 0)
    assert node.busy_gpus == frozenset()
