"""V0.15 G3 fix round 1 (Lead D2, D4).

* D2: a set that could not be loaded is not re-read every tick but is
  **tried again after ``retry_s``**, so a restored blob serves without a
  restart; a retry that fails again logs nothing new.
* D4: NUL and the other C0 controls (but tab / newline) and DEL are
  replaced in reasons and in every string (keys too) of the gate / inputs
  JSON before PostgreSQL sees them.
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog

from meshforge.controller.ml.artefact import ArtefactError
from meshforge.controller.ml.registry import safe_json, safe_text
from meshforge.controller.ml.serve import LoadedSet
from meshforge.controller.ml.source import ModelSource

pytestmark = pytest.mark.unit


class _Row:
    def __init__(self, version: str) -> None:
        self.set_version = version
        self.status = "active"
        self.reason = None


class _Registry:
    def __init__(self) -> None:
        self.broken = True
        self.loads = 0

    async def serving_version(self) -> tuple[str, str]:
        return "active", "v1"

    async def get(self, version: str) -> _Row:
        return _Row(version)

    async def load(self, row: _Row) -> LoadedSet:
        self.loads += 1
        if self.broken:
            raise ArtefactError("gone", details={"reason": "artefact_missing"})
        return LoadedSet(set_version=row.set_version, feature_version=1, models={})


@pytest.mark.asyncio
async def test_an_unloadable_set_is_retried_after_retry_s() -> None:
    reg = _Registry()
    now = [0.0]
    source = ModelSource(reg, "auto", retry_s=60.0, clock=lambda: now[0])  # type: ignore[arg-type]
    with structlog.testing.capture_logs() as logs:
        assert await source.refresh() is None
        for _ in range(5):  # within the window: not re-read
            now[0] += 10.0
            assert await source.refresh() is None
        assert reg.loads == 1
        now[0] += 20.0  # past 60 s: tried again, still gone, no new line
        assert await source.refresh() is None
        assert reg.loads == 2
    errors = [e for e in logs if e["event"] == "controller.planner.ml_set_unloadable"]
    assert len(errors) == 1
    assert source.unavailable == "artefact_missing"
    reg.broken = False  # the blob is restored
    now[0] += 61.0
    loaded = await source.refresh()
    assert loaded is not None and loaded.set_version == "v1"
    assert source.unavailable is None


def test_a_booster_with_a_foreign_feature_count_is_refused_at_load() -> None:
    """D2: the load-time probe answers one row through the whole path, so a
    model that cannot serve is refused when it is loaded (store, pin,
    source), never at the first plan."""
    pytest.importorskip("xgboost", reason="needs the ml extra")
    pytest.importorskip("sklearn", reason="needs the ml extra")
    import json

    from mf_ml_synthetic import synthetic_rows

    from meshforge.controller.ml.artefact import digest, encode
    from meshforge.controller.ml.serve import load_model
    from meshforge.controller.ml.train.algorithms import (
        GRID,
        Config,
        FitContext,
        fit_one,
    )
    from meshforge.controller.ml.train.compare import CompareSettings
    from meshforge.controller.ml.train.data import table_from_rows
    from meshforge.controller.ml.train.fit import artefact_bytes

    table = table_from_rows(
        synthetic_rows("signal", families=4, groups_per_family=3, runs_per_group=8)
    )
    rows = table.rows_of("compute")
    config = Config("xgboost", tuple(sorted(GRID["xgboost"][0].items())), 0)
    model = fit_one(config, FitContext(table=table), rows)
    doc = json.loads(
        artefact_bytes(table, "compute", config, model, rows, 0.2, CompareSettings())
    )
    booster = json.loads(doc["model"]["json"])
    booster["learner"]["learner_model_param"]["num_feature"] = "3"
    doc["model"]["json"] = json.dumps(booster, separators=(",", ":"))
    data = encode(doc)
    with pytest.raises(ArtefactError) as caught:
        load_model(data, expected_version=digest(data))
    assert caught.value.details["reason"] == "bad_model"


def test_control_characters_are_replaced_everywhere() -> None:
    assert safe_text("a\x00b\x07c\td\ne\x7f") == "a�b�c\td\ne�"
    doc: dict[str, Any] = {
        "k\x00": ["x\x01", {"y": "z\x1f"}, 3, None, 1.5],
        "ok": "plain",
    }
    assert safe_json(doc) == {
        "k�": ["x�", {"y": "z�"}, 3, None, 1.5],
        "ok": "plain",
    }
