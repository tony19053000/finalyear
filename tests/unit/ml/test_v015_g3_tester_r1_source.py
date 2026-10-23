"""V0.15 G3 tester r1 - ModelSource under a flaky registry (pure logic with
a scripted registry double; the real registry is covered on PostgreSQL)."""

from __future__ import annotations

from typing import Any

import pytest
import structlog

from meshforge.common.errors import ConfigError
from meshforge.controller.ml.artefact import ArtefactError
from meshforge.controller.ml.registry import MlRegistryError
from meshforge.controller.ml.schema import FEATURE_VERSION
from meshforge.controller.ml.serve import LoadedSet
from meshforge.controller.ml.source import ModelSource, parse_ml_mode

pytestmark = pytest.mark.unit


class Row:
    def __init__(self, version: str, status: str = "active") -> None:
        self.set_version = version
        self.status = status
        self.reason = None
        self.feature_version = FEATURE_VERSION  # SetRow field (merge: stale check)


class Registry:
    def __init__(self) -> None:
        self.serving: tuple[str | None, str | None] = (None, None)
        self.rows: dict[str, Row] = {}
        self.fail_serving: Exception | None = None
        self.fail_load: dict[str, Exception] = {}
        self.loads = 0

    async def serving_version(self) -> tuple[str | None, str | None]:
        if self.fail_serving is not None:
            raise self.fail_serving
        return self.serving

    async def get(self, version: str) -> Row | None:
        return self.rows.get(version)

    async def load(self, row: Row) -> LoadedSet:
        self.loads += 1
        if row.set_version in self.fail_load:
            raise self.fail_load[row.set_version]
        return LoadedSet(set_version=row.set_version, feature_version=1, models={})


def _source(reg: Registry, setting: str = "auto") -> ModelSource:
    return ModelSource(reg, setting)  # type: ignore[arg-type]


def test_parse_modes() -> None:
    assert parse_ml_mode("auto") == ("auto", None)
    assert parse_ml_mode("off") == ("off", None)
    assert parse_ml_mode("sha256:" + "a" * 64) == ("pinned", "sha256:" + "a" * 64)


@pytest.mark.asyncio
async def test_a_flaky_database_keeps_the_set_and_logs_one_line_per_streak() -> None:
    reg = Registry()
    reg.rows["v1"] = Row("v1")
    reg.serving = ("active", "v1")
    source = _source(reg)
    assert (await source.refresh()) is not None
    with structlog.testing.capture_logs() as logs:
        reg.fail_serving = MlRegistryError("db", details={"cause": "OSError"})
        for _ in range(6):
            kept = await source.refresh()
            assert kept is not None and kept.set_version == "v1"
        assert source.failures == 6
        reg.fail_serving = RuntimeError("bug")
        for _ in range(3):
            assert (await source.refresh()) is not None
    warnings = [e for e in logs if e["event"].startswith("controller.planner.ml_read")]
    assert len(warnings) == 1, warnings  # one per streak, not per tick
    reg.fail_serving = None
    assert (await source.refresh()) is not None
    # a new streak logs again
    with structlog.testing.capture_logs() as logs2:
        reg.fail_serving = MlRegistryError("db", details={"cause": "OSError"})
        await source.refresh()
        await source.refresh()
    assert len([e for e in logs2 if "ml_read" in e["event"]]) == 1


@pytest.mark.asyncio
async def test_nothing_serving_clears_the_set_and_a_new_one_replaces_it() -> None:
    reg = Registry()
    reg.rows.update(v1=Row("v1"), v2=Row("v2"))
    reg.serving = ("active", "v1")
    source = _source(reg)
    first = await source.refresh()
    assert first is not None and first.set_version == "v1"
    loads = reg.loads
    for _ in range(5):  # unchanged: no reload
        await source.refresh()
    assert reg.loads == loads
    reg.serving = ("pinned", "v2")
    second = await source.refresh()
    assert second is not None and second.set_version == "v2" and source.kind == "pinned"
    reg.serving = (None, None)
    assert await source.refresh() is None
    assert source.current is None


@pytest.mark.asyncio
async def test_an_unloadable_set_is_logged_once_and_not_reread_every_tick() -> None:
    reg = Registry()
    reg.rows.update(v1=Row("v1"), v2=Row("v2"))
    reg.serving = ("active", "v1")
    reg.fail_load["v1"] = ArtefactError("gone", details={"reason": "artefact_missing"})
    source = _source(reg)
    with structlog.testing.capture_logs() as logs:
        for _ in range(10):
            assert await source.refresh() is None
    assert reg.loads == 1
    assert len([e for e in logs if "unloadable" in e["event"]]) == 1
    assert source.unavailable == "artefact_missing"
    # a different set version is tried at once
    reg.serving = ("active", "v2")
    loaded = await source.refresh()
    assert loaded is not None and loaded.set_version == "v2"
    assert source.unavailable is None


@pytest.mark.asyncio
async def test_a_set_that_vanishes_between_the_two_reads_is_not_an_error() -> None:
    reg = Registry()
    reg.serving = ("active", "ghost")
    source = _source(reg)
    assert await source.refresh() is None
    assert source.failures == 0


@pytest.mark.asyncio
async def test_off_never_touches_the_registry() -> None:
    reg = Registry()
    reg.fail_serving = RuntimeError("must not be called")
    source = _source(reg, "off")
    assert await source.refresh() is None
    await source.check()


@pytest.mark.asyncio
async def test_check_refuses_unknown_rejected_and_unloadable_pins() -> None:
    reg = Registry()
    reg.rows.update(
        ok=Row("ok", "superseded"), bad=Row("bad", "rejected"), gone=Row("gone")
    )
    reg.fail_load["gone"] = ArtefactError("x", details={"reason": "artefact_missing"})
    for setting in ("nope", "bad", "gone"):
        with pytest.raises(ConfigError):
            await _source(reg, setting).check()
    good = _source(reg, "ok")
    await good.check()
    assert good.kind == "config"
    kept: Any = await good.refresh()
    assert kept is not None and kept.set_version == "ok"
    # a configured pin ignores later changes in the registry
    reg.serving = ("pinned", "other")
    again: Any = await good.refresh()
    assert again.set_version == "ok"
