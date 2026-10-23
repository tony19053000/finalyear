# ruff: noqa: S311, RUF059, ASYNC240
"""V0.15 G3 tester r1 - the registry, the retrain task and the GC on a real
PostgreSQL + a real CAS (D339, D358-D361).

Adversarial cases beyond the implementer's: randomised concurrent
activate / pin / unpin storms (at most one active and one pinned, no
exception but a typed refusal, no deadlock); pin semantics matrix; a
rejected set is re-judged when the gate's settings change; two controllers
race for the retrain (DB single-flight); a cancelled retrain releases the
cross-controller lock; a real ``fit`` child SIGKILLed mid-run; a child that
exits 0 with damaged / forged output; GC keeps what is served.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import signal
import sys
import tempfile
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

from alembic import command
from mf_ml_synthetic import synthetic_rows
from sqlalchemy import text

from meshforge.common.cas import CasStore
from meshforge.common.errors import FailureType
from meshforge.controller.ml import export
from meshforge.controller.ml.artefact import digest
from meshforge.controller.ml.dataset import Dataset, parquet_bytes, sort_key
from meshforge.controller.ml.registry import (
    MlRegistryError,
    ModelRegistry,
    PinError,
    protected_artefacts,
)
from meshforge.controller.ml.source import ModelSource
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set
from meshforge.controller.ml.train.fit_main import set_document
from meshforge.controller.ml.train.gate import Reference, run_gate
from meshforge.controller.ml_retrain_task import MlRetrainer, run_child
from meshforge.controller.settings import PlannerSettings
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(900)]

FAST = CompareSettings(terms=("compute",), algorithms=("ridge",), min_rows=100)
INPUTS = {"watermark": {"x": 1}, "train": {}}


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=10)
    try:
        async with eng.begin() as conn:  # a clean registry per test
            await conn.execute(text("DELETE FROM ml_model_set"))
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def cas(tmp_path: Path) -> CasStore:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    return store


def _rows(seed: int, kind: str = "signal") -> list[dict[str, Any]]:
    rows = synthetic_rows(
        "signal", families=6, groups_per_family=5, runs_per_group=10, seed=seed
    )
    if kind == "noise":
        rng = random.Random(seed)
        for row in rows:
            row["y"] = rng.gauss(0.0, 0.3)
            row["actual_s"] = row["raw_s"] * math.exp(row["y"])
    return rows


def _trained(
    seed: int,
    kind: str = "signal",
    reference: Reference | None = None,
    settings: CompareSettings = FAST,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    table = table_from_rows(_rows(seed, kind))
    result = train_set(table, settings)
    gate = run_gate(table, settings, result, reference or Reference())
    doc = set_document(result, gate, settings)
    return doc, {m.version: m.artefact for m in result.members if m.artefact}


async def _statuses(engine: Any) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT set_version, status FROM ml_model_set"))
        ).all()
    return {str(v): str(s) for v, s in rows}


def _built(rows: list[dict[str, Any]]) -> export.BuiltDataset:
    ordered = sorted(rows, key=sort_key)
    data = parquet_bytes(ordered)
    dataset = Dataset(
        rows=tuple(ordered),
        counts={"rows": len(ordered)},
        parquet=data,
        digest="sha256:" + hashlib.sha256(data).hexdigest(),
    )
    return export.BuiltDataset(dataset=dataset, max_runs=1000, truncated=False)


def _planner(**over: Any) -> PlannerSettings:
    return PlannerSettings(ml_min_rows=100, **over)


# ----------------------------------------------------------------- invariants


async def test_random_concurrent_storm_keeps_one_active_one_pinned(
    engine: Any, cas: CasStore
) -> None:
    registry = ModelRegistry(engine, cas)
    docs = [_trained(seed) for seed in range(100, 106)]
    versions = [d["set_version"] for d, _ in docs]
    first, arts = docs[0]
    await registry.store(first, arts, reference_version=None, inputs=INPUTS)
    rng = random.Random(7)
    known: set[str] = {first["set_version"]}

    async def one(op: str) -> object:
        doc, arts_ = docs[rng.randrange(len(docs))]
        try:
            if op == "store":
                serving = await registry.serving_version()
                return await registry.store(
                    doc, arts_, reference_version=serving[1], inputs=INPUTS
                )
            if op == "pin":
                return await registry.pin(rng.choice(versions))
            return await registry.unpin(rng.choice(versions))
        except PinError as exc:
            return exc

    for _round in range(25):
        ops = [rng.choice(("store", "pin", "pin", "unpin")) for _ in range(8)]
        out = await asyncio.wait_for(
            asyncio.gather(*(one(o) for o in ops), return_exceptions=True), 120
        )
        for item in out:
            assert not isinstance(item, BaseException) or isinstance(item, PinError), (
                repr(item)
            )
            assert not isinstance(item, MlRegistryError), repr(item)
        statuses = list((await _statuses(engine)).values())
        assert statuses.count("active") <= 1, statuses
        assert statuses.count("pinned") <= 1, statuses
        known |= set(await _statuses(engine))
        kind, version = await registry.serving_version()
        if kind == "pinned":
            assert statuses.count("pinned") == 1
    assert known  # something was stored


async def test_pin_semantics_matrix(engine: Any, cas: CasStore) -> None:
    registry = ModelRegistry(engine, cas)
    a, arts_a = _trained(201)
    b, arts_b = _trained(202)
    await registry.store(a, arts_a, reference_version=None, inputs=INPUTS)
    await registry.store(b, arts_b, reference_version=a["set_version"], inputs=INPUTS)
    A, B = a["set_version"], b["set_version"]
    assert await _statuses(engine) == {A: "superseded", B: "active"}
    # pin the active set: serving is pinned, no active set remains
    assert (await registry.pin(B)).status == "pinned"
    assert await registry.serving_version() == ("pinned", B)
    # pinning again is idempotent
    assert (await registry.pin(B)).status == "pinned"
    # pin another: the first pin is released
    assert (await registry.pin(A)).status == "pinned"
    assert (await _statuses(engine))[B] == "superseded"
    # unpin: the set becomes serving again (no other is active)
    assert (await registry.unpin(A)).status == "active"
    assert await registry.serving_version() == ("active", A)
    # unpin of a non-pinned and of an unknown set
    with pytest.raises(PinError, match="not pinned"):
        await registry.unpin(A)
    ghost = "sha256:" + "5" * 64
    with pytest.raises(PinError) as nf:
        await registry.unpin(ghost)
    assert nf.value.failure_type == FailureType.NOT_FOUND
    with pytest.raises(PinError) as nf2:
        await registry.pin(ghost)
    assert nf2.value.failure_type == FailureType.NOT_FOUND
    statuses = list((await _statuses(engine)).values())
    assert statuses.count("active") == 1 and statuses.count("pinned") == 0


async def test_an_accepted_set_beside_a_pinned_one(engine: Any, cas: CasStore) -> None:
    registry = ModelRegistry(engine, cas)
    a, arts_a = _trained(211)
    b, arts_b = _trained(212)
    c, arts_c = _trained(213)
    await registry.store(a, arts_a, reference_version=None, inputs=INPUTS)
    await registry.store(b, arts_b, reference_version=a["set_version"], inputs=INPUTS)
    await registry.pin(a["set_version"])  # pinned A, active B
    # judged against what serves (A): the new set supersedes the ACTIVE one only
    stored = await registry.store(
        c, arts_c, reference_version=a["set_version"], inputs=INPUTS
    )
    assert stored.row is not None and stored.row.status == "active"
    assert stored.superseded == b["set_version"]
    assert await _statuses(engine) == {
        a["set_version"]: "pinned",
        b["set_version"]: "superseded",
        c["set_version"]: "active",
    }
    assert await registry.serving_version() == ("pinned", a["set_version"])
    # unpin while another set is active: the pinned one is just superseded
    assert (await registry.unpin(a["set_version"])).status == "superseded"
    assert await registry.serving_version() == ("active", c["set_version"])


async def test_a_rejected_set_is_never_serving_or_activated_by_pin_unpin(
    engine: Any, cas: CasStore
) -> None:
    registry = ModelRegistry(engine, cas)
    good, arts = _trained(221)
    await registry.store(good, arts, reference_version=None, inputs=INPUTS)
    noise, arts_n = _trained(222, "noise")
    await registry.store(
        noise, arts_n, reference_version=good["set_version"], inputs=INPUTS
    )
    with pytest.raises(PinError):
        await registry.pin(noise["set_version"])
    with pytest.raises(PinError, match="not pinned"):
        await registry.unpin(noise["set_version"])
    assert (await _statuses(engine))[noise["set_version"]] == "rejected"
    assert await registry.serving_version() == ("active", good["set_version"])


async def test_store_refuses_a_forged_document_without_changing_the_registry(
    engine: Any, cas: CasStore
) -> None:
    registry = ModelRegistry(engine, cas)
    good, arts = _trained(231)
    await registry.store(good, arts, reference_version=None, inputs=INPUTS)
    other, other_arts = _trained(232)
    # member version claims one digest, bytes hash to another
    forged_arts = {
        v: arts[next(iter(arts))] for v in other_arts
    }  # right shape, wrong bytes
    with pytest.raises(ValueError):
        await registry.store(
            other, forged_arts, reference_version=good["set_version"], inputs=INPUTS
        )
    # a member missing from the artefact map
    with pytest.raises(ValueError):
        await registry.store(
            other, {}, reference_version=good["set_version"], inputs=INPUTS
        )
    assert await _statuses(engine) == {good["set_version"]: "active"}


async def test_an_accepted_set_with_unloadable_artefacts_is_not_activated(
    engine: Any, cas: CasStore
) -> None:
    """Hardening: the child's output is trusted by the store. A set whose
    artefact bytes hash to their name but are not an artefact would become
    ``active`` and then never load (the planner silently falls back for
    ever; the retrain then skips on an unchanged watermark)."""
    registry = ModelRegistry(engine, cas)
    good, arts = _trained(241)
    await registry.store(good, arts, reference_version=None, inputs=INPUTS)
    other, other_arts = _trained(242)
    junk = b'{"not":"an artefact"}'
    forged = json.loads(json.dumps(other))
    for member in forged["members"]:
        if member["algorithm"] != "none":
            member["version"] = digest(junk)
    try:
        result = await registry.store(
            forged,
            {digest(junk): junk},
            reference_version=good["set_version"],
            inputs=INPUTS,
        )
    except (ValueError, PinError):
        return  # refused: fine
    statuses = await _statuses(engine)
    if result.row is not None:
        assert statuses.get(forged["set_version"]) != "active", (
            f"an unloadable set was activated; serving is now broken: {statuses}"
        )


# --------------------------------------------------------- rejected re-judged


async def test_a_rejected_set_is_rejudged_when_the_gate_settings_change(
    engine: Any, cas: CasStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retrain task records the training settings and retrains when they
    change. A set rejected under a strict ``ml_min_effect_log`` must be
    accepted when the operator relaxes it (same data, same artefacts): the
    old rejected row must not shadow the new verdict (same_set)."""
    built = _built(_rows(251))

    async def fake_build(_engine: Any, *, max_runs: int) -> export.BuiltDataset:
        return built

    monkeypatch.setattr(export, "build_dataset", fake_build)
    registry = ModelRegistry(engine, cas)
    source = ModelSource(registry, "auto")
    strict = MlRetrainer(engine, registry, source, _planner(ml_min_effect_log=5.0))
    first = await strict.retrain_now()
    assert first.error is None, first
    assert first.row is not None and first.row.status == "rejected", first
    assert (first.row.reason or "").startswith("not_better")
    relaxed = MlRetrainer(engine, registry, source, _planner(ml_min_effect_log=0.02))
    second = await relaxed.retrain_now()
    assert second.error is None, second
    assert second.skipped != "no_new_runs"  # the settings changed
    assert second.skipped != "same_set", (
        "relaxing ml_min_effect_log never re-judges the rejected set: the "
        "operator's change has no effect"
    )
    assert second.row is not None and second.row.status == "active"


