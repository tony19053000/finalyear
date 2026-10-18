"""V0.9 G4 lane B tester round 2: stop_all / cancel at EVERY await point of a
netem apply, double stop_all, finish racing apply start, cancel during revert,
and the installed-bit sweep matrix.

Invariant after ``finish()``: no netem qdisc of ours is left, no marker entry,
and a report never says ``verified`` while the apply was cut (nor
``reverted`` while a qdisc is still there). Fake ``tc`` only.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.conditions.base import ConditionFailedError
from meshforge.agent.conditions.netem import TcError, TcRunner
from meshforge.agent.conditions.runner import ConditionRunner
from meshforge.agent.settings import ConditionSettings
from meshforge.common.bench_conditions import ConditionKind

# ruff: noqa: ASYNC110
pytestmark = pytest.mark.unit

_DEFAULT = "qdisc fq_codel 0: root refcnt 2"
_NETEM = "qdisc netem 8001: root refcnt 2 limit 1000 delay 50ms"

POINTS = ["show1", "rtt1", "replace", "show2", "rtt2"]


class Probe:
    """Fake tc / egress / rtt. The await point named ``stop_at`` blocks on a
    gate; ``sticky`` means the kernel-side effect lands even when the caller is
    cancelled (a real tc killed mid-netlink)."""

    def __init__(self, stop_at: str | None, sticky: bool) -> None:
        self.stop_at = stop_at
        self.sticky = sticky
        self.hit = asyncio.Event()
        self.gate = asyncio.Event()
        self.qdisc: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []
        self.deleted_non_netem = False
        self.rtt_calls = 0

    async def point(self, name: str) -> None:
        """Block at the named await point until the gate opens. A cancelled
        sticky point waits for the gate, then re-raises (the kernel-side effect
        is the caller's to land)."""
        if name != self.stop_at:
            return
        self.hit.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            if self.sticky:
                await self.gate.wait()
            raise


class FakeTc(TcRunner):
    def __init__(self, probe: Probe) -> None:
        super().__init__(["fake"])
        self.p = probe
        self.marker: Any = None
        self.marker_at_replace: dict[str, Any] = {}

    async def run(self, *args: str) -> str:
        p = self.p
        p.calls.append(args)
        verb, iface = args[1], args[3]
        if verb == "show":
            name = "show1" if not any(c[1] == "replace" for c in p.calls) else "show2"
            await p.point(name)
            return p.qdisc.get(iface, _DEFAULT) + "\n"
        if verb == "replace":
            if self.marker is not None:
                self.marker_at_replace = self.marker.load()
            try:
                await p.point("replace")
            except asyncio.CancelledError:
                if p.sticky:
                    p.qdisc[iface] = _NETEM
                raise
            p.qdisc[iface] = _NETEM
            return ""
        if verb == "del":
            if "netem" not in p.qdisc.get(iface, ""):
                if iface in p.qdisc:
                    p.deleted_non_netem = True
                raise TcError("tc exited 2: RTNETLINK answers: No such file")
            del p.qdisc[iface]
            return ""
        raise AssertionError(args)


def _runner(tmp_path: Path, probe: Probe) -> tuple[ConditionRunner, FakeTc]:
    tc = FakeTc(probe)

    def egress(_h: str, _p: int) -> str | None:
        return "eth9"

    async def rtt(_h: str, _p: int) -> float | None:
        probe.rtt_calls += 1
        name = "rtt1" if probe.rtt_calls == 1 else "rtt2"
        await probe.point(name)
        return 1.0 if probe.rtt_calls == 1 else 61.0

    runner = ConditionRunner(
        ConditionSettings(netem=True, netem_interface="eth9"),
        data_dir=tmp_path,
        kinds={ConditionKind.NETEM},
        tc=tc,
        controller=lambda: ("10.0.0.1", 50051),
        egress=egress,
        rtt=rtt,
    )
    tc.marker = runner.marker
    return runner, tc


def _assert_clean(runner: ConditionRunner, probe: Probe, report: Any) -> None:
    assert "eth9" not in probe.qdisc, "a netem qdisc of ours survived the finish"
    assert runner.marker.load() == {}
    assert runner.active == []
    assert not probe.deleted_non_netem
    assert report.reverted is True
    assert not (report.verified and "eth9" in probe.qdisc)


@pytest.mark.asyncio
@pytest.mark.parametrize("sticky", [False, True])
@pytest.mark.parametrize("point", POINTS)
async def test_stop_all_at_every_await_point_of_apply(
    tmp_path: Path, point: str, sticky: bool
) -> None:
    probe = Probe(point, sticky)
    runner, _tc = _runner(tmp_path, probe)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    applying = asyncio.ensure_future(active.apply())
    await asyncio.wait_for(probe.hit.wait(), 5)
    stopping = asyncio.ensure_future(runner.stop_all("owner_paused"))
    await asyncio.sleep(0.05)
    probe.gate.set()  # let a sticky effect land
    assert await asyncio.wait_for(stopping, 5) == 1
    with pytest.raises(ConditionFailedError):
        await asyncio.wait_for(applying, 5)
    report = await active.finish()
    _assert_clean(runner, probe, report)
    assert report.verified is False
    assert "owner_paused" in report.reason or "stopped" in report.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("sticky", [False, True])
@pytest.mark.parametrize("point", ["show1", "rtt1", "replace", "show2", "rtt2"])
async def test_executor_cancel_at_every_await_point_then_finish(
    tmp_path: Path, point: str, sticky: bool
) -> None:
    probe = Probe(point, sticky)
    runner, _tc = _runner(tmp_path, probe)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    applying = asyncio.ensure_future(active.apply())
    await asyncio.wait_for(probe.hit.wait(), 5)
    applying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await applying
    finishing = asyncio.ensure_future(active.finish())
    await asyncio.sleep(0.05)
    probe.gate.set()
    report = await asyncio.wait_for(finishing, 5)
    _assert_clean(runner, probe, report)
    assert report.verified is False


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["show1", "rtt1", "replace", "show2", "rtt2"])
async def test_double_and_concurrent_stop_all(tmp_path: Path, point: str) -> None:
    probe = Probe(point, sticky=True)
    runner, _tc = _runner(tmp_path, probe)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    applying = asyncio.ensure_future(active.apply())
    await asyncio.wait_for(probe.hit.wait(), 5)
    a = asyncio.ensure_future(runner.stop_all("one"))
    b = asyncio.ensure_future(runner.stop_all("two"))
    c = asyncio.ensure_future(active.finish())
    await asyncio.sleep(0.02)
    d = asyncio.ensure_future(active.finish())
    await asyncio.sleep(0.02)
    probe.gate.set()
    await asyncio.wait_for(asyncio.gather(a, b, c, d), 5)
    with pytest.raises(ConditionFailedError):
        await applying
    # a later stop_all finds nothing and is harmless
    assert await runner.stop_all("three") == 0
    reports = {id(r) for r in (c.result(), d.result())}
    assert len(reports) == 1  # the same report object
    _assert_clean(runner, probe, c.result())
    assert probe.calls.count(("qdisc", "del", "dev", "eth9", "root")) <= 1


