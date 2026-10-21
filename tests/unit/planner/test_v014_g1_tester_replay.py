"""V0.14 G1 tester r1 - replay completeness, round trip, float boxing, codec
errors. Derived from the brief's criteria (0.14.1, D292), not from the
implementation: the mutation sweep below perturbs *every field* of every
planner input and asks whether the replay still plans the same as the
perturbed original - so a field ``project()``/``to_replay()`` dropped while
``plan()`` reads it is found without knowing which fields those are.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from mf_planner_fabrics import random_case, synthetic_fabric
from mf_planner_golden import decision_view, fields_text

from meshforge.common.manifest import POLICIES
from meshforge.controller.planner import (
    ClusterSnapshot,
    NodeSnapshot,
    PlanRequest,
    plan,
)
from meshforge.controller.planner.candidates import (
    AcceleratorRequest,
    RequestKind,
    candidates,
)
from meshforge.controller.planner.replay import (
    REPLAY_VERSION,
    ReplayError,
    bounded_replay,
    canonical_json,
    from_replay,
    project,
    replay_size,
    to_replay,
)
from meshforge.controller.planner.types import (
    ControllerLinkStat,
    History,
    LinkStat,
    RejectCode,
    TreeNeed,
)

pytestmark = pytest.mark.unit

STR_FIELDS = {
    "pinned_node_id",
    "submitter_node_id",
    "fingerprint",
    "owner_name",
    "round_robin_after",
    "items_tree",
    "objective",
}


def _roundtrip(
    request: PlanRequest, snapshot: ClusterSnapshot, now: dt.datetime | None
) -> tuple[PlanRequest, ClusterSnapshot, dt.datetime | None]:
    return from_replay(json.loads(canonical_json(to_replay(request, snapshot, now))))


def _sans_digest(decision: Any) -> dict[str, Any]:
    view = decision_view(decision)
    del view["snapshot_digest"], view["fields_sha256"]
    return view


# ---------------------------------------------------------------- mutation


def _mutate_value(name: str, value: Any, annotation: str) -> Iterator[Any]:
    """A few different values for one field (empty when none makes sense)."""
    if isinstance(value, bool):
        yield not value
    elif isinstance(value, int):
        yield value * 2 + 1
        yield 0
    elif isinstance(value, float):
        if math.isfinite(value):
            yield value * 3 + 1.5
            yield 0.0
    elif value is None:
        if name in STR_FIELDS:
            yield "zz_mutated"
        elif "float" in annotation:
            yield 3.5
            yield 1e9
        elif "int" in annotation:
            yield 777
            yield 3 * 10**9
    elif isinstance(value, str):
        yield value + "x"
    elif isinstance(value, frozenset):
        sample = next(iter(value), None)
        yield value | {99 if "int" in annotation or isinstance(sample, int) else "zz"}
        if value:
            yield frozenset()
    elif isinstance(value, tuple):
        if value and isinstance(value[0], TreeNeed):
            yield (
                dataclasses.replace(value[0], size=value[0].size * 7 + 1),
                *value[1:],
            )
        elif value and all(isinstance(v, float) for v in value):
            yield tuple(v * 2 + 0.25 for v in value)
        elif value and all(isinstance(v, int) for v in value):
            yield (*value, 1)
            yield tuple(v * 2 + 1 for v in value)
        elif name == "node_set":
            yield (*value, "zz")
        elif not value and "int" in annotation:
            yield (8 * 1024**3,)
    elif isinstance(value, Mapping):
        for key in list(value)[:1]:
            for new in _mutate_value(name, value[key], "float"):
                yield {**value, key: new}
        for key in list(value)[:1]:
            if dataclasses.is_dataclass(value[key]):
                for sub in _mutations(value[key]):
                    yield {**value, key: sub}
        if not value:
            yield from ()
    elif dataclasses.is_dataclass(value):
        yield from _mutations(value)


def _mutations(obj: Any) -> Iterator[Any]:
    for fld in dataclasses.fields(obj):
        value = getattr(obj, fld.name)
        for new in _mutate_value(fld.name, value, str(fld.type)):
            try:
                yield dataclasses.replace(obj, **{fld.name: new})
            except (TypeError, ValueError):  # the type refuses it: skip
                continue


def _cases() -> list[int]:
    return list(range(0, 400, 7))


@pytest.mark.parametrize("block", range(4))
def test_no_field_plan_reads_is_lost_by_project_or_the_replay(block: int) -> None:
    """For every single-field perturbation of the request, its history/split,
    the snapshot, its config, the first nodes (+ their owner policy), links,
    controller links and env builds: planning the replay equals planning the
    perturbed original, for two policies - field for field."""
    checked = sensitive = 0
    for seed in _cases()[block::4]:
        request, snapshot, now = random_case(seed)
        policies = (POLICIES[seed % len(POLICIES)], "cost_model")
        variants: list[tuple[PlanRequest, ClusterSnapshot, dt.datetime | None]] = []
        variants += [(r, snapshot, now) for r in _mutations(request)]
        variants += [(request, s, now) for s in _mutations(snapshot)]
        for i, node in enumerate(snapshot.nodes[:3]):
            for n in _mutations(node):
                nodes = (*snapshot.nodes[:i], n, *snapshot.nodes[i + 1 :])
                variants.append(
                    (request, dataclasses.replace(snapshot, nodes=nodes), now)
                )
        if now is not None:
            variants.append((request, snapshot, now + dt.timedelta(hours=5)))
            variants.append((request, snapshot, None))
        base = {p: _sans_digest(plan(request, snapshot, p, now)) for p in policies}
        for req, snap, at in variants:
            try:
                back = _roundtrip(req, snap, at)
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"seed {seed}: replay of a mutated input raised {exc!r}")
            for policy in policies:
                try:
                    direct = plan(req, snap, policy, at)
                except Exception:  # noqa: BLE001, S112 - mutant not plannable
                    continue
                again = plan(*back[:2], policy, back[2])
                full = _sans_digest(direct)
                # the projection must not change the decision ...
                assert (
                    _sans_digest(plan(req, project(req, snap), policy, at)) == full
                ), (
                    seed,
                    policy,
                )
                # ... and the replay equals the projected planning, exactly
                projected = plan(req, project(req, snap), policy, at)
                assert fields_text(again) == fields_text(projected), (seed, policy)
                checked += 1
                sensitive += full != base[policy]
    assert checked > 150, checked
    assert sensitive > 20, f"sweep is vacuous: only {sensitive} mutations mattered"


def test_every_node_field_mutation_of_a_synthetic_fabric_replays() -> None:
    """The same sweep on the synthetic latency fabrics (whole + split)."""
    checked = 0
    for split in (False, True):
        request, snapshot, now = synthetic_fabric(4, split=split)
        for i in range(len(snapshot.nodes)):
            for n in _mutations(snapshot.nodes[i]):
                nodes = (*snapshot.nodes[:i], n, *snapshot.nodes[i + 1 :])
                snap = dataclasses.replace(snapshot, nodes=nodes)
                back = _roundtrip(request, snap, now)
                for policy in POLICIES:
                    want = plan(request, project(request, snap), policy, now)
                    assert fields_text(plan(*back[:2], policy, back[2])) == fields_text(
                        want
                    )
                    checked += 1
    assert checked > 200


def test_links_and_cache_holdings_that_matter_survive_the_projection() -> None:
    """Fabric with a holder: every link (candidate, holder), stale
    ones included, is replayed; changing its bandwidth changes the decision
    and the replay."""
    request, snapshot, now = synthetic_fabric(4, split=False)
    holder = next(n.node_id for n in snapshot.nodes if n.cached)
    dst = next(n.node_id for n in snapshot.nodes if n.node_id != holder)
    key = (dst, holder)  # the estimator's key: (fetching node, holder)
    link = snapshot.links[key]
    slow = dataclasses.replace(link, bw_bytes_s=link.bw_bytes_s / 1000)
    stale = dataclasses.replace(link, measured_at=now - dt.timedelta(days=30))  # type: ignore[operator]
    base = plan(request, snapshot, "cost_model", now)
    texts = set()
    for variant in (link, slow, stale):
        snap = dataclasses.replace(snapshot, links={**snapshot.links, key: variant})
        back = _roundtrip(request, snap, now)
        assert back[1].links[key] == variant
        texts.add(canonical_json(to_replay(request, snap, now)))
        assert fields_text(plan(*back[:2], "cost_model", back[2])) == fields_text(
            plan(request, project(request, snap), "cost_model", now)
        )
    assert len(texts) == 3
    assert base.chosen is not None


# ---------------------------------------------------------------- policies


@pytest.mark.parametrize("policy", POLICIES)
def test_round_trip_for_every_policy_over_many_seeds(policy: str) -> None:
    chose = 0
    for seed in range(1000, 1120):
        request, snapshot, now = random_case(seed)
        want = plan(request, project(request, snapshot), policy, now)
        back = _roundtrip(request, snapshot, now)
        got = plan(*back[:2], policy, back[2])
        assert fields_text(got) == fields_text(want), (policy, seed)
        chose += want.chosen is not None
    assert chose > 10


def test_round_robin_cursor_is_part_of_the_replay() -> None:
    request, snapshot, now = synthetic_fabric(5, split=False)
    ids = [n.node_id for n in snapshot.nodes]
    chosen: dict[str | None, str] = {}
    for cursor in [None, *ids]:
        snap = dataclasses.replace(snapshot, round_robin_after=cursor)
        want = plan(request, project(request, snap), "round_robin", now)
        back = _roundtrip(request, snap, now)
        assert back[1].round_robin_after == cursor
        got = plan(*back[:2], "round_robin", back[2])
        assert fields_text(got) == fields_text(want)
        assert got.chosen is not None
        chosen[cursor] = got.chosen.node_id
    # the cursor really moves the choice (else this test proves nothing)
    assert len(set(chosen.values())) >= 3, chosen


def test_random_seed_inputs_are_part_of_the_replay() -> None:
    """``random`` is seeded by job_id + attempt_no: both survive the replay,
    and the replay reproduces the choice for many (job, attempt) pairs."""
    request, snapshot, now = synthetic_fabric(6, split=False)
    seen = set()
    for n in range(40):
        req = dataclasses.replace(request, job_id=f"job_{n:026d}", attempt_no=1 + n % 5)
        want = plan(req, project(req, snapshot), "random", now)
        back = _roundtrip(req, snapshot, now)
        assert back[0].job_id == req.job_id and back[0].attempt_no == req.attempt_no
        got = plan(*back[:2], "random", back[2])
        assert fields_text(got) == fields_text(want)
        assert got.chosen is not None
        seen.add(got.chosen.node_id)
    assert len(seen) >= 3  # the seed matters


# ---------------------------------------------------------------- floats


def _dump(obj: Any) -> str:
    """Types kept: an int and a float of equal value differ."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return (
            type(obj).__name__
            + "("
            + ",".join(
                f"{f.name}={_dump(getattr(obj, f.name))}"
                for f in dataclasses.fields(obj)
            )
            + ")"
        )
    if isinstance(obj, Mapping):
        return (
            "{"
            + ",".join(
                f"{_dump(k)}:{_dump(v)}" for k, v in sorted(obj.items(), key=repr)
            )
            + "}"
        )
    if isinstance(obj, frozenset | set):
        return "{" + ",".join(sorted(_dump(v) for v in obj)) + "}"
    if isinstance(obj, tuple | list):
        return "[" + ",".join(_dump(v) for v in obj) + "]"
    return f"{type(obj).__name__}:{obj!r}"


