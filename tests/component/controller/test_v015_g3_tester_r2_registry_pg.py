# ruff: noqa: S311, RUF059, ASYNC240, PLR2004
"""V0.15 G3 tester r2 - verifies fix r1 (D1-D4) and probes the neighbouring
failure paths on a real PostgreSQL + real CAS.

D1 re-judge of a rejected set (history, supersede, idempotence, races),
D2 unloadable artefacts never activate / no raw native error / retry,
D4 control characters in every stored string (reasons, keys, params).
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncIterator
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
from meshforge.common.hashing import Digest
from meshforge.controller.ml.artefact import digest
from meshforge.controller.ml.registry import ModelRegistry, PinError
from meshforge.controller.ml.source import ModelSource
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set
from meshforge.controller.ml.train.fit_main import set_document
from meshforge.controller.ml.train.gate import Reference, run_gate
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(600)]

FAST = CompareSettings(terms=("compute",), algorithms=("ridge",), min_rows=100)
INPUTS = {"watermark": {"x": 1}, "train": {}}


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=10)
    try:
        async with eng.begin() as conn:
            await conn.execute(text("DELETE FROM ml_model_set"))
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def cas(tmp_path: Path) -> CasStore:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    return store


def _trained(seed: int) -> tuple[dict[str, Any], dict[str, bytes]]:
    rows = synthetic_rows(
        "signal", families=6, groups_per_family=5, runs_per_group=10, seed=seed
    )
    table = table_from_rows(rows)
    result = train_set(table, FAST)
    gate = run_gate(table, FAST, result, Reference())
    doc = set_document(result, gate, FAST)
    return doc, {m.version: m.artefact for m in result.members if m.artefact}


def _variant(doc: dict[str, Any], *, accepted: bool, tag: str) -> dict[str, Any]:
    """Same set (same artefacts, same version), another verdict / gate."""
    out = copy.deepcopy(doc)
    out["accepted"] = accepted
    out["reason"] = None if accepted else f"not_better: {tag}"
    out["gate"] = {**(out.get("gate") or {}), "accepted": accepted, "tag": tag}
    return out


async def _statuses(engine: Any) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT set_version, status FROM ml_model_set"))
        ).all()
    return {str(v): str(s) for v, s in rows}


async def _raw(engine: Any, version: str) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT status, reason, gate, inputs FROM ml_model_set "
                    "WHERE set_version = :v"
                ),
                {"v": version},
            )
        ).one()


# ------------------------------------------------------------------ D1


async def test_d1_rejected_then_accepted_is_rejudged_and_supersedes(
    engine: Any, cas: CasStore
) -> None:
    registry = ModelRegistry(engine, cas)
    a, arts_a = _trained(301)
    b, arts_b = _trained(302)
    await registry.store(a, arts_a, reference_version=None, inputs=INPUTS)
    rej = await registry.store(
        _variant(b, accepted=False, tag="strict"),
        arts_b,
        reference_version=a["set_version"],
        inputs=INPUTS,
    )
    assert rej.row is not None and rej.row.status == "rejected"
    assert await registry.serving_version() == ("active", a["set_version"])
    # the same verdict again: nothing written
    again = await registry.store(
        _variant(b, accepted=False, tag="strict"),
        arts_b,
        reference_version=a["set_version"],
        inputs=INPUTS,
    )
    assert again.row is None and again.skipped == "same_set"
    # relaxed settings: re-judged, activated, previous active superseded
    ok = await registry.store(
        _variant(b, accepted=True, tag="relaxed"),
        arts_b,
        reference_version=a["set_version"],
        inputs=INPUTS,
    )
    assert ok.rejudged and ok.row is not None and ok.row.status == "active"
    assert ok.superseded == a["set_version"]
    assert await _statuses(engine) == {
        a["set_version"]: "superseded",
        b["set_version"]: "active",
    }
    status, reason, gate, _ = await _raw(engine, b["set_version"])
    assert status == "active" and reason is None
    assert len(gate["history"]) == 1
    assert gate["history"][0]["status"] == "rejected"
    assert gate["history"][0]["reason"] == "not_better: strict"
    assert "history" not in gate["history"][0]["gate"]
    # members still exactly the set's members (no duplicates from re-judge)
    fetched = await registry.get(b["set_version"])
    assert fetched is not None
    assert len(fetched.members) == len(b["members"])
    # an active row is final: the old rejected verdict can never revert it
    back = await registry.store(
        _variant(b, accepted=False, tag="strict"),
        arts_b,
        reference_version=a["set_version"],
        inputs=INPUTS,
    )
    assert back.row is None and back.skipped == "same_set"
    assert (await _statuses(engine))[b["set_version"]] == "active"
    assert await registry.serving_version() == ("active", b["set_version"])


async def test_d1_rejected_to_rejected_with_new_settings_keeps_history(
    engine: Any, cas: CasStore
) -> None:
    registry = ModelRegistry(engine, cas)
    a, arts_a = _trained(311)
    b, arts_b = _trained(312)
    await registry.store(a, arts_a, reference_version=None, inputs=INPUTS)
    for i in range(6):
        res = await registry.store(
            _variant(b, accepted=False, tag=f"s{i}"),
            arts_b,
            reference_version=a["set_version"],
            inputs=INPUTS,
        )
        assert res.row is not None and res.row.status == "rejected"
    _, reason, gate, _ = await _raw(engine, b["set_version"])
    assert reason == "not_better: s5"
    assert [h["reason"] for h in gate["history"]] == [
        f"not_better: s{i}" for i in range(5)
    ]
    # the active set was never touched by rejected re-judging
    assert await registry.serving_version() == ("active", a["set_version"])


async def test_d1_rejudge_history_is_bounded(engine: Any, cas: CasStore) -> None:
    """Every settings flip appends to a JSONB array on one row. 200 flips must
    not grow the row without limit (operator toggling a setting)."""
    registry = ModelRegistry(engine, cas)
    a, arts_a = _trained(321)
    b, arts_b = _trained(322)
    await registry.store(a, arts_a, reference_version=None, inputs=INPUTS)
    for i in range(200):
        await registry.store(
            _variant(b, accepted=False, tag=f"flip{i % 2}-{i}"),
            arts_b,
            reference_version=a["set_version"],
            inputs=INPUTS,
        )
    _, _, gate, _ = await _raw(engine, b["set_version"])
    assert len(gate.get("history", [])) <= 50, (
        f"rejudge history grew to {len(gate['history'])} entries - unbounded"
    )


async def test_d1_concurrent_rejudges_leave_one_active_and_no_error(
    engine: Any, cas: CasStore
) -> None:
    registry = ModelRegistry(engine, cas)
    a, arts_a = _trained(331)
    b, arts_b = _trained(332)
    await registry.store(a, arts_a, reference_version=None, inputs=INPUTS)
    await registry.store(
        _variant(b, accepted=False, tag="strict"),
        arts_b,
        reference_version=a["set_version"],
        inputs=INPUTS,
    )
    jobs = [
        registry.store(
            _variant(b, accepted=bool(i % 2), tag=f"c{i}"),
            arts_b,
            reference_version=a["set_version"],
            inputs=INPUTS,
        )
        for i in range(12)
    ]
    out = await asyncio.gather(*jobs, return_exceptions=True)
    assert not [o for o in out if isinstance(o, BaseException)], out
    statuses = list((await _statuses(engine)).values())
    assert statuses.count("active") <= 1
    assert statuses.count("pinned") == 0
    status, _, _, _ = await _raw(engine, b["set_version"])
    assert status in {"active", "rejected"}
    # whatever the interleaving, serving is consistent with the table
    kind, version = await registry.serving_version()
    assert (kind, version) in {
        ("active", a["set_version"]),
        ("active", b["set_version"]),
    }


async def test_d1_rejudge_accepted_beside_a_pin_keeps_the_pin(
    engine: Any, cas: CasStore
) -> None:
    registry = ModelRegistry(engine, cas)
    a, arts_a = _trained(341)
    b, arts_b = _trained(342)
    await registry.store(a, arts_a, reference_version=None, inputs=INPUTS)
    await registry.pin(a["set_version"])
    await registry.store(
        _variant(b, accepted=False, tag="strict"),
        arts_b,
        reference_version=a["set_version"],
        inputs=INPUTS,
    )
    ok = await registry.store(
        _variant(b, accepted=True, tag="relaxed"),
        arts_b,
        reference_version=a["set_version"],
        inputs=INPUTS,
    )
    assert ok.row is not None and ok.row.status == "active"
    assert await _statuses(engine) == {
        a["set_version"]: "pinned",
        b["set_version"]: "active",
    }
    assert await registry.serving_version() == ("pinned", a["set_version"])


# ------------------------------------------------------------------ D2


def _forged(
    doc: dict[str, Any], blob: bytes
) -> tuple[dict[str, Any], dict[str, bytes]]:
    out = copy.deepcopy(doc)
    for m in out["members"]:
        if m["algorithm"] != "none":
            m["version"] = digest(blob)
    return out, {digest(blob): blob}


def _bad_blobs(real: bytes) -> dict[str, bytes]:
    flipped = json.loads(real)
    flipped["algorithm"] = "xgboost"  # claims a library/model it does not hold
    return {
        "junk": b'{"not":"an artefact"}',
        "truncated": real[: len(real) // 2],
        "empty": b"",
        "binary": bytes(range(256)) * 4,
        "wrong_algorithm": json.dumps(flipped).encode(),
        "huge_nesting": b"[" * 5000 + b"]" * 5000,
    }


@pytest.mark.parametrize(
    "kind", ["junk", "truncated", "empty", "binary", "wrong_algorithm", "huge_nesting"]
)
async def test_d2_unloadable_accepted_set_is_rejected_not_activated(
    engine: Any, cas: CasStore, kind: str
) -> None:
    registry = ModelRegistry(engine, cas)
    good, arts = _trained(351)
    await registry.store(good, arts, reference_version=None, inputs=INPUTS)
    other, other_arts = _trained(352)
    (real,) = [a for a in other_arts.values()]
    blob = _bad_blobs(real)[kind]
    forged, forged_arts = _forged(other, blob)
    result = await registry.store(
        forged, forged_arts, reference_version=good["set_version"], inputs=INPUTS
    )
    assert result.row is not None and result.row.status == "rejected", result
    assert (result.row.reason or "").startswith("unloadable_artefact:")
    for leak in ("Traceback", "xgboost", "XGBoost", "num_feature", ".py"):
        if leak == "xgboost" and kind == "wrong_algorithm":
            continue
        assert leak not in (result.row.reason or ""), result.row.reason
    assert await registry.serving_version() == ("active", good["set_version"])
    assert (await _statuses(engine))[good["set_version"]] == "active"
    _, _, gate, _ = await _raw(engine, forged["set_version"])
    assert gate["rule"] == "unloadable_artefact" and gate["accepted"] is False
    with pytest.raises(PinError):
        await registry.pin(forged["set_version"])
    # the planner's source keeps serving the good set
    source = ModelSource(registry, "auto")
    loaded = await source.refresh()
    assert loaded is not None and loaded.set_version == good["set_version"]


async def test_d2_unloadable_set_does_not_supersede_when_it_is_the_only_one(
    engine: Any, cas: CasStore
) -> None:
    registry = ModelRegistry(engine, cas)
    doc, arts = _trained(361)
    (real,) = list(arts.values())
    forged, forged_arts = _forged(doc, real[:50])
    res = await registry.store(
        forged, forged_arts, reference_version=None, inputs=INPUTS
    )
    assert res.row is not None and res.row.status == "rejected"
    assert await registry.serving_version() == (None, None)
    assert res.superseded is None


async def test_d2_unloadable_then_valid_artefacts_rejudge_same_version_is_stable(
    engine: Any, cas: CasStore
) -> None:
    """Re-storing the identical unloadable set is idempotent (same_set)."""
    registry = ModelRegistry(engine, cas)
    good, arts = _trained(371)
    await registry.store(good, arts, reference_version=None, inputs=INPUTS)
    other, other_arts = _trained(372)
    (real,) = list(other_arts.values())
    forged, forged_arts = _forged(other, real[:30])
    first = await registry.store(
        forged, forged_arts, reference_version=good["set_version"], inputs=INPUTS
    )
    second = await registry.store(
        forged, forged_arts, reference_version=good["set_version"], inputs=INPUTS
    )
    assert first.row is not None
    assert second.row is None and second.skipped == "same_set"


async def test_d2_blob_restored_serves_after_retry_without_restart(
    engine: Any, cas: CasStore
) -> None:
    registry = ModelRegistry(engine, cas)
    doc, arts = _trained(381)
    await registry.store(doc, arts, reference_version=None, inputs=INPUTS)
    (version,) = list(arts)
    path = cas.path_for(Digest.parse(version))
    original = path.read_bytes()
    path.chmod(0o644)
    path.unlink()
    now = [0.0]
    source = ModelSource(registry, "auto", retry_s=60.0, clock=lambda: now[0])
    assert await source.refresh() is None
    assert source.unavailable is not None
    path.write_bytes(original)
    now[0] += 5.0
    assert await source.refresh() is None  # inside the window: still bad
    now[0] += 120.0
    loaded = await source.refresh()
    assert loaded is not None and loaded.set_version == doc["set_version"]
    assert source.unavailable is None
    # corrupted again after a good load: current is kept until the set changes
    path.write_bytes(b"x")
    now[0] += 500.0
    again = await source.refresh()
    assert again is not None  # same version, already loaded: no re-read


async def test_d2_concurrent_store_of_unloadable_and_good_sets(
    engine: Any, cas: CasStore
) -> None:
    registry = ModelRegistry(engine, cas)
    base, base_arts = _trained(391)
    await registry.store(base, base_arts, reference_version=None, inputs=INPUTS)
    good, good_arts = _trained(392)
    bad_src, bad_src_arts = _trained(393)
    (real,) = list(bad_src_arts.values())
    bad, bad_arts = _forged(bad_src, real[:40])
    out = await asyncio.gather(
        *[
            registry.store(
                good, good_arts, reference_version=base["set_version"], inputs=INPUTS
            ),
            registry.store(
                bad, bad_arts, reference_version=base["set_version"], inputs=INPUTS
            ),
        ]
        * 4,
        return_exceptions=True,
    )
    assert not [o for o in out if isinstance(o, BaseException)], out
    st = await _statuses(engine)
    assert st[good["set_version"]] == "active"
    assert st[bad["set_version"]] == "rejected"
    assert list(st.values()).count("active") == 1


# ------------------------------------------------------------------ D4

CTRL = "".join(chr(c) for c in [*range(0x00, 0x20), 0x7F])


@pytest.mark.parametrize("path", ["fresh_rejected", "rejudge", "accepted"])
async def test_d4_every_control_character_everywhere_is_stored(
    engine: Any, cas: CasStore, path: str
) -> None:
    registry = ModelRegistry(engine, cas)
    a, arts_a = _trained(401)
    b, arts_b = _trained(402)
    await registry.store(a, arts_a, reference_version=None, inputs=INPUTS)
    nasty = f"fam{CTRL}ily"
    doc = _variant(b, accepted=(path == "accepted"), tag=nasty)
    doc["reason"] = None if path == "accepted" else f"coverage: {nasty}"
    doc["gate"][nasty] = {nasty: [nasty, 1]}
    doc["settings"] = {nasty: nasty}
    for m in doc["members"]:
        m["reason"] = nasty
        m["params"] = {**(m.get("params") or {}), nasty: nasty}
        m["metrics"] = {**(m.get("metrics") or {}), nasty: 1.0}
    inputs = {"watermark": {nasty: nasty}, "train": {nasty: [nasty]}}
    if path == "rejudge":
        await registry.store(
            _variant(b, accepted=False, tag="first"),
            arts_b,
            reference_version=a["set_version"],
            inputs=INPUTS,
        )
        doc = _variant(b, accepted=False, tag=nasty)
        doc["reason"] = f"coverage: {nasty}"
        doc["gate"][nasty] = nasty
    res = await registry.store(
        doc, arts_b, reference_version=a["set_version"], inputs=inputs
    )
    assert res.row is not None
    stored = await _raw(engine, b["set_version"])
    blob = json.dumps([stored[1], stored[2], stored[3]], ensure_ascii=False)
    for ch in [*range(0x00, 0x20), 0x7F]:
        if chr(ch) in "\t\n":
            continue
        assert chr(ch) not in (stored[1] or ""), hex(ch)
        assert chr(ch) not in blob.replace("\\n", "").replace("\\t", ""), hex(ch)
    fetched = await registry.get(b["set_version"])
    assert fetched is not None
    for m in fetched.members:
        assert "\x00" not in (m.reason or "")
    await registry.recent()  # listing does not choke on the row


async def test_d4_unicode_and_surrogates(engine: Any, cas: CasStore) -> None:
    """Valid unicode (CJK, emoji, RTL, combining, U+2028) survives untouched;
    a lone surrogate (cannot be UTF-8 encoded) must not make the store fail
    the whole retrain with a database error."""
    registry = ModelRegistry(engine, cas)
    a, arts_a = _trained(411)
    b, arts_b = _trained(412)
    await registry.store(a, arts_a, reference_version=None, inputs=INPUTS)
    pretty = "日本語 \U0001f600 ‮RTL é   end"
    doc = _variant(b, accepted=False, tag="u")
    doc["reason"] = f"coverage: {pretty}"
    res = await registry.store(
        doc, arts_b, reference_version=a["set_version"], inputs=INPUTS
    )
    assert res.row is not None and pretty in (res.row.reason or "")
    c, arts_c = _trained(413)
    doc2 = _variant(c, accepted=False, tag="s")
    doc2["reason"] = "coverage: lone \ud800 surrogate"
    doc2["gate"]["note"] = "lone \udfff surrogate"
    try:
        res2 = await registry.store(
            doc2, arts_c, reference_version=a["set_version"], inputs=INPUTS
        )
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"a lone surrogate in a reason breaks the store: {exc!r}")
    assert res2.row is not None and res2.row.status == "rejected"
