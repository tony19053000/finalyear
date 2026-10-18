"""V0.9 G4 lane B tester round 1: ``cache:cold`` eviction edge cases on a real CAS."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from meshforge.agent.conditions.cache import CacheController
from meshforge.agent.conditions.runner import ConditionRunner
from meshforge.agent.settings import ConditionSettings
from meshforge.common.bench_conditions import ConditionKind
from meshforge.common.cas import CasStore
from meshforge.common.gc_index import GcIndex
from meshforge.common.hashing import Digest
from meshforge.common.tree import build_tree

pytestmark = pytest.mark.unit


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[CasStore, GcIndex, str, list[str], str]]:
    """A CAS holding tree A and an unrelated tree B (both with GC index rows)."""
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    index = GcIndex(tmp_path / "gc")
    index.open()
    ids: list[str] = []
    members: list[list[str]] = []
    for name in ("a", "b"):
        root = tmp_path / f"proj_{name}"
        root.mkdir()
        for i in range(3):
            (root / f"f{i}.txt").write_text(f"{name} content {i}\n" * 10)
        built = build_tree(root)
        store.put_bytes(built.manifest, built.tree.tree_id)
        keys = sorted({e.sha256 for e in built.tree.files()})
        for entry in built.tree.files():
            store.put_bytes((root / entry.path).read_bytes())
        for key in [built.tree.tree_id.hex, *keys]:
            index.upsert("blob", key, 10)
        ids.append(str(built.tree.tree_id))
        members.append(keys)
    yield store, index, ids[0], members[0], ids[1]
    index.close()


def _has(store: CasStore, key: str) -> bool:
    return store.has(Digest.parse(f"sha256:{key}"))


@pytest.mark.asyncio
async def test_cold_touches_only_the_attempts_own_tree(world) -> None:  # type: ignore[no-untyped-def]
    store, index, tree_a, members_a, tree_b = world
    ctl = CacheController("cold", store=store, index=index, tree_ids=[tree_a])
    await ctl.apply()
    assert not any(_has(store, k) for k in members_a)
    assert store.has(Digest.parse(tree_b))  # the neighbour tree is untouched


@pytest.mark.asyncio
async def test_cold_keeps_a_blob_leased_by_a_running_attempt(world) -> None:  # type: ignore[no-untyped-def]
    store, index, tree_a, members_a, _ = world
    index.lease("running_attempt", [("blob", k) for k in members_a])
    ctl = CacheController("cold", store=store, index=index, tree_ids=[tree_a])
    await ctl.apply()
    assert all(_has(store, k) for k in members_a)  # leased: never evicted
    assert ctl.measured["kept_blobs"] == len(members_a)

    # and the controller says so rather than claiming cold
    class S:
        blobs_hit, bytes_hit, blobs_fetched, bytes_fetched, manifest_hit = (
            3,
            30,
            0,
            0,
            False,
        )

    from meshforge.agent.conditions.base import ConditionFailedError

    with pytest.raises(ConditionFailedError):
        ctl.after_stage([S()])


@pytest.mark.asyncio
async def test_cold_after_the_lease_is_released_evicts(world) -> None:  # type: ignore[no-untyped-def]
    store, index, tree_a, members_a, _ = world
    index.lease("att", [("blob", k) for k in members_a])
    index.release("att") if hasattr(index, "release") else index.release_lease("att")
    ctl = CacheController("cold", store=store, index=index, tree_ids=[tree_a])
    await ctl.apply()
    assert not any(_has(store, k) for k in members_a)


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [[], [""], ["", ""]])
async def test_cold_with_no_tree_evicts_nothing(world, ids: list[str]) -> None:  # type: ignore[no-untyped-def]
    store, index, _tree_a, members_a, _ = world
    ctl = CacheController("cold", store=store, index=index, tree_ids=ids)
    await ctl.apply()
    assert ctl.measured["evicted_blobs"] == 0
    assert all(_has(store, k) for k in members_a)


@pytest.mark.asyncio
async def test_cold_is_idempotent_and_survives_a_missing_index(world) -> None:  # type: ignore[no-untyped-def]
    store, _index, tree_a, members_a, _ = world
    first = CacheController("cold", store=store, index=None, tree_ids=[tree_a])
    await first.apply()
    again = CacheController("cold", store=store, index=None, tree_ids=[tree_a])
    await again.apply()
    assert again.measured["evicted_blobs"] == 0
    assert not any(_has(store, k) for k in members_a)


@pytest.mark.asyncio
async def test_cold_with_a_corrupt_manifest_still_drops_the_manifest(
    world, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    store, index, tree_a, _members, _ = world
    path = store.path_for(Digest.parse(tree_a)) if hasattr(store, "path_for") else None
    if path is None:
        pytest.skip("no path_for on this CasStore")
    path.chmod(0o644)
    path.write_bytes(b"\x00garbage")
    ctl = CacheController("cold", store=store, index=index, tree_ids=[tree_a])
    await ctl.apply()  # must not raise
    assert ctl.measured["mode"] == "cold"


@pytest.mark.xfail(
    strict=True,
    raises=ValueError,
    reason="DEFECT TR1-B4 (minor): CacheController.__init__ calls Digest.parse on "
    "the controller-sent tree ids, so ConditionRunner.prepare raises ValueError "
    "although it documents 'never raises' (controller ids are valid in practice)",
)
@pytest.mark.parametrize("bad", ["garbage", "sha256:zz", "sha256:" + "0" * 63, "\x00"])
def test_runner_prepare_with_a_malformed_tree_id_never_raises(
    tmp_path: Path, bad: str
) -> None:
    """prepare() promises never to raise: a refusal is a failed ActiveCondition."""
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 20)
    store.prepare()
    runner = ConditionRunner(
        ConditionSettings(),
        data_dir=tmp_path,
        kinds={ConditionKind.CACHE},
        store=store,
    )
    active = runner.prepare("att_1", "cache:cold", timeout_s=60, tree_ids=[bad])
    assert active is not None