FLOATS = [
    0.0,
    -0.0,
    1.0,
    5.0,
    0.1,
    1e-300,
    5e-324,
    123456789.125,
    999_999_999_999_999.0,
    1e15,
    1e15 + 2,
    -1e15,
    1e16,
    2.0**53,
    2.0**63,
    1e300,
    1.7976931348623157e308,
    -1e300,
    math.inf,
    -math.inf,
    math.nan,
]
INTS = [0, 1, -1, 5, 2**53 + 1, 10**15, 10**18, 2**62]


def _node_with(**kw: Any) -> NodeSnapshot:
    return NodeSnapshot(
        node_id="n1",
        state="online",
        connected=True,
        capabilities=frozenset({"exec"}),
        **kw,
    )


@pytest.mark.parametrize("value", FLOATS, ids=repr)
def test_floats_keep_their_type_and_value_through_jsonb_like_json(value: float) -> None:
    request, snapshot, now = synthetic_fabric(2, split=False)
    node = _node_with(cpu_score=value, cpu_percent=value, gpu_score=value)
    snap = dataclasses.replace(snapshot, nodes=(node, *snapshot.nodes[1:]))
    text = canonical_json(to_replay(request, snap, now))
    assert "NaN" not in text and "Infinity" not in text
    got = from_replay(json.loads(text))[1].nodes[0]
    assert type(got.cpu_score) is float
    assert _dump(got.cpu_score) == _dump(value)
    # ... and the history's medians and the link table, the other float homes
    hist = History(by_node_s={"n1": value}, output_bytes=value, chunk_overhead_s=value)
    req = dataclasses.replace(request, history=hist)
    back = from_replay(json.loads(canonical_json(to_replay(req, snap, now))))[0]
    assert _dump(back.history) == _dump(hist)


