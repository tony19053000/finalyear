"""V0.9 G5 lane B item 0 (D245): the G4 reviewer's non-blocking findings.

* 0.1 (N2) ``finish`` cancelling an in-flight apply logs
  ``agent.conditions.stopped_while_applying`` (attempt id, kind);
* 0.2 (N3) one :func:`kill_and_reap` helper for every child a condition
  controller starts (``tc``, the burners);
* 0.3 (D244 residual) the marker stores the **expected netem params**; revert
  and the sweep delete a root netem only when ``tc qdisc show`` matches them -
  a foreign netem with other params survives.

Fake ``tc`` only (real ``tc`` is lab step C1-C3).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
import structlog

from meshforge.agent.conditions.base import kill_and_reap
from meshforge.agent.conditions.marker import ConditionMarker
from meshforge.agent.conditions.netem import (
    NetemController,
    TcRunner,
    delete_netem,
    expected_params,
    netem_matches,
    parse_netem_line,
    valid_expect,
)
from meshforge.agent.conditions.runner import ConditionRunner
from meshforge.agent.settings import ConditionSettings
from meshforge.common.bench_conditions import ConditionKind, Netem, parse_condition

pytestmark = pytest.mark.unit

_DEFAULT = "qdisc fq_codel 0: root refcnt 2"
_OURS_50 = {"delay_ms": 50.0, "loss_pct": 0.0, "rate_mbit": None}


class FakeTc(TcRunner):
    """One root qdisc per interface; ``replace`` installs what it was asked
    (in ``tc``'s own print format); ``gate`` can hold a replace."""

    def __init__(self) -> None:
        super().__init__(["fake-tc"])
        self.calls: list[tuple[str, ...]] = []
        self.qdisc: dict[str, str] = {}
        self.gate = asyncio.Event()
        self.gate.set()
        self.in_replace = asyncio.Event()

    async def run(self, *args: str) -> str:
        self.calls.append(args)
        verb, iface = args[1], args[3]
        if verb == "show":
            return self.qdisc.get(iface, _DEFAULT) + "\n"
        if verb == "replace":
            self.in_replace.set()
            await self.gate.wait()
            self.qdisc[iface] = (
                "qdisc netem 8001: root refcnt 2 limit 1000 " + " ".join(args[6:])
            )
            return ""
        if verb == "del":
            self.qdisc.pop(iface, None)
            return ""
        raise AssertionError(args)

    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls]


def _runner(tmp_path: Path, tc: FakeTc) -> ConditionRunner:
    values = iter([1.0, 61.0])

    async def rtt(_h: str, _p: int) -> float | None:
        return next(values, 61.0)

    return ConditionRunner(
        ConditionSettings(netem=True, netem_interface="eth9"),
        data_dir=tmp_path,
        kinds={ConditionKind.NETEM},
        tc=tc,
        controller=lambda: ("10.0.0.1", 50051),
        egress=lambda _h, _p: "eth9",
        rtt=rtt,
    )


# ------------------------------------------------ 0.1 stopped_while_applying


@pytest.mark.asyncio
async def test_finish_cancelling_an_apply_logs_stopped_while_applying(
    tmp_path: Path,
) -> None:
    tc = FakeTc()
    tc.gate.clear()
    runner = _runner(tmp_path, tc)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    applying = asyncio.ensure_future(active.apply())
    await asyncio.wait_for(tc.in_replace.wait(), 5)
    with structlog.testing.capture_logs() as logs:
        stopping = asyncio.ensure_future(runner.stop_all("owner_pause"))
        await asyncio.sleep(0)
        tc.gate.set()
        await asyncio.wait_for(stopping, 5)
    with pytest.raises(Exception, match="stopped while applying"):
        await applying
    events = [
        e for e in logs if e["event"] == "agent.conditions.stopped_while_applying"
    ]
    assert len(events) == 1
    assert events[0]["attempt_id"] == "att_1"
    assert events[0]["kind"] == "netem"
    assert events[0]["reason"] == "owner_pause"
    assert events[0]["log_level"] == "warning"