@pytest.mark.asyncio
async def test_finish_before_apply_starts_and_apply_scheduled_in_same_tick(
    tmp_path: Path,
) -> None:
    probe = Probe(None, False)
    runner, _tc = _runner(tmp_path, probe)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    applying = asyncio.ensure_future(active.apply())
    stopping = asyncio.ensure_future(runner.stop_all("race"))  # same tick
    await asyncio.gather(stopping, applying, return_exceptions=True)
    report = await active.finish()
    _assert_clean(runner, probe, report)
    assert report.verified is False


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [0, 1, 2, 3])
async def test_stop_all_after_n_loop_ticks_without_gates(
    tmp_path: Path, delay: int
) -> None:
    probe = Probe(None, False)
    runner, _tc = _runner(tmp_path, probe)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    applying = asyncio.ensure_future(active.apply())
    for _ in range(delay):
        await asyncio.sleep(0)
    await runner.stop_all("tick")
    outcome = (await asyncio.gather(applying, return_exceptions=True))[0]
    report = await active.finish()
    _assert_clean(runner, probe, report)
    if outcome is None:  # the apply won the race: it may be verified, but the
        assert report.verified is False  # stop marks it broken: never verified
    assert report.verified is False


@pytest.mark.asyncio
async def test_finish_caller_cancelled_during_revert_still_reverts(
    tmp_path: Path,
) -> None:
    probe = Probe(None, False)
    runner, tc = _runner(tmp_path, probe)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    await active.apply()
    assert "eth9" in probe.qdisc
    slow = asyncio.Event()
    orig = tc.run

    async def slow_del(*args: str) -> str:
        if args[1] == "del":
            await slow.wait()
        return await orig(*args)

    tc.run = slow_del  # type: ignore[method-assign]
    first = asyncio.ensure_future(active.finish())
    await asyncio.sleep(0.05)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.ensure_future(active.finish())  # joins the same revert
    await asyncio.sleep(0.02)
    assert not second.done()
    slow.set()
    report = await asyncio.wait_for(second, 5)
    _assert_clean(runner, probe, report)
    assert report.verified is True  # nothing was cut: an honest success