@pytest.mark.parametrize("value", INTS, ids=repr)
def test_ints_stay_ints(value: int) -> None:
    request, snapshot, now = synthetic_fabric(2, split=False)
    node = _node_with(ram_bytes=value, disk_free_bytes=value, cpu_threads=value)
    snap = dataclasses.replace(snapshot, nodes=(node, *snapshot.nodes[1:]))
    got = _roundtrip(request, snap, now)[1].nodes[0]
    assert _dump((got.ram_bytes, got.disk_free_bytes, got.cpu_threads)) == _dump(
        (value, value, value)
    )


def test_a_float_that_looks_like_an_int_is_not_turned_into_one() -> None:
    """jsonb prints 1e15 as 1000000000000000: a plain JSON round trip would
    return an int, and an int/float flip changes digest text."""
    request, snapshot, now = synthetic_fabric(2, split=False)
    link = LinkStat(bw_bytes_s=1e15, rtt_ms=2.0, samples=3, measured_at=now)  # type: ignore[arg-type]
    clink = ControllerLinkStat(bw_bytes_s=1e16, samples=1)
    snap = dataclasses.replace(
        snapshot,
        links={("node_001", "node_000"): link},
        controller_links={"node_000": clink},
    )
    back = _roundtrip(request, snap, now)[1]
    assert type(back.links[("node_001", "node_000")].bw_bytes_s) is float
    assert type(back.controller_links["node_000"].bw_bytes_s) is float