@pytest.mark.asyncio
async def test_finish_after_a_finished_apply_logs_nothing_of_the_kind(
    tmp_path: Path,
) -> None:
    tc = FakeTc()
    runner = _runner(tmp_path, tc)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    await active.apply()
    with structlog.testing.capture_logs() as logs:
        await runner.stop_all("owner_pause")
    assert not [e for e in logs if e["event"].endswith("stopped_while_applying")]


# ------------------------------------------------ 0.2 kill_and_reap


async def _sleeper() -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)"
    )


@pytest.mark.asyncio
async def test_kill_and_reap_kills_and_reaps() -> None:
    proc = await _sleeper()
    assert await kill_and_reap(proc) is True
    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_kill_and_reap_of_a_gone_child_is_fine() -> None:
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await proc.wait()
    assert await kill_and_reap(proc) is True


@pytest.mark.asyncio
async def test_kill_and_reap_holds_a_cancel_until_the_child_is_reaped() -> None:
    class SlowWait:
        """A child whose reap takes a while (the kill is already sent)."""

        def __init__(self) -> None:
            self.killed = False
            self.reaped = asyncio.Event()
            self.returncode: int | None = None

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            await asyncio.sleep(0.2)
            self.returncode = -9
            self.reaped.set()
            return -9

    proc = SlowWait()
    task = asyncio.ensure_future(kill_and_reap(proc))  # type: ignore[arg-type]
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()  # a second cancel cannot skip the reap either
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed and proc.reaped.is_set()


@pytest.mark.asyncio
async def test_kill_and_reap_gives_up_after_its_timeout() -> None:
    class Stuck:
        returncode = None

        def kill(self) -> None:
            pass

        async def wait(self) -> int:
            await asyncio.sleep(3600)
            return 0

    assert await kill_and_reap(Stuck(), timeout_s=0.05) is False  # type: ignore[arg-type]


