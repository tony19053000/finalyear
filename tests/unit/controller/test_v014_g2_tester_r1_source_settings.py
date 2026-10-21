"""V0.14 G2 tester round 1 - CalibrationSource modes, startup pin check,
failure handling, refresher loop, settings validation."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from meshforge.common.errors import ConfigError
from meshforge.controller.calibration_store import (
    CalibrationRow,
    CalibrationSource,
    CalibrationStoreError,
    RefreshResult,
    parse_mode,
)
from meshforge.controller.planner.calibration import (
    Calibration,
    CalibrationConfig,
    Factor,
    TermFactors,
)
from meshforge.controller.planner_calibration_task import CalibrationRefresher
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit


def cal(version: int, f: float = 2.0) -> Calibration:
    return Calibration(version, {"compute": TermFactors(global_=Factor(f, 5, f))}, {})


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[int, Calibration | None] = {}
        self.active: int | None = None
        self.fail = False
        self.reads = 0
        self.loads: list[int] = []

    def _maybe_fail(self) -> None:
        if self.fail:
            raise CalibrationStoreError("db down", details={"cause": "OSError"})

    async def active_version(self) -> int | None:
        self.reads += 1
        self._maybe_fail()
        return self.active

    async def load(self, v: int) -> Calibration | None:
        self.loads.append(v)
        self._maybe_fail()
        return self.rows.get(v)

    async def row(self, v: int) -> Any:
        self._maybe_fail()
        if v not in self.rows:
            return None
        return CalibrationRow(v, None, "active", self.rows[v], {}, None, None, None)  # type: ignore[arg-type]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_off_never_touches_the_store_even_with_failures() -> None:
    s = FakeStore()
    s.fail = True
    src = CalibrationSource(s, "off")  # type: ignore[arg-type]
    assert run(src.refresh()) is None
    assert s.reads == 0 and src.failures == 0


def test_pin_missing_at_startup_is_config_error_not_fallback() -> None:
    s = FakeStore()
    s.rows[1] = cal(1)
    s.active = 1
    src = CalibrationSource(s, "7")  # type: ignore[arg-type]
    with pytest.raises(ConfigError) as ei:
        run(src.check())
    assert "7" in str(ei.value)
    assert src.current is None


def test_pin_with_unreadable_factors_is_config_error() -> None:
    s = FakeStore()
    s.rows[3] = None
    src = CalibrationSource(s, "3")  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        run(src.check())


def test_pin_ignores_the_active_version_and_survives_store_outage() -> None:
    s = FakeStore()
    s.rows[2] = cal(2, 1.5)
    s.rows[9] = cal(9, 3.0)
    s.active = 9
    src = CalibrationSource(s, "2")  # type: ignore[arg-type]

    async def go() -> None:
        await src.check()
        for _ in range(3):
            got = await src.refresh()
            assert got is not None and got.version == 2
        s.fail = True
        got = await src.refresh()
        assert got is not None and got.version == 2
        s.active = 12
        s.fail = False
        got = await src.refresh()
        assert got is not None and got.version == 2

    run(go())


def test_pin_when_check_was_skipped_loads_lazily_and_failure_keeps_none() -> None:
    s = FakeStore()
    s.rows[4] = cal(4)
    s.fail = True
    src = CalibrationSource(s, "4")  # type: ignore[arg-type]

    async def go() -> None:
        assert await src.refresh() is None  # failure, no version in use
        s.fail = False
        got = await src.refresh()
        assert got is not None and got.version == 4

    run(go())


def test_auto_keeps_version_in_use_when_store_fails_then_recovers() -> None:
    s = FakeStore()
    s.rows[1] = cal(1)
    s.rows[2] = cal(2, 3.0)
    s.active = 1
    src = CalibrationSource(s, "auto")  # type: ignore[arg-type]

    async def go() -> None:
        assert (await src.refresh()).version == 1  # type: ignore[union-attr]
        s.fail = True
        s.active = 2
        for _ in range(5):
            got = await src.refresh()  # must never raise
            assert got is not None and got.version == 1
        assert src.failures == 5
        s.fail = False
        assert (await src.refresh()).version == 2  # type: ignore[union-attr]
        # no re-load while unchanged
        n = len(s.loads)
        await src.refresh()
        await src.refresh()
        assert len(s.loads) == n

    run(go())


def test_auto_active_disappears_means_uncalibrated() -> None:
    s = FakeStore()
    s.rows[1] = cal(1)
    s.active = 1
    src = CalibrationSource(s, "auto")  # type: ignore[arg-type]

    async def go() -> None:
        assert await src.refresh() is not None
        s.active = None
        assert await src.refresh() is None

    run(go())


def test_auto_unexpected_exception_never_propagates() -> None:
    s = FakeStore()

    async def boom() -> int:
        raise RuntimeError("bug")

    s.active_version = boom  # type: ignore[method-assign]
    src = CalibrationSource(s, "auto")  # type: ignore[arg-type]
    assert run(src.refresh()) is None
    assert src.failures == 1


def test_auto_unreadable_active_factors_plan_uncalibrated() -> None:
    s = FakeStore()
    s.rows[5] = None
    s.active = 5
    src = CalibrationSource(s, "auto")  # type: ignore[arg-type]
    assert run(src.refresh()) is None


@pytest.mark.parametrize("v", ["auto", "off"])
def test_parse_mode_words(v: str) -> None:
    assert parse_mode(v) == (v, None)


# ------------------------------------------------------------ settings


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("auto", "auto"),
        ("off", "off"),
        (7, "7"),
        ("7", "7"),
        (" 12 ", "12"),
        ("007", "7"),
    ],
)
def test_calibration_setting_accepts(raw: Any, stored: str) -> None:
    assert PlannerSettings(calibration=raw).calibration == stored


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "AUTO",
        "Off",
        "0",
        "-1",
        "1.5",
        "x",
        "1e3",
        "٣",
        None,
        True,
        False,
        1.5,
        [1],
        {"a": 1},
        "9" * 30,
        2**63,
    ],
)
def test_calibration_setting_refuses(raw: Any) -> None:
    with pytest.raises(ValidationError):
        PlannerSettings(calibration=raw)


@pytest.mark.parametrize(
    "clamp",
    [
        (1.0, 5.0),
        (0.2, 1.0),
        (1.5, 5.0),
        (0.2, 0.9),
        (0.0, 5.0),
        (-1.0, 5.0),
        (0.2, float("inf")),
        (0.2, float("nan")),
        (0.2, 1001.0),
        (5.0, 0.2),
    ],
)
def test_clamp_must_bracket_one(clamp: tuple[float, float]) -> None:
    with pytest.raises(ValidationError):
        PlannerSettings(calibration_clamp=clamp)


@pytest.mark.parametrize(
    "kw",
    [
        {"calibration_window_runs": 0},
        {"calibration_window_runs": -3},
        {"calibration_window_runs": 1001},
        {"calibration_shrink_k": 0.0},
        {"calibration_shrink_k": -1.0},
        {"calibration_shrink_k": float("nan")},
        {"calibration_refresh_s": 0.0},
        {"calibration_refresh_s": 9.9},
        {"calibration_refresh_s": float("nan")},
    ],
)
def test_other_limits_refuse(kw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PlannerSettings(**kw)


def test_defaults_match_the_brief() -> None:
    s = PlannerSettings()
    assert s.calibration == "auto"
    assert s.calibration_window_runs == 200
    assert s.calibration_shrink_k == 5.0
    assert tuple(s.calibration_clamp) == (0.2, 5.0)
    assert s.calibration_refresh_s == 1800.0


def test_unknown_calibration_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        PlannerSettings(calibration_windw_runs=3)  # typo must not be silently ignored


# --------------------------------------------------------------- refresher


class CountingStore:
    """refresh() is slow and records overlap."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def refresh(self, config: CalibrationConfig, **_: Any) -> RefreshResult:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return RefreshResult(0, None, "too_few_runs")


def test_refresher_serialises_in_process_refreshes() -> None:
    store = CountingStore()
    r = CalibrationRefresher(store, PlannerSettings())  # type: ignore[arg-type]

    async def go() -> None:
        await asyncio.gather(*(r.refresh_now() for _ in range(8)))

    run(go())
    assert store.calls == 8 and store.max_active == 1


def test_refresher_stop_before_first_interval_never_refreshes() -> None:
    store = CountingStore()
    r = CalibrationRefresher(store, PlannerSettings(calibration_refresh_s=3600.0))  # type: ignore[arg-type]

    async def go() -> None:
        t = asyncio.create_task(r.run())
        await asyncio.sleep(0.05)
        r.request_stop()
        await asyncio.wait_for(t, 2.0)

    run(go())
    assert store.calls == 0