@pytest.mark.parametrize(
    "bad",
    [{"$f": "abc"}, {"$f": 5}, {"$f": "1", "x": 1}, {}, [], "5", True and "x"],
    ids=repr,
)
def test_a_malformed_float_box_is_a_replay_error_not_a_crash(bad: Any) -> None:
    request, snapshot, now = synthetic_fabric(2, split=False)
    replay = json.loads(canonical_json(to_replay(request, snapshot, now)))
    replay["snapshot"]["nodes"][0]["cpu_score"] = bad
    with pytest.raises(ReplayError):
        from_replay(replay)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda r: r.pop("snapshot"),
        lambda r: r.pop("request"),
        lambda r: r.update(now="not-a-date"),
        lambda r: r.update(replay_version=2),
        lambda r: r.update(replay_version="1"),
        lambda r: r.update(replay_version=None),
        lambda r: r["snapshot"].pop("nodes"),
        lambda r: r["snapshot"].update(nodes=[1, 2]),
        lambda r: r["snapshot"]["nodes"][0].pop("node_id"),
        lambda r: r["request"].update(trees=[{"nokey": 1}]),
        lambda r: r["snapshot"].update(links={"bad": {}}),
    ],
)
def test_corrupt_stored_replays_raise_replay_error_only(mangle: Any) -> None:
    request, snapshot, now = synthetic_fabric(3, split=True)
    replay = json.loads(canonical_json(to_replay(request, snapshot, now)))
    mangle(replay)
    with pytest.raises(ReplayError):
        from_replay(replay)


def test_non_object_replays_are_refused() -> None:
    for junk in (None, [], "x", 5):
        with pytest.raises(ReplayError):
            from_replay(junk)  # type: ignore[arg-type]


# ---------------------------------------------------------------- size cap


def test_cap_boundary_is_exact_and_never_raises() -> None:
    request, snapshot, now = synthetic_fabric(4, split=True)
    size = replay_size(to_replay(request, snapshot, now))
    replay, got = bounded_replay(request, snapshot, now, max_bytes=size)
    assert replay is not None and got == size
    over, got2 = bounded_replay(request, snapshot, now, max_bytes=size - 1)
    assert over is None and got2 == size
    none, _ = bounded_replay(request, snapshot, now, max_bytes=0)
    assert none is None


def test_size_is_utf8_bytes_not_characters() -> None:
    request, snapshot, now = synthetic_fabric(2, split=False)
    req = dataclasses.replace(request, owner_name="é中\U0001f600" * 20)
    text = canonical_json(to_replay(req, snapshot, now))
    assert replay_size(to_replay(req, snapshot, now)) == len(text.encode()) > len(text)
    back = from_replay(json.loads(text))[0]
    assert back.owner_name == req.owner_name


def test_a_huge_fabric_is_over_the_default_cap_not_an_error() -> None:
    request, snapshot, now = synthetic_fabric(150, split=True)
    replay, size = bounded_replay(request, snapshot, now, max_bytes=65_536)
    assert replay is None and size > 65_536


# ---------------------------------------------------------------- candidates


def test_accelerator_request_is_not_supported_in_build() -> None:
    fields = {f.name for f in dataclasses.fields(AcceleratorRequest)}
    assert fields  # typed, constructible
    _, snapshot, _ = synthetic_fabric(2, split=False)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(AcceleratorRequest):
        if (
            f.default is dataclasses.MISSING
            and f.default_factory is dataclasses.MISSING
        ):
            kwargs[f.name] = 1 if "int" in str(f.type) else "x"
    req = AcceleratorRequest(**kwargs)
    got = candidates(req, snapshot)
    assert got.kind is RequestKind.ACCELERATOR
    assert got.candidates == ()
    assert got.refused is not None
    assert got.refused.code is RejectCode.NOT_SUPPORTED_IN_BUILD
    assert RejectCode.NOT_SUPPORTED_IN_BUILD.value == "not_supported_in_build"


def test_replay_version_constant() -> None:
    assert REPLAY_VERSION == 1