def test_every_condition_child_stop_uses_the_helper() -> None:
    """N3: no controller kills a child by hand any more."""
    root = Path(__file__).resolve().parents[3] / "src/meshforge/agent/conditions"
    for name in ("netem.py", "load.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "kill_and_reap(" in text, name
        assert ".kill()" not in text, name


# ------------------------------------------------ 0.3 expected netem params


@pytest.mark.parametrize(
    ("line", "params"),
    [
        (
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms",
            {"delay_ms": 50.0, "loss_pct": 0.0, "rate_mbit": None},
        ),
        (
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 12.5ms loss 0.5% "
            "rate 1500Kbit",
            {"delay_ms": 12.5, "loss_pct": 0.5, "rate_mbit": 1.5},
        ),
        (
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 500us",
            {"delay_ms": 0.5, "loss_pct": 0.0, "rate_mbit": None},
        ),
        (
            "qdisc netem 8001: root refcnt 2 limit 1000 delay 1s",
            {"delay_ms": 1000.0, "loss_pct": 0.0, "rate_mbit": None},
        ),
        (
            "qdisc netem 8001: root refcnt 2 limit 1000 loss 1% rate 10Mbit seed 7",
            {"delay_ms": 0.0, "loss_pct": 1.0, "rate_mbit": 10.0},
        ),
        (
            "qdisc netem 8001: root refcnt 2 limit 1000 rate 1Gbit",
            {"delay_ms": 0.0, "loss_pct": 0.0, "rate_mbit": 1000.0},
        ),
    ],
)
def test_parse_netem_line_reads_tc_print_formats(
    line: str, params: dict[str, Any]
) -> None:
    assert parse_netem_line(line) == pytest.approx(params)


@pytest.mark.parametrize(
    "line",
    [
        "qdisc fq_codel 0: root refcnt 2",
        "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms 10ms",  # jitter
        "qdisc netem 8001: root refcnt 2 limit 1000 loss 1% 25%",  # correlation
        "qdisc netem 8001: root refcnt 2 limit 1000 loss state 1 2",  # a model
        "qdisc netem 8001: root refcnt 2 limit 1000 delay soon",
        "qdisc netem 8001: root refcnt 2 limit 1000 rate fast",
    ],
)
def test_parse_netem_line_refuses_what_we_never_install(line: str) -> None:
    assert parse_netem_line(line) is None


@pytest.mark.parametrize(
    ("spec", "line", "ours"),
    [
        ("netem:delay=50ms", "qdisc netem 8001: root limit 1000 delay 50ms", True),
        ("netem:delay=50ms", "qdisc netem 8001: root limit 1000 delay 50.2ms", True),
        ("netem:delay=50ms", "qdisc netem 9001: root limit 1000 delay 7ms", False),
        ("netem:delay=50ms", "qdisc netem 9001: root delay 50ms loss 1%", False),
        ("netem:delay=1.234ms", "qdisc netem 8001: root delay 1.23ms", True),
        ("netem:loss=1%", "qdisc netem 8001: root limit 1000 loss 1%", True),
        ("netem:loss=1%", "qdisc netem 8001: root limit 1000 loss 2%", False),
        ("netem:loss=1%", "qdisc netem 8001: root limit 1000", False),
        ("netem:rate=1.5mbit", "qdisc netem 8001: root rate 1500Kbit", True),
        ("netem:rate=1.5mbit", "qdisc netem 8001: root rate 15Mbit", False),
        (
            "netem:delay=50ms,loss=1%,rate=10mbit",
            "qdisc netem 8001: root limit 1000 delay 50ms loss 1% rate 10Mbit",
            True,
        ),
    ],
)
def test_netem_matches_only_its_own_params(spec: str, line: str, ours: bool) -> None:
    netem = parse_condition(spec)
    assert isinstance(netem, Netem)
    assert netem_matches(line, expected_params(netem)) is ours


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (_OURS_50, True),
        ({"delay_ms": 0.0, "loss_pct": 0.0, "rate_mbit": 5}, True),
        ({"delay_ms": 0.0, "loss_pct": 0.0, "rate_mbit": None}, False),  # nothing
        ({"delay_ms": 50.0, "loss_pct": 0.0}, False),
        ({"delay_ms": "50", "loss_pct": 0.0, "rate_mbit": None}, False),
        ({"delay_ms": True, "loss_pct": 0.0, "rate_mbit": None}, False),
        ({"delay_ms": float("nan"), "loss_pct": 0.0, "rate_mbit": None}, False),
        ({"delay_ms": -1, "loss_pct": 0.0, "rate_mbit": None}, False),
        ({**_OURS_50, "extra": 1}, False),
        ([50.0], False),
        ("delay=50ms", False),
    ],
)
def test_valid_expect(value: object, ok: bool) -> None:
    assert valid_expect(value) is ok


@pytest.mark.security
@pytest.mark.asyncio
async def test_sweep_leaves_a_foreign_netem_with_other_params(tmp_path: Path) -> None:
    """The D244 residual: crash after the bit was written, a foreign netem
    installed on that interface before the restart - it survives."""
    tc = FakeTc()
    foreign = "qdisc netem 9001: root refcnt 2 limit 1000 delay 7ms"
    tc.qdisc["eth9"] = foreign
    ConditionMarker(tmp_path / "conditions").put(
        "att_dead",
        {
            "spec": "netem:delay=50ms",
            "netem": "eth9",
            "netem_installed": True,
            "netem_expect": _OURS_50,
        },
    )
    runner = _runner(tmp_path, tc)
    with structlog.testing.capture_logs() as logs:
        assert await runner.sweep() == 1
    assert tc.qdisc["eth9"] == foreign
    assert "del" not in tc.verbs()
    assert runner.marker.load() == {}  # nothing of ours is left: entry done
    assert any(e["event"] == "agent.conditions.netem_not_ours" for e in logs)


