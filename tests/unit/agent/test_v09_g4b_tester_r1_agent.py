"""V0.9 G4 lane B tester round 1: agent condition controllers under failure injection.

Fake ``tc`` only (real tc is a lab step); every case is derived from the 0.9.5
promise: a condition is applied only after the agent re-validated it, is always
reverted (done / failed / crash / shutdown), and an unverified condition is
reported so (never labelled).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.conditions.base import ConditionController, ConditionFailedError
from meshforge.agent.conditions.marker import ConditionMarker
from meshforge.agent.conditions.netem import NetemController, TcError, TcRunner
from meshforge.agent.conditions.runner import ConditionRunner, _kill_burners
from meshforge.agent.settings import ConditionSettings
from meshforge.common.bench_conditions import ConditionKind

pytestmark = pytest.mark.unit


class FakeTc(TcRunner):
    def __init__(
        self, *, fail: set[str] | None = None, fail_after: dict[str, int] | None = None
    ) -> None:
        super().__init__(["fake-tc"])
        self.calls: list[tuple[str, ...]] = []
        self.qdisc: dict[str, str] = {}
        self.fail = fail or set()
        self.hang: set[str] = set()

    async def run(self, *args: str) -> str:
        self.calls.append(args)
        verb, iface = args[1], args[3]
        if verb in self.hang:
            await asyncio.sleep(3600)
        if verb in self.fail:
            raise TcError(f"tc exited 2: {verb} refused")
        if verb == "show":
            return self.qdisc.get(iface, "qdisc fq_codel 0: root refcnt 2") + "\n"
        if verb == "replace":
            self.qdisc[iface] = "qdisc netem 8001: root refcnt 2 " + " ".join(args[6:])
            return ""
        if verb == "del":
            if iface not in self.qdisc:
                raise TcError(
                    "tc exited 2: RTNETLINK answers: No such file or directory"
                )
            del self.qdisc[iface]
            return ""
        raise AssertionError(args)

    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls]


def _runner(
    tmp_path: Path,
    kinds: set[ConditionKind],
    tc: FakeTc,
    rtts: list[float | None] | None = None,
) -> ConditionRunner:
    values = iter(rtts or [1.0, 61.0])

    async def rtt(_h: str, _p: int) -> float | None:
        return next(values)

    runner = ConditionRunner(
        ConditionSettings(netem=True, netem_interface="eth9"),
        data_dir=tmp_path,
        kinds=kinds,
        tc=tc,
        controller=lambda: ("10.0.0.1", 50051),
        egress=lambda _h, _p: "eth9",
        rtt=rtt,
    )
    return runner


# ------------------------------------------------ agent re-validation


@pytest.mark.asyncio
@pytest.mark.security
@pytest.mark.parametrize(
    "spec",
    [
        "",
        "none",  # no condition is not sent; "none" as a spec is not applied by an agent
        "queue:2",
        "unavailable:30s",
        "cpu_load:1; reboot",
        "netem:delay=50ms loss=1%",
        "netem:loss=1.0%",
        "NETEM:loss=1%",
        "netem:delay=9999ms",
        "netem:delay=50ms\x00",
        "x" * 5000,
        "$(touch /tmp/pwn)",
    ],
)
async def test_hostile_spec_applies_nothing_and_reports_unverified(
    tmp_path: Path, spec: str
) -> None:
    tc = FakeTc()
    runner = _runner(
        tmp_path, {ConditionKind.NETEM, ConditionKind.CPU_LOAD, ConditionKind.CACHE}, tc
    )
    active = runner.prepare("att_1", spec, timeout_s=60)
    with pytest.raises(ConditionFailedError) as caught:
        await active.apply()
    assert caught.value.retryable is False
    report = await active.finish()
    assert report.verified is False and report.reverted is True
    assert len(report.spec) <= 64  # a refused spec is never echoed unbounded
    assert tc.calls == []  # not a single tc invocation
    assert runner.marker.load() == {} and runner.active == []


# ------------------------------------------------ netem apply / revert


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["show", "replace"])
async def test_netem_tc_failure_is_condition_failed_and_nothing_stays(
    tmp_path: Path, failing: str
) -> None:
    tc = FakeTc(fail={failing})
    runner = _runner(tmp_path, {ConditionKind.NETEM}, tc)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    with pytest.raises(ConditionFailedError):
        await active.apply()
    report = await active.finish()
    assert report.verified is False and report.reverted is True
    assert tc.qdisc == {}  # no shaping left behind
    assert runner.marker.load() == {}


@pytest.mark.asyncio
async def test_netem_revert_failure_keeps_the_marker_and_says_not_reverted(
    tmp_path: Path,
) -> None:
    tc = FakeTc()
    runner = _runner(tmp_path, {ConditionKind.NETEM}, tc)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    await active.apply()
    assert "eth9" in tc.qdisc
    tc.fail.add("del")
    report = await active.finish()
    assert report.verified is True  # it held while the job ran
    assert report.reverted is False  # but the owner must be told
    assert set(runner.marker.load()) == {"att_1"}  # so the next start retries
    # next agent start: tc works again -> swept
    tc.fail.discard("del")
    runner2 = _runner(tmp_path, set(), tc)
    assert await runner2.sweep() == 1
    assert "eth9" not in tc.qdisc and runner2.marker.load() == {}


@pytest.mark.asyncio
async def test_netem_rtt_probe_crash_is_a_failed_condition_and_reverts(
    tmp_path: Path,
) -> None:
    tc = FakeTc()

    async def boom(_h: str, _p: int) -> float:
        raise RuntimeError("probe bug")

    runner = _runner(tmp_path, {ConditionKind.NETEM}, tc)
    runner._rtt = boom
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    assert isinstance(active.controller, NetemController)
    active.controller._rtt = boom
    with pytest.raises(ConditionFailedError):
        await active.apply()
    report = await active.finish()
    assert report.verified is False and report.reverted is True
    assert tc.qdisc == {}


@pytest.mark.asyncio
async def test_netem_unreachable_controller_cannot_verify_delay(tmp_path: Path) -> None:
    tc = FakeTc()
    runner = _runner(tmp_path, {ConditionKind.NETEM}, tc, rtts=[None, None])
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    with pytest.raises(ConditionFailedError, match="RTT"):
        await active.apply()
    report = await active.finish()
    assert report.verified is False and report.reverted is True and tc.qdisc == {}


@pytest.mark.asyncio
async def test_concurrent_finish_reverts_exactly_once(tmp_path: Path) -> None:
    tc = FakeTc()
    runner = _runner(tmp_path, {ConditionKind.NETEM}, tc)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    await active.apply()
    reports = await asyncio.gather(*(active.finish() for _ in range(8)))
    assert tc.verbs().count("del") == 1
    assert all(r is reports[0] for r in reports)


@pytest.mark.asyncio
async def test_finish_survives_cancellation_of_one_waiter(tmp_path: Path) -> None:
    tc = FakeTc()
    runner = _runner(tmp_path, {ConditionKind.NETEM}, tc)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    await active.apply()
    gate = asyncio.Event()
    orig = tc.run

    async def slow(*args: str) -> str:
        if args[1] == "del":
            await gate.wait()
        return await orig(*args)

    tc.run = slow  # type: ignore[method-assign]
    waiter = asyncio.ensure_future(active.finish())
    await asyncio.sleep(0.05)
    waiter.cancel()
    other = asyncio.ensure_future(active.finish())
    gate.set()
    report = await other
    assert report.reverted is True and "eth9" not in tc.qdisc


@pytest.mark.asyncio
async def test_a_hung_tc_does_not_hang_forever_in_apply(tmp_path: Path) -> None:
    """tc has its own timeout (TcRunner); a FakeTc that hangs stands for it."""
    tc = FakeTc()
    tc.hang.add("replace")
    runner = _runner(tmp_path, {ConditionKind.NETEM}, tc)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(active.apply(), 0.3)
    # the executor cancels; the revert on the finish path must still clean up
    tc.hang.clear()
    report = await active.finish()
    assert report.reverted is True and tc.qdisc == {}


# ------------------------------------------------ controller crash inside apply / revert


class _Exploding(ConditionController):
    kind = "cpu_load"

    def __init__(self, *, in_apply: bool, in_revert: bool) -> None:
        super().__init__()
        self.in_apply, self.in_revert = in_apply, in_revert
        self.reverted = 0

    async def apply(self) -> None:
        if self.in_apply:
            raise RuntimeError("controller bug")

    async def revert(self) -> bool:
        self.reverted += 1
        if self.in_revert:
            raise RuntimeError("revert bug")
        return True


@pytest.mark.asyncio
async def test_a_crashing_apply_is_a_typed_failure_and_still_reverts(
    tmp_path: Path,
) -> None:
    from meshforge.agent.conditions.runner import ActiveCondition

    runner = _runner(tmp_path, {ConditionKind.CPU_LOAD}, FakeTc())
    ctl = _Exploding(in_apply=True, in_revert=False)
    active = ActiveCondition(runner, "att_1", "cpu_load:1", ctl)
    with pytest.raises(ConditionFailedError):
        await active.apply()
    report = await active.finish()
    assert ctl.reverted == 1
    assert report.verified is False and report.reverted is True
    assert runner.marker.load() == {}


@pytest.mark.asyncio
async def test_a_crashing_revert_is_reported_not_reverted_and_keeps_the_marker(
    tmp_path: Path,
) -> None:
    from meshforge.agent.conditions.runner import ActiveCondition

    runner = _runner(tmp_path, {ConditionKind.CPU_LOAD}, FakeTc())
    ctl = _Exploding(in_apply=False, in_revert=True)
    active = ActiveCondition(runner, "att_1", "cpu_load:1", ctl)
    await active.apply()
    report = await active.finish()
    assert report.reverted is False
    assert "att_1" in runner.marker.load()
    assert runner.active == []  # not tracked forever


@pytest.mark.asyncio
async def test_stop_all_and_close_revert_everything_and_mark_broken(
    tmp_path: Path,
) -> None:
    tc = FakeTc()
    runner = _runner(tmp_path, {ConditionKind.NETEM}, tc)
    active = runner.prepare("att_1", "netem:loss=1%", timeout_s=60)
    await active.apply()
    assert await runner.stop_all("owner_pause") == 1
    report = await active.finish()
    assert report.reverted is True and report.verified is False
    assert "owner_pause" in report.reason
    assert tc.qdisc == {}
    await runner.close()  # idempotent
    assert await runner.stop_all("x") == 0


# ------------------------------------------------ crash recovery (sweep)


@pytest.mark.asyncio
async def test_sweep_survives_hostile_marker_entries(tmp_path: Path) -> None:
    tc = FakeTc()
    tc.qdisc["eth9"] = "qdisc netem 8001: root delay 50ms"
    marker = ConditionMarker(tmp_path / "conditions")
    marker.put(
        "att_a",
        {
            "spec": "x" * 9999,
            "netem": "eth9",
            "netem_installed": True,  # V0.9 G4B fix 1: only installed ones
            "pids": "not a list",
        },
    )
    marker.put("att_b", {"pids": [[1], "x", [None, None], [2**70, 1.0], [0, 0]]})
    marker.put("att_c", {"netem": ["eth9"], "pids": [[os.getpid(), 1.0]]})
    marker.put("att_d", {"netem": 5})
    runner = _runner(tmp_path, set(), tc)
    assert await runner.sweep() >= 3
    assert "eth9" not in tc.qdisc


@pytest.mark.asyncio
async def test_sweep_survives_a_negative_pid_in_the_marker(tmp_path: Path) -> None:
    tc = FakeTc()
    ConditionMarker(tmp_path / "conditions").put("att_1", {"pids": [[-5, 1.0]]})
    runner = _runner(tmp_path, set(), tc)
    assert await runner.sweep() == 1


def test_sweep_never_kills_a_process_that_only_shares_a_recorded_pid() -> None:
    """pid reuse: the pid is ours (pytest) but its creation time/cmdline differ."""
    assert _kill_burners([[os.getpid(), 1.0]]) == 0
    import psutil

    me = psutil.Process()
    assert (
        _kill_burners([[os.getpid(), me.create_time()]]) == 0
    )  # right time, not a burner
    assert me.is_running()


@pytest.mark.asyncio
async def test_sweep_twice_is_idempotent(tmp_path: Path) -> None:
    tc = FakeTc()
    tc.qdisc["eth9"] = "qdisc netem 8001: root delay 50ms"
    ConditionMarker(tmp_path / "conditions").put(
        "att_1", {"netem": "eth9", "netem_installed": True}
    )
    runner = _runner(tmp_path, set(), tc)
    assert await runner.sweep() == 1
    assert await runner.sweep() == 0
    ConditionMarker(tmp_path / "conditions").put(
        "att_2", {"netem": "eth9", "netem_installed": True}
    )  # already gone
    assert await runner.sweep() == 1  # "nothing to delete" counts as undone
    assert runner.marker.load() == {}


@pytest.mark.asyncio
async def test_two_attempts_two_conditions_do_not_share_state(tmp_path: Path) -> None:
    tc = FakeTc()
    runner = _runner(tmp_path, {ConditionKind.NETEM, ConditionKind.CACHE}, tc)
    first = runner.prepare("att_1", "netem:loss=1%", timeout_s=60)
    second = runner.prepare("att_2", "netem:loss=2%", timeout_s=60)
    await first.apply()
    with pytest.raises(ConditionFailedError, match="already has a netem"):
        await second.apply()  # one shaped egress at a time: the second is refused
    r2 = await second.finish()
    assert r2.verified is False
    assert "eth9" in tc.qdisc  # the first one's shaping survived the second's failure
    r1 = await first.finish()
    assert r1.verified and r1.reverted and "eth9" not in tc.qdisc


def _unused(*_: Any) -> None: ...