@pytest.mark.asyncio
async def test_cancel_during_replace_after_apply_task_done_is_noop(
    tmp_path: Path,
) -> None:
    probe = Probe(None, False)
    runner, _tc = _runner(tmp_path, probe)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    await active.apply()
    r1 = await active.finish()
    r2 = await active.finish()
    assert r1 is r2
    _assert_clean(runner, probe, r1)


@pytest.mark.asyncio
async def test_revert_failure_keeps_marker_and_reports_not_reverted(
    tmp_path: Path,
) -> None:
    probe = Probe(None, False)
    runner, tc = _runner(tmp_path, probe)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    await active.apply()
    orig = tc.run

    async def broken(*args: str) -> str:
        if args[1] in ("del", "show"):
            raise TcError("tc exited 1: busy")
        return await orig(*args)

    tc.run = broken  # type: ignore[method-assign]
    report = await active.finish()
    assert report.reverted is False
    entry = runner.marker.load()["att_1"]
    assert entry["netem"] == "eth9" and entry["netem_installed"] is True


# ------------------------------------------------------------ installed bit


class SweepTc(TcRunner):
    def __init__(self, show: str | Exception) -> None:
        super().__init__(["fake"])
        self.show_out = show
        self.calls: list[tuple[str, ...]] = []

    async def run(self, *args: str) -> str:
        self.calls.append(args)
        if args[1] == "show":
            if isinstance(self.show_out, Exception):
                raise self.show_out
            return self.show_out + "\n"
        return ""

    @property
    def deleted(self) -> bool:
        return any(c[1] == "del" for c in self.calls)


def _sweeper(tmp_path: Path, tc: TcRunner) -> ConditionRunner:
    return ConditionRunner(
        ConditionSettings(netem=True, netem_interface="eth9"),
        data_dir=tmp_path,
        kinds={ConditionKind.NETEM},
        tc=tc,
    )


def _put(tmp_path: Path, body: Any) -> None:
    d = tmp_path / "conditions"
    d.mkdir(parents=True, exist_ok=True)
    (d / "active.json").write_text(json.dumps(body), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "show", "delete", "kept"),
    [
        ({"netem": "eth9", "netem_installed": True}, _NETEM, True, False),
        ({"netem": "eth9"}, _NETEM, False, False),  # foreign / pre-check refused
        ({"netem": "eth9", "netem_installed": False}, _NETEM, False, False),
        ({"netem": "eth9", "netem_installed": "true"}, _NETEM, False, False),
        ({"netem": "eth9", "netem_installed": 1}, _NETEM, False, False),
        ({"netem": "eth9", "netem_installed": None}, _NETEM, False, False),
        ({"netem": "eth9", "netem_installed": True}, "qdisc htb 1: root", False, False),
        ({"netem": "eth9", "netem_installed": True}, "qdisc fq 0: root", False, False),
        ({"netem": "eth9", "netem_installed": True}, "", False, False),
        # netem as a CHILD of htb, not root: never deleted
        (
            {"netem": "eth9", "netem_installed": True},
            "qdisc htb 1: root\nqdisc netem 10: parent 1:1 limit 1000",
            False,
            False,
        ),
        ({"netem": 5, "netem_installed": True}, _NETEM, False, False),
        ({"netem": "", "netem_installed": True}, _NETEM, False, False),
        ({"netem": ["eth9"], "netem_installed": True}, _NETEM, False, False),
        ({"netem": None, "netem_installed": True}, _NETEM, False, False),
    ],
)
async def test_sweep_installed_bit_matrix(
    tmp_path: Path, entry: dict[str, Any], show: str, delete: bool, kept: bool
) -> None:
    _put(tmp_path, {"att_1": {"spec": "netem:delay=50ms", **entry}})
    tc = SweepTc(show)
    runner = _sweeper(tmp_path, tc)
    await runner.sweep()
    assert tc.deleted is delete
    assert (runner.marker.load() != {}) is kept