@pytest.mark.asyncio
async def test_sweep_deletes_our_netem_when_the_params_match(tmp_path: Path) -> None:
    tc = FakeTc()
    tc.qdisc["eth9"] = "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms"
    ConditionMarker(tmp_path / "conditions").put(
        "att_dead",
        {"netem": "eth9", "netem_installed": True, "netem_expect": _OURS_50},
    )
    runner = _runner(tmp_path, tc)
    assert await runner.sweep() == 1
    assert "eth9" not in tc.qdisc
    assert "del" in tc.verbs()


@pytest.mark.security
@pytest.mark.asyncio
async def test_sweep_with_a_malformed_expectation_deletes_nothing(
    tmp_path: Path,
) -> None:
    tc = FakeTc()
    ours = "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms"
    tc.qdisc["eth9"] = ours
    ConditionMarker(tmp_path / "conditions").put(
        "att_dead",
        {"netem": "eth9", "netem_installed": True, "netem_expect": "delay=50ms"},
    )
    runner = _runner(tmp_path, tc)
    with structlog.testing.capture_logs() as logs:
        assert await runner.sweep() == 1
    assert tc.qdisc["eth9"] == ours
    assert tc.calls == []
    assert any(e["event"] == "agent.conditions.sweep_netem_unverifiable" for e in logs)


@pytest.mark.asyncio
async def test_a_g4_entry_without_params_keeps_the_g4_rule(tmp_path: Path) -> None:
    tc = FakeTc()
    tc.qdisc["eth9"] = "qdisc netem 8001: root refcnt 2 limit 1000 delay 7ms"
    ConditionMarker(tmp_path / "conditions").put(
        "att_old", {"netem": "eth9", "netem_installed": True}
    )
    runner = _runner(tmp_path, tc)
    with structlog.testing.capture_logs() as logs:
        assert await runner.sweep() == 1
    assert "eth9" not in tc.qdisc
    assert any(e["event"] == "agent.conditions.sweep_netem_legacy_entry" for e in logs)


@pytest.mark.asyncio
async def test_the_marker_carries_the_expected_params_from_before_the_replace(
    tmp_path: Path,
) -> None:
    tc = FakeTc()
    seen: list[dict[str, Any]] = []
    runner = _runner(tmp_path, tc)
    orig = tc.run

    async def spy(*args: str) -> str:
        if args[1] == "replace":
            seen.append(runner.marker.load())
        return await orig(*args)

    tc.run = spy  # type: ignore[method-assign]
    active = runner.prepare("att_1", "netem:delay=50ms,loss=1%", timeout_s=60)
    await active.apply()
    entry = seen[0]["att_1"]
    assert entry["netem_installed"] is True
    assert entry["netem_expect"] == {
        "delay_ms": 50.0,
        "loss_pct": 1.0,
        "rate_mbit": None,
    }
    assert netem_matches(tc.qdisc["eth9"], entry["netem_expect"])
    report = await active.finish()
    assert report.reverted and "eth9" not in tc.qdisc


@pytest.mark.security
@pytest.mark.asyncio
async def test_revert_leaves_a_netem_someone_put_in_place_of_ours() -> None:
    tc = FakeTc()
    netem = parse_condition("netem:delay=50ms")
    assert isinstance(netem, Netem)
    values = iter([1.0, 61.0])

    async def rtt(_h: str, _p: int) -> float | None:
        return next(values)

    ctl = NetemController(
        netem,
        tc=tc,
        interface="eth9",
        controller=lambda: ("10.0.0.1", 50051),
        egress=lambda _h, _p: "eth9",
        rtt=rtt,
    )
    await ctl.apply()
    foreign = "qdisc netem 9001: root refcnt 2 limit 1000 delay 7ms loss 3%"
    tc.qdisc["eth9"] = foreign  # someone replaced ours while the job ran
    assert await ctl.revert() is True  # nothing of ours is left there
    assert tc.qdisc["eth9"] == foreign
    assert "del" not in tc.verbs()


@pytest.mark.asyncio
async def test_delete_netem_with_matching_params_deletes() -> None:
    tc = FakeTc()
    tc.qdisc["eth9"] = "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms"
    assert await delete_netem(tc, "eth9", expect=_OURS_50) is None
    assert "eth9" not in tc.qdisc
