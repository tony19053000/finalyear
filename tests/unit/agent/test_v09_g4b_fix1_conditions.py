"""V0.9 G4 lane B fix round 1: apply/finish race, netem installed bit, sweep safety.

Reviewer findings (fix round 1):

1. ``finish()`` (``stop_all`` / agent exit) must never revert under a running
   ``apply()``: a ``tc replace`` landing after the revert left the laptop
   shaped while the report said ``reverted = true``.
2. The marker says ``netem_installed`` only right before ``tc replace``; the
   sweep deletes only such entries, and only a root qdisc that **is** a netem.
3. One hostile marker entry cannot stop the sweep (B2).
4. A malformed tree id makes ``prepare`` refuse, never raise (B4).

Fake ``tc`` only (real ``tc`` is lab step C1-C3).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.conditions.base import ConditionFailedError
from meshforge.agent.conditions.marker import ConditionMarker
from meshforge.agent.conditions.netem import TcError, TcRunner, delete_netem
from meshforge.agent.conditions.runner import ConditionRunner, _kill_burners
from meshforge.agent.settings import ConditionSettings
from meshforge.common.bench_conditions import ConditionKind
from meshforge.common.cas import CasStore

# ruff: noqa: ASYNC110
pytestmark = pytest.mark.unit

_DEFAULT = "qdisc fq_codel 0: root refcnt 2"


class GateTc(TcRunner):
    """A fake ``tc``. ``replace`` blocks on :attr:`gate`; with ``sticky`` it
    behaves like a real ``tc`` killed mid-call whose netlink change already
    went through: on cancellation it still installs once released."""

    def __init__(self, *, sticky: bool = True) -> None:
        super().__init__(["fake-tc"])
        self.calls: list[tuple[str, ...]] = []
        self.qdisc: dict[str, str] = {}
        self.gate = asyncio.Event()
        self.in_replace = asyncio.Event()
        self.sticky = sticky
        self.markers: list[dict[str, Any]] = []
        self.marker: ConditionMarker | None = None
        self.crash_on: set[str] = set()

    async def run(self, *args: str) -> str:
        self.calls.append(args)
        verb, iface = args[1], args[3]
        if iface in self.crash_on:
            raise RuntimeError("a tc bug")
        if verb == "show":
            return self.qdisc.get(iface, _DEFAULT) + "\n"
        if verb == "replace":
            if self.marker is not None:
                self.markers.append(self.marker.load())
            self.in_replace.set()
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                if self.sticky:
                    await self.gate.wait()
                    self.qdisc[iface] = "qdisc netem 8001: root refcnt 2 delay 50ms"
                raise
            self.qdisc[iface] = "qdisc netem 8001: root refcnt 2 delay 50ms"
            return ""
        if verb == "del":
            if iface not in self.qdisc:
                raise TcError("tc exited 2: RTNETLINK answers: No such file")
            del self.qdisc[iface]
            return ""
        raise AssertionError(args)

    def verbs(self) -> list[str]:
        return [c[1] for c in self.calls]


def _runner(
    tmp_path: Path, tc: GateTc, kinds: set[ConditionKind] | None = None
) -> ConditionRunner:
    values = iter([1.0, 61.0])

    async def rtt(_h: str, _p: int) -> float | None:
        return next(values, 61.0)

    runner = ConditionRunner(
        ConditionSettings(netem=True, netem_interface="eth9"),
        data_dir=tmp_path,
        kinds=kinds or {ConditionKind.NETEM},
        tc=tc,
        controller=lambda: ("10.0.0.1", 50051),
        egress=lambda _h, _p: "eth9",
        rtt=rtt,
    )
    tc.marker = runner.marker
    return runner


# ------------------------------------------------ 1: finish waits for apply


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("sticky", [True, False])
async def test_stop_all_during_tc_replace_leaves_no_qdisc_and_tells_the_truth(
    tmp_path: Path, sticky: bool
) -> None:
    tc = GateTc(sticky=sticky)
    runner = _runner(tmp_path, tc)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    applying = asyncio.ensure_future(active.apply())
    await asyncio.wait_for(tc.in_replace.wait(), 5)

    stopping = asyncio.ensure_future(runner.stop_all("owner_paused"))
    await asyncio.sleep(0.05)
    # the revert never runs under the in-flight replace
    assert "del" not in tc.verbs()
    if sticky:  # the replace finishes before anything is reverted
        assert not stopping.done()
    tc.gate.set()
    assert await asyncio.wait_for(stopping, 5) == 1

    with pytest.raises(ConditionFailedError, match="stopped while applying"):
        await applying
    assert "eth9" not in tc.qdisc  # nothing left shaped
    assert runner.marker.load() == {}
    report = await active.finish()
    assert report.reverted is True
    assert report.verified is False
    assert "owner_paused" in report.reason
    # a deletion ran only if the replace really landed
    assert ("del" in tc.verbs()) is sticky


@pytest.mark.asyncio
async def test_apply_after_stop_all_is_refused_and_changes_nothing(
    tmp_path: Path,
) -> None:
    tc = GateTc()
    runner = _runner(tmp_path, tc)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    assert await runner.stop_all("owner_paused") == 1
    with pytest.raises(ConditionFailedError, match="stopped before it was applied"):
        await active.apply()
    assert tc.calls == []
    assert runner.marker.load() == {}
    report = await active.finish()
    assert report.verified is False and report.reverted is True


@pytest.mark.asyncio
async def test_a_cancelled_apply_is_awaited_by_finish_and_never_verified(
    tmp_path: Path,
) -> None:
    """The executor cancels its step (attempt cancelled) during apply."""
    tc = GateTc(sticky=True)
    runner = _runner(tmp_path, tc)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    applying = asyncio.ensure_future(active.apply())
    await asyncio.wait_for(tc.in_replace.wait(), 5)
    applying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await applying
    finishing = asyncio.ensure_future(active.finish())
    await asyncio.sleep(0.05)
    assert not finishing.done()  # waits for the replace to end
    tc.gate.set()
    report = await asyncio.wait_for(finishing, 5)
    assert report.verified is False and report.reverted is True
    assert "eth9" not in tc.qdisc
    assert runner.marker.load() == {}


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="a /bin/sh script (tc is Linux)")
async def test_real_tc_runner_kills_its_process_when_cancelled(tmp_path: Path) -> None:
    """``TcRunner.run`` cancelled: the child is gone before the error leaves."""
    script = tmp_path / "slow_tc.sh"
    pid_file = tmp_path / "pid"
    script.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 60\n")
    script.chmod(0o755)
    runner = TcRunner([str(script)])
    task = asyncio.ensure_future(runner.run("qdisc", "show", "dev", "eth9"))
    async with asyncio.timeout(5):
        while not pid_file.exists() or not pid_file.read_text().strip():
            await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    from mf_procs import pid_gone  # needs psutil (agent extra), like the old check

    # zombie, "dead" (being reaped) or missing - never a check-then-read race
    assert pid_gone(int(pid_file.read_text()))


# ------------------------------------------------ 2: installed bit


@pytest.mark.asyncio
async def test_marker_says_installed_only_from_right_before_the_replace(
    tmp_path: Path,
) -> None:
    tc = GateTc()
    tc.gate.set()
    runner = _runner(tmp_path, tc)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    await active.apply()
    # what the marker held while tc replace ran
    assert tc.markers == [
        {
            "att_1": {
                "spec": "netem:delay=50ms",
                "netem": "eth9",
                "netem_installed": True,
                "netem_expect": {"delay_ms": 50.0, "loss_pct": 0.0, "rate_mbit": None},
            }
        }
    ]
    report = await active.finish()
    assert report.verified and report.reverted
    assert runner.marker.load() == {}


@pytest.mark.security
@pytest.mark.asyncio
async def test_a_foreign_netem_refused_by_the_pre_check_is_never_deleted(
    tmp_path: Path,
) -> None:
    tc = GateTc()
    foreign = "qdisc netem 9001: root refcnt 2 delay 7ms"
    tc.qdisc["eth9"] = foreign
    runner = _runner(tmp_path, tc)
    active = runner.prepare("att_1", "netem:delay=50ms", timeout_s=60)
    with pytest.raises(ConditionFailedError, match="did not set"):
        await active.apply()
    # a crash here: the marker entry has no installed bit ...
    entry = runner.marker.load()["att_1"]
    assert "netem_installed" not in entry
    # ... so the next start's sweep leaves the foreign netem alone
    survivor = _runner(tmp_path, tc)
    assert await survivor.sweep() == 1
    assert tc.qdisc["eth9"] == foreign
    assert "del" not in tc.verbs()
    assert survivor.marker.load() == {}
    # and the attempt's own revert does not touch it either
    report = await active.finish()
    assert report.reverted is True and report.verified is False
    assert tc.qdisc["eth9"] == foreign


@pytest.mark.security
@pytest.mark.asyncio
async def test_sweep_never_deletes_a_non_netem_root_qdisc(tmp_path: Path) -> None:
    tc = GateTc()
    tc.qdisc["eth9"] = "qdisc htb 1: root refcnt 2 r2q 10 default 0"
    ConditionMarker(tmp_path / "conditions").put(
        "att_dead", {"netem": "eth9", "netem_installed": True}
    )
    runner = _runner(tmp_path, tc)
    assert await runner.sweep() == 1
    assert tc.qdisc["eth9"].startswith("qdisc htb")
    assert tc.verbs() == ["show"]
    assert runner.marker.load() == {}


@pytest.mark.security
@pytest.mark.asyncio
async def test_delete_netem_shows_first_and_deletes_only_a_netem() -> None:
    tc = GateTc()
    assert (
        await delete_netem(tc, "eth9", expect=None) is None
    )  # the default qdisc: untouched
    assert tc.verbs() == ["show"]
    tc.qdisc["eth9"] = "qdisc netem 8001: root refcnt 2 delay 50ms"
    assert await delete_netem(tc, "eth9", expect=None) is None
    assert tc.verbs() == ["show", "show", "del"]
    assert "eth9" not in tc.qdisc


@pytest.mark.asyncio
async def test_delete_netem_with_a_failing_show_deletes_nothing() -> None:
    class NoShow(GateTc):
        async def run(self, *args: str) -> str:
            if args[1] == "show":
                self.calls.append(args)
                raise TcError("tc exited 1: show refused")
            return await super().run(*args)

    tc = NoShow()
    tc.qdisc["eth9"] = "qdisc netem 8001: root refcnt 2 delay 50ms"
    cause = await delete_netem(tc, "eth9", expect=None)
    assert cause is not None and "show" in cause
    assert "del" not in tc.verbs()


# ------------------------------------------------ 3: sweep robustness (B2)


@pytest.mark.parametrize(
    "pids",
    [
        [[-5, 1.0]],
        [[0, 1.0]],
        [[True, 1.0]],
        [[2**70, 1.0]],
        [[5, True]],
        [[5, float("nan")]],
        [[-(2**70), 1.0]],
    ],
)
def test_kill_burners_never_raises_on_odd_pids(pids: Any) -> None:
    assert _kill_burners(pids) == 0


@pytest.mark.security
@pytest.mark.asyncio
async def test_one_crashing_marker_entry_does_not_stop_the_sweep(
    tmp_path: Path,
) -> None:
    tc = GateTc()
    tc.crash_on = {"bad0"}
    tc.qdisc["eth9"] = "qdisc netem 8001: root refcnt 2 delay 50ms"
    marker = ConditionMarker(tmp_path / "conditions")
    marker.put("att_a", {"netem": "bad0", "netem_installed": True})
    marker.put("att_b", {"netem": "eth9", "netem_installed": True})
    marker.put("att_c", {"pids": [[-1, 1.0], [0, 0]]})
    runner = _runner(tmp_path, tc)
    assert await runner.sweep() == 2  # att_b and att_c undone
    assert "eth9" not in tc.qdisc
    assert runner.marker.load() == {}  # the crashed entry is cleaned too


# ------------------------------------------------ 4: malformed tree id (B4)


@pytest.mark.asyncio
async def test_a_malformed_tree_id_is_a_cache_condition_failure(
    tmp_path: Path,
) -> None:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 20)
    store.prepare()
    runner = ConditionRunner(
        ConditionSettings(),
        data_dir=tmp_path,
        kinds={ConditionKind.CACHE},
        store=store,
    )
    active = runner.prepare("att_1", "cache:cold", timeout_s=60, tree_ids=["zz"])
    with pytest.raises(ConditionFailedError) as caught:
        await active.apply()
    assert str(caught.value).startswith("condition_failed:cache:")
    assert caught.value.failure_type.value == "config_error"
    report = await active.finish()
    assert report.verified is False
    assert runner.active == []