@pytest.mark.asyncio
async def test_sweep_failing_show_deletes_nothing_and_keeps_the_entry(
    tmp_path: Path,
) -> None:
    _put(
        tmp_path,
        {
            "att_1": {
                "spec": "netem:delay=50ms",
                "netem": "eth9",
                "netem_installed": True,
            }
        },
    )
    tc = SweepTc(TcError("tc exited 1: Cannot find device"))
    runner = _sweeper(tmp_path, tc)
    assert await runner.sweep() == 0
    assert not tc.deleted
    assert "att_1" in runner.marker.load()  # retried at the next start


@pytest.mark.asyncio
async def test_sweep_unexpected_exception_from_tc_never_aborts_other_entries(
    tmp_path: Path,
) -> None:
    class Boom(SweepTc):
        async def run(self, *args: str) -> str:
            if args[3] == "eth8":
                raise RuntimeError("tc bug")
            return await super().run(*args)

    _put(
        tmp_path,
        {
            "a": {"netem": "eth8", "netem_installed": True},
            "b": {"netem": "eth9", "netem_installed": True},
        },
    )
    tc = Boom(_NETEM)
    runner = _sweeper(tmp_path, tc)
    await runner.sweep()
    assert any(c[3] == "eth9" and c[1] == "del" for c in tc.calls)
    assert "b" not in runner.marker.load()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "not json",
        [],
        {"a": 5},
        {"a": []},
        {"a": {"netem": "eth9", "netem_installed": True, "pids": "x"}},
        {"a": {"pids": [[1], [1, 2, 3], "ab", None, [True, 1.0], [-5, 1.0], [0, 0]]}},
        {"a": {"pids": [[2**80, 1.0], [1, float("inf")], [1, 1e400]]}},
    ],
)
async def test_sweep_survives_corrupted_marker_shapes(
    tmp_path: Path, body: Any
) -> None:
    d = tmp_path / "conditions"
    d.mkdir()
    text = body if isinstance(body, str) else json.dumps(body)
    (d / "active.json").write_text(text, encoding="utf-8")
    tc = SweepTc(_DEFAULT)
    runner = _sweeper(tmp_path, tc)
    await runner.sweep()  # must not raise
    assert not tc.deleted
    # never signalled pid 1 / ourselves
    import os

    import psutil

    assert psutil.Process(1).is_running()
    assert psutil.Process(os.getpid()).is_running()


@pytest.mark.asyncio
async def test_stop_all_while_egress_lookup_blocks_in_its_thread(
    tmp_path: Path,
) -> None:
    import threading

    release = threading.Event()
    entered = threading.Event()
    probe = Probe(None, False)
    runner, _tc = _runner(tmp_path, probe)

    def blocking_egress(_h: str, _p: int) -> str | None:
        entered.set()
        release.wait(5)
        return "eth9"

    runner._egress = blocking_egress
    # rebuild the controller with the blocking lookup
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    active.controller._egress = blocking_egress  # type: ignore[union-attr]
    applying = asyncio.ensure_future(active.apply())
    while not entered.is_set():
        await asyncio.sleep(0.005)
    await asyncio.wait_for(runner.stop_all("pause"), 5)
    release.set()
    await asyncio.sleep(0.05)
    with pytest.raises(ConditionFailedError):
        await applying
    report = await active.finish()
    _assert_clean(runner, probe, report)
    assert not any(c[1] == "replace" for c in probe.calls)