# ------------------------------------------------------------- retrain races


async def test_two_controllers_one_run(
    engine: Any,
    cas: CasStore,
    pg_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _built(_rows(261))

    async def fake_build(_engine: Any, *, max_runs: int) -> export.BuiltDataset:
        return built

    monkeypatch.setattr(export, "build_dataset", fake_build)
    engine2 = create_engine(pg_settings, pool_size=4)
    gate = asyncio.Event()
    entered = 0

    async def slow(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        nonlocal entered
        entered += 1
        await asyncio.wait_for(gate.wait(), 60)
        return await run_child(argv, timeout_s)

    try:
        reg1, reg2 = ModelRegistry(engine, cas), ModelRegistry(engine2, cas)
        t1 = MlRetrainer(
            engine, reg1, ModelSource(reg1, "auto"), _planner(), child=slow
        )
        t2 = MlRetrainer(
            engine2, reg2, ModelSource(reg2, "auto"), _planner(), child=slow
        )
        first = asyncio.create_task(t1.retrain_now("scheduled"))
        for _ in range(200):
            if entered:
                break
            await asyncio.sleep(0.05)
        assert entered == 1
        second = await t2.retrain_now("scheduled")
        assert second.skipped == "busy", second
        gate.set()
        done = await first
        assert done.error is None and done.row is not None
        assert entered == 1  # the second controller never ran a child
        # the lock is free again afterwards
        third = await t2.retrain_now("scheduled")
        assert third.skipped in ("no_new_runs", "same_set"), third
    finally:
        gate.set()
        await engine2.dispose()


async def test_a_cancelled_retrain_releases_the_cross_controller_lock(
    engine: Any,
    cas: CasStore,
    pg_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _built(_rows(271))

    async def fake_build(_engine: Any, *, max_runs: int) -> export.BuiltDataset:
        return built

    monkeypatch.setattr(export, "build_dataset", fake_build)
    registry = ModelRegistry(engine, cas)
    entered = asyncio.Event()

    async def hang(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        entered.set()
        await asyncio.sleep(600)
        return 0, ""

    task = MlRetrainer(
        engine, registry, ModelSource(registry, "auto"), _planner(), child=hang
    )
    running = asyncio.create_task(task.retrain_now("manual"))
    await asyncio.wait_for(entered.wait(), 60)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not task.running
    # a different backend (own engine) must be able to take the lock now
    other = create_engine(pg_settings, pool_size=2)
    try:
        async with other.connect() as conn:
            got = (
                await conn.execute(
                    text(
                        "SELECT pg_try_advisory_lock("
                        "hashtextextended('meshforge.ml_retrain', 0))"
                    )
                )
            ).scalar()
            if got:
                await conn.execute(
                    text(
                        "SELECT pg_advisory_unlock("
                        "hashtextextended('meshforge.ml_retrain', 0))"
                    )
                )
    finally:
        await other.dispose()
    assert got, "the cancelled retrain's pooled connection still holds the lock"
    assert await _statuses(engine) == {}


async def test_two_post_train_requests_start_one_run(
    engine: Any, cas: CasStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _built(_rows(281))

    async def fake_build(_engine: Any, *, max_runs: int) -> export.BuiltDataset:
        return built

    monkeypatch.setattr(export, "build_dataset", fake_build)
    registry = ModelRegistry(engine, cas)
    release = asyncio.Event()

    async def slow(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        await release.wait()
        return await run_child(argv, timeout_s)

    task = MlRetrainer(
        engine, registry, ModelSource(registry, "auto"), _planner(), child=slow
    )
    starts = [task.start("manual") for _ in range(5)]
    assert starts.count(True) == 1
    release.set()
    result = await task.wait()
    assert result is not None and result.error is None
    assert task.retrains == 1


# --------------------------------------------------------- failure injection


async def _seed_active(engine: Any, cas: CasStore, rows: list[dict[str, Any]]) -> str:
    registry = ModelRegistry(engine, cas)
    table = table_from_rows(rows)
    result = train_set(table, FAST)
    gate = run_gate(table, FAST, result, Reference())
    doc = set_document(result, gate, FAST)
    assert doc["accepted"] is True
    arts = {m.version: m.artefact for m in result.members if m.artefact}
    stored = await registry.store(doc, arts, reference_version=None, inputs=INPUTS)
    assert stored.row is not None
    return str(doc["set_version"])


async def test_sigkill_of_the_real_child_stores_nothing_and_cleans_up(
    engine: Any,
    cas: CasStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = await _seed_active(engine, cas, _rows(291))
    built = _built(_rows(292))

    async def fake_build(_engine: Any, *, max_runs: int) -> export.BuiltDataset:
        return built

    monkeypatch.setattr(export, "build_dataset", fake_build)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    registry = ModelRegistry(engine, cas)
    source = ModelSource(registry, "auto")
    await source.refresh()
    assert source.current is not None

    async def kill_midway(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.sleep(1.5)
        proc.send_signal(signal.SIGKILL)
        _, err = await proc.communicate()
        return int(proc.returncode or 0), (err or b"").decode()

    task = MlRetrainer(engine, registry, source, _planner(), child=kill_midway)
    result = await task.retrain_now("manual")
    assert result.error == "child_failed", result
    assert result.row is None
    assert await _statuses(engine) == {active: "active"}
    assert source.current is not None and source.current.set_version == active
    assert list(scratch.iterdir()) == []  # the scratch directory is gone
    # the failure is not remembered as "done": the next tick tries again
    again = await task.retrain_now("scheduled")
    assert again.skipped != "no_new_runs"


async def test_a_child_that_overruns_is_killed_and_the_active_set_stays(
    engine: Any,
    cas: CasStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = await _seed_active(engine, cas, _rows(301))

    async def fake_build(_engine: Any, *, max_runs: int) -> export.BuiltDataset:
        return _built(_rows(302))

    monkeypatch.setattr(export, "build_dataset", fake_build)
    pid_file = tmp_path / "pid"

    async def sleeper(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        code = (
            f"import os,time;open({str(pid_file)!r},'w').write(str(os.getpid()));"
            "time.sleep(120)"
        )
        return await run_child([sys.executable, "-c", code], 1.5)

    registry = ModelRegistry(engine, cas)
    source = ModelSource(registry, "auto")
    task = MlRetrainer(engine, registry, source, _planner(), child=sleeper)
    result = await task.retrain_now("scheduled")
    assert result.error == "timeout"
    assert await _statuses(engine) == {active: "active"}
    pid = int(pid_file.read_text())
    stat = Path(f"/proc/{pid}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"


def _out_dir(argv: Sequence[str]) -> Path:
    return Path(argv[list(argv).index("--out") + 1])


@pytest.mark.parametrize(
    "damage",
    [
        "nothing",
        "bad_json",
        "missing_artefact",
        "wrong_bytes",
        "not_a_set",
        "no_members",
    ],
)
async def test_a_child_that_exits_zero_with_damaged_output_stores_nothing(
    engine: Any,
    cas: CasStore,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    active = await _seed_active(engine, cas, _rows(311))
    rows = _rows(312)
    built = _built(rows)

    async def fake_build(_engine: Any, *, max_runs: int) -> export.BuiltDataset:
        return built

    monkeypatch.setattr(export, "build_dataset", fake_build)
    doc, arts = _trained(312)

    async def liar(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        out = _out_dir(argv)
        out.mkdir(parents=True, exist_ok=True)
        if damage == "nothing":
            return 0, ""
        if damage == "bad_json":
            (out / "set.json").write_text("{truncated")
            return 0, ""
        if damage == "not_a_set":
            (out / "set.json").write_text("[1, 2, 3]")
            return 0, ""
        if damage == "no_members":
            (out / "set.json").write_text(
                json.dumps({"set_version": "sha256:" + "1" * 64})
            )
            return 0, ""
        (out / "set.json").write_text(json.dumps(doc))
        for member in doc["members"]:
            if member["artefact"] is None:
                continue
            if damage == "wrong_bytes":
                (out / member["artefact"]).write_bytes(b"not the artefact")
        return 0, ""

    registry = ModelRegistry(engine, cas)
    source = ModelSource(registry, "auto")
    task = MlRetrainer(engine, registry, source, _planner(), child=liar)
    result = await task.retrain_now("manual")
    assert result.row is None, result
    assert result.error is not None or result.skipped is not None
    assert await _statuses(engine) == {active: "active"}
    # the process-level task survived and is usable
    assert not task.running


# --------------------------------------------------------------------- GC


async def test_gc_never_deletes_the_active_pinned_or_window_artefacts(
    engine: Any, cas: CasStore
) -> None:
    registry = ModelRegistry(engine, cas)
    stored: list[tuple[dict[str, Any], dict[str, bytes]]] = []
    previous: str | None = None
    for seed in range(401, 410):
        doc, arts = _trained(seed)
        result = await registry.store(
            doc, arts, reference_version=previous, inputs=INPUTS
        )
        assert result.row is not None and result.row.status == "active"
        previous = doc["set_version"]
        stored.append((doc, arts))
    noise, noise_arts = _trained(450, "noise")
    await registry.store(noise, noise_arts, reference_version=previous, inputs=INPUTS)
    statuses = await _statuses(engine)
    assert list(statuses.values()).count("superseded") == 8
    oldest = stored[0][0]["set_version"]
    pinned_doc = stored[1][0]
    await registry.pin(pinned_doc["set_version"])

    def hexes(arts: dict[str, bytes]) -> set[str]:
        return {v.split(":", 1)[1] for v in arts}

    async with create_session_factory(engine)() as db:
        keep0 = await protected_artefacts(db, 0)
        keep3 = await protected_artefacts(db, 3)
        keep_all = await protected_artefacts(db, 1000)
    active_doc, active_arts = stored[-1]
    assert hexes(active_arts) <= keep0 and hexes(stored[1][1]) <= keep0
    assert not (hexes(noise_arts) & keep_all)  # rejected never protected
    # exactly the 3 newest superseded (seeds 406..408) plus active/pinned
    window = {h for _d, a in stored[5:8] for h in hexes(a)}
    assert window <= keep3
    assert hexes(stored[0][1]).isdisjoint(keep3)  # outside the rollback window
    assert hexes(stored[0][1]) <= keep_all
    assert oldest in statuses


# ------------------------------------------------------------ disk faults


async def test_corrupt_artefact_on_disk_is_typed_everywhere(
    engine: Any, cas: CasStore
) -> None:
    from meshforge.common.hashing import Digest

    registry = ModelRegistry(engine, cas)
    doc, arts = _trained(501)
    await registry.store(doc, arts, reference_version=None, inputs=INPUTS)
    other, other_arts = _trained(502)
    await registry.store(
        other, other_arts, reference_version=doc["set_version"], inputs=INPUTS
    )
    # flip one byte of the OLD set's artefact on the real disk (same size)
    (version,) = [m["version"] for m in doc["members"] if m["algorithm"] != "none"]
    path = cas.path_for(Digest.parse(version))
    path.chmod(0o644)
    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 0x01
    path.write_bytes(bytes(raw))
    with pytest.raises(PinError) as caught:
        await registry.pin(doc["set_version"])
    assert caught.value.details["reason"] == "digest_mismatch"
    # the serving (active) set is untouched
    assert await registry.serving_version() == ("active", other["set_version"])
    # a truncated and an emptied blob behave the same
    for data in (bytes(raw[:10]), b""):
        path.write_bytes(data)
        with pytest.raises(PinError):
            await registry.pin(doc["set_version"])
    # corrupt the ACTIVE set's blob: the source answers None, logs, never raises
    (active_version,) = [
        m["version"] for m in other["members"] if m["algorithm"] != "none"
    ]
    active_path = cas.path_for(Digest.parse(active_version))
    active_path.chmod(0o644)
    active_path.write_bytes(b'{"junk":true}')
    source = ModelSource(registry, "auto")
    assert await source.refresh() is None
    assert source.unavailable == "digest_mismatch"


async def test_a_full_disk_while_storing_leaves_the_registry_untouched(
    engine: Any, cas: CasStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meshforge.common.cas import CasError

    registry = ModelRegistry(engine, cas)
    good, arts = _trained(511)
    await registry.store(good, arts, reference_version=None, inputs=INPUTS)
    other, other_arts = _trained(512)

    def full(_data: bytes, _version: str) -> None:
        raise CasError("no space left on device")

    monkeypatch.setattr(registry, "_put", full)
    with pytest.raises(MlRegistryError):
        await registry.store(
            other, other_arts, reference_version=good["set_version"], inputs=INPUTS
        )
    assert await _statuses(engine) == {good["set_version"]: "active"}


async def test_a_full_disk_during_retrain_is_a_failed_result_not_a_crash(
    engine: Any, cas: CasStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meshforge.common.cas import CasError

    async def fake_build(_engine: Any, *, max_runs: int) -> export.BuiltDataset:
        return _built(_rows(521))

    monkeypatch.setattr(export, "build_dataset", fake_build)
    registry = ModelRegistry(engine, cas)
    source = ModelSource(registry, "auto")

    def full(_data: bytes, _version: str) -> None:
        raise CasError("no space left on device")

    monkeypatch.setattr(registry, "_put", full)
    task = MlRetrainer(engine, registry, source, _planner())
    result = await task.retrain_now("scheduled")
    assert result.row is None and result.error is not None, result
    assert await _statuses(engine) == {}
    assert not task.running
    # and it retries the next time (a failure is never remembered as done)
    monkeypatch.undo()
    monkeypatch.setattr(export, "build_dataset", fake_build)
    again = await task.retrain_now("scheduled")
    assert again.error is None and again.row is not None, again


@pytest.mark.parametrize("size", [0, 1, 20])
async def test_too_little_data_stores_nothing_with_the_real_child(
    engine: Any, cas: CasStore, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    rows = _rows(531)[:size]

    async def fake_build(_engine: Any, *, max_runs: int) -> export.BuiltDataset:
        return _built(rows)

    monkeypatch.setattr(export, "build_dataset", fake_build)
    registry = ModelRegistry(engine, cas)
    source = ModelSource(registry, "auto")
    task = MlRetrainer(engine, registry, source, _planner())
    result = await task.retrain_now("manual")
    assert result.row is None, result
    assert result.error is None, result  # not enough data is not a failure
    assert result.skipped == "insufficient_data", result
    assert await _statuses(engine) == {}
    assert await source.refresh() is None


@pytest.mark.parametrize(
    "reason",
    ["x" * 10_000, "family é日本 \U0001f600 end", "nul\x00inside", "line\nbreak\r\n"],
)
async def test_hostile_reason_and_gate_text_is_stored_or_cleanly_refused(
    engine: Any, cas: CasStore, reason: str
) -> None:
    """Reasons embed names that come from data (families, nodes). Whatever
    they contain, the store must neither corrupt the row nor leave the
    retrain failing on every tick with a database error."""
    registry = ModelRegistry(engine, cas)
    good, arts = _trained(541)
    await registry.store(good, arts, reference_version=None, inputs=INPUTS)
    noise, noise_arts = _trained(542, "noise")
    forged = json.loads(json.dumps(noise))
    forged["accepted"] = False
    forged["reason"] = f"coverage: {reason}"
    forged["gate"]["terms"]["compute"]["note"] = reason
    forged["members"][0]["reason"] = reason
    result = await registry.store(
        forged, noise_arts, reference_version=good["set_version"], inputs=INPUTS
    )
    assert result.row is not None and result.row.status == "rejected"
    assert len(result.row.reason or "") <= 512
    fetched = await registry.get(forged["set_version"])
    assert fetched is not None and fetched.status == "rejected"
