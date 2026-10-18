"""V0.9 G4 lane B item 2-3: agent condition controllers without real load (D217).

Real ``tc`` is never run: netem is checked with a recording fake
:class:`TcRunner`; the egress interface and RTT probes are injected. The cache
condition runs on a real CAS + GC index in ``tmp_path``. Load generators with
real processes are in ``tests/component/agent/test_v09_g4_conditions_exec.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.conditions import burn
from meshforge.agent.conditions.base import ConditionFailedError
from meshforge.agent.conditions.cache import CacheController
from meshforge.agent.conditions.marker import ConditionMarker
from meshforge.agent.conditions.netem import (
    NetemController,
    TcError,
    TcRunner,
    delete_netem,
    is_loopback_interface,
    valid_interface_name,
)
from meshforge.agent.conditions.runner import ConditionRunner, probe_kinds
from meshforge.agent.settings import AgentSettings, ConditionSettings
from meshforge.common.bench_conditions import ConditionKind, Netem, parse_condition
from meshforge.common.cas import CasStore
from meshforge.common.config import ConfigError
from meshforge.common.gc_index import GcIndex
from meshforge.common.hashing import Digest
from meshforge.common.tree import build_tree

pytestmark = pytest.mark.unit


# ------------------------------------------------------------ fakes


class FakeTc(TcRunner):
    """Records every call; keeps one root qdisc per interface in memory."""

    def __init__(self, *, fail: set[str] | None = None) -> None:
        super().__init__(["fake-tc"])
        self.calls: list[tuple[str, ...]] = []
        self.qdisc: dict[str, str] = {}
        self.fail = fail or set()

    async def run(self, *args: str) -> str:
        self.calls.append(args)
        verb, iface = args[1], args[3]
        if verb in self.fail:
            raise TcError(f"tc exited 2: {verb} refused")
        if verb == "show":
            line = self.qdisc.get(iface, "qdisc fq_codel 0: root refcnt 2")
            return line + "\n"
        if verb == "replace":
            self.qdisc[iface] = (
                "qdisc netem 8001: root refcnt 2 limit 1000 " + " ".join(args[6:])
            )
            return ""
        if verb == "del":
            if iface not in self.qdisc:
                raise TcError(
                    "tc exited 2: RTNETLINK answers: No such file or directory"
                )
            del self.qdisc[iface]
            return ""
        raise AssertionError(args)


def _netem(
    text: str,
    tc: FakeTc,
    *,
    egress: str | None = "eth9",
    rtts: list[float] | None = None,
    iface: str = "eth9",
) -> NetemController:
    condition = parse_condition(text)
    assert isinstance(condition, Netem)
    values = iter(rtts or [1.0, 61.0])

    async def rtt(_host: str, _port: int) -> float | None:
        return next(values)

    return NetemController(
        condition,
        tc=tc,
        interface=iface,
        controller=lambda: ("10.0.0.1", 50051),
        egress=lambda _h, _p: egress,
        rtt=rtt,
    )


# ------------------------------------------------------------ marker


def test_marker_put_remove_and_atomic_file(tmp_path: Path) -> None:
    marker = ConditionMarker(tmp_path / "conditions")
    assert marker.load() == {}
    marker.put("att_1", {"spec": "cpu_load:1", "pids": [[11, 1.5]]})
    marker.put("att_2", {"spec": "netem:delay=5ms", "netem": "eth9"})
    assert set(marker.load()) == {"att_1", "att_2"}
    marker.remove("att_1")
    assert marker.load() == {"att_2": {"spec": "netem:delay=5ms", "netem": "eth9"}}
    marker.remove("att_2")
    assert not marker.path.exists()  # nothing applied -> no file
    assert list((tmp_path / "conditions").iterdir()) == []  # no temp file left


@pytest.mark.parametrize("body", [b"{not json", b"[1, 2]", b'{"a": 1}', b"\xff\xfe"])
def test_corrupt_marker_is_moved_aside_and_read_as_empty(
    tmp_path: Path, body: bytes
) -> None:
    marker = ConditionMarker(tmp_path)
    marker.path.write_bytes(body)
    assert marker.load() == {}
    assert not marker.path.exists()
    assert len(list(tmp_path.glob("active.json.corrupt-*"))) == 1


# ------------------------------------------------------------ burner


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["disk", "--deadline-s", "1", "--parent", "1"],
        ["cpu", "--parent", "1"],
        ["cpu", "--deadline-s", "0", "--parent", "1"],
        ["cpu", "--deadline-s", "1e9", "--parent", "1"],
        ["gpu", "--deadline-s", "1", "--parent", "1", "--percent", "0"],
    ],
)
def test_burner_refuses_bad_arguments(argv: list[str]) -> None:
    assert burn.main(argv) == burn.EXIT_BAD_ARGS


def test_burner_ends_at_its_deadline_and_when_the_parent_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    started = time.monotonic()
    assert burn.burn_cpu(time.monotonic() + 0.2, parent=burn.os.getppid()) == 0
    assert 0.2 <= time.monotonic() - started < 2.0
    monkeypatch.setattr(burn, "parent_alive", lambda _pid: False)
    started = time.monotonic()
    assert burn.burn_cpu(time.monotonic() + 60, parent=1) == 0
    assert time.monotonic() - started < 1.0


# ------------------------------------------------------------ netem


@pytest.mark.asyncio
@pytest.mark.security
async def test_netem_apply_verify_revert_with_fixed_arguments() -> None:
    tc = FakeTc()
    ctl = _netem("netem:delay=50ms,loss=1%", tc)
    await ctl.apply()
    assert tc.calls == [
        ("qdisc", "show", "dev", "eth9"),
        (
            "qdisc",
            "replace",
            "dev",
            "eth9",
            "root",
            "netem",
            "delay",
            "50ms",
            "loss",
            "1%",
        ),
        ("qdisc", "show", "dev", "eth9"),
    ]
    assert ctl.measured["rtt_added_ms"] == 60.0
    assert ctl.measured["interface"] == "eth9"
    assert "netem" in ctl.measured["qdisc"]
    assert ctl.marker_entry() == {
        "netem": "eth9",
        "netem_installed": True,
        # G5 item 0.3: the expected params, matched before any delete
        "netem_expect": {"delay_ms": 50.0, "loss_pct": 1.0, "rate_mbit": None},
    }
    assert await ctl.revert() is True
    assert tc.calls[-1] == ("qdisc", "del", "dev", "eth9", "root")
    assert "eth9" not in tc.qdisc
    assert await ctl.revert() is True  # idempotent: nothing more is run
    assert tc.calls[-1] == ("qdisc", "del", "dev", "eth9", "root")


@pytest.mark.asyncio
async def test_netem_rtt_not_grown_is_a_failed_condition_still_reverted() -> None:
    tc = FakeTc()
    ctl = _netem("netem:delay=100ms", tc, rtts=[1.0, 2.0])
    with pytest.raises(ConditionFailedError, match=r"RTT grew 1\.0 ms"):
        await ctl.apply()
    assert "eth9" in tc.qdisc  # applied: the caller's finish() reverts it
    assert await ctl.revert() is True and "eth9" not in tc.qdisc


@pytest.mark.asyncio
async def test_netem_loss_only_is_verified_by_the_qdisc_alone() -> None:
    tc = FakeTc()
    ctl = _netem("netem:loss=5%", tc, rtts=[])
    await ctl.apply()
    assert ctl.measured["rtt_before_ms"] is None
    assert tc.qdisc["eth9"].endswith("loss 5%")


@pytest.mark.asyncio
@pytest.mark.security
async def test_netem_refuses_the_wrong_interface_and_touches_nothing() -> None:
    tc = FakeTc()
    ctl = _netem("netem:delay=5ms", tc, egress="wlan0")
    with pytest.raises(ConditionFailedError, match="leaves through 'wlan0'"):
        await ctl.apply()
    assert tc.calls == []
    assert await ctl.revert() is True and tc.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("iface", ["lo", "lo:1", "-x", "a" * 16, "eth 0", "eth0;rm"])
@pytest.mark.security
async def test_netem_refuses_loopback_and_odd_names(iface: str) -> None:
    tc = FakeTc()
    ctl = _netem("netem:delay=5ms", tc, egress=iface, iface=iface)
    with pytest.raises(ConditionFailedError, match="refusing interface"):
        await ctl.apply()
    assert tc.calls == []


@pytest.mark.asyncio
async def test_netem_leaves_someone_elses_netem_alone() -> None:
    tc = FakeTc()
    tc.qdisc["eth9"] = "qdisc netem 1: root refcnt 2 delay 5ms"
    ctl = _netem("netem:delay=5ms", tc)
    with pytest.raises(ConditionFailedError, match="already has a netem"):
        await ctl.apply()
    assert [c[1] for c in tc.calls] == ["show"]
    assert await ctl.revert() is True  # never applied: nothing deleted
    assert tc.qdisc["eth9"].endswith("delay 5ms")


@pytest.mark.asyncio
async def test_netem_failed_delete_keeps_the_marker() -> None:
    tc = FakeTc()
    ctl = _netem("netem:delay=5ms", tc)
    await ctl.apply()
    tc.fail = {"del"}
    assert await ctl.revert() is False
    tc.fail = set()
    assert await ctl.revert() is True


@pytest.mark.asyncio
async def test_delete_netem_with_nothing_there_is_fine() -> None:
    assert await delete_netem(FakeTc(), "eth9", expect=None) is None


def test_interface_name_rules() -> None:
    assert valid_interface_name("wlp2s0") and valid_interface_name("enp0s31f6")
    assert not valid_interface_name("") and not valid_interface_name("-i")
    assert is_loopback_interface("lo")


# ------------------------------------------------------------ cache


@dataclass
class Stage:
    blobs_hit: int = 0
    bytes_hit: int = 0
    blobs_fetched: int = 0
    bytes_fetched: int = 0
    manifest_hit: bool = False


@pytest.fixture
def cas(tmp_path: Path) -> Iterator[tuple[CasStore, GcIndex, str, list[str]]]:
    root = tmp_path / "proj"
    root.mkdir()
    for i in range(3):
        (root / f"f{i}.txt").write_text(f"content {i}\n" * 10)
    built = build_tree(root)
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    index = GcIndex(tmp_path / "gc")
    index.open()
    store.put_bytes(built.manifest, built.tree.tree_id)
    members = sorted({e.sha256 for e in built.tree.files()})
    for entry in built.tree.files():
        store.put_bytes((root / entry.path).read_bytes())
    for key in [built.tree.tree_id.hex, *members]:
        index.upsert("blob", key, 10)
    yield store, index, str(built.tree.tree_id), members
    index.close()


@pytest.mark.asyncio
async def test_cache_cold_evicts_the_trees_blobs_but_not_protected_ones(
    cas: tuple[CasStore, GcIndex, str, list[str]],
) -> None:
    store, index, tree_id, members = cas
    index.lease("other_attempt", [("blob", members[0])])  # another attempt uses it
    ctl = CacheController("cold", store=store, index=index, tree_ids=[tree_id, ""])
    await ctl.apply()
    assert ctl.measured["evicted_blobs"] == 1 + len(members) - 1  # manifest + 2
    assert ctl.measured["kept_blobs"] == 1
    assert store.has(Digest.parse(f"sha256:{members[0]}"))
    assert not store.has(Digest.parse(tree_id))
    # staging then hit the protected blob: not cold -> condition failed
    with pytest.raises(ConditionFailedError, match="1 blob"):
        ctl.after_stage([Stage(blobs_hit=1, blobs_fetched=2)])


@pytest.mark.asyncio
async def test_cache_cold_pinned_tree_is_never_evicted(
    cas: tuple[CasStore, GcIndex, str, list[str]],
) -> None:
    store, index, tree_id, members = cas
    index.record_tree(Digest.parse(tree_id).hex, members)
    index.set_pins("tree", [Digest.parse(tree_id).hex])
    ctl = CacheController("cold", store=store, index=index, tree_ids=[tree_id])
    await ctl.apply()
    assert ctl.measured["evicted_blobs"] == 0
    assert all(store.has(Digest.parse(f"sha256:{m}")) for m in members)


@pytest.mark.asyncio
async def test_cache_cold_verified_when_nothing_was_a_hit(
    cas: tuple[CasStore, GcIndex, str, list[str]],
) -> None:
    store, index, tree_id, members = cas
    ctl = CacheController("cold", store=store, index=index, tree_ids=[tree_id])
    await ctl.apply()
    assert ctl.measured["evicted_blobs"] == 1 + len(members)
    assert index.usage("blob") == 0
    ctl.after_stage([Stage(blobs_fetched=3, bytes_fetched=90)])
    assert ctl.measured["blobs_fetched"] == 3 and ctl.measured["blobs_hit"] == 0


def test_cache_warm_needs_nothing_fetched(tmp_path: Path) -> None:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 20)
    ctl = CacheController("warm", store=store, index=None, tree_ids=[])
    ctl.after_stage([Stage(blobs_hit=3, manifest_hit=True)])
    assert ctl.measured["blobs_hit"] == 3
    with pytest.raises(ConditionFailedError, match="not warm"):
        ctl.after_stage([Stage(blobs_hit=2, blobs_fetched=1)])
    with pytest.raises(ConditionFailedError, match="no tree"):
        ctl.after_stage([])


# ------------------------------------------------------------ runner


def _runner(tmp_path: Path, kinds: set[ConditionKind], **extra: Any) -> ConditionRunner:
    return ConditionRunner(ConditionSettings(), data_dir=tmp_path, kinds=kinds, **extra)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spec", "needle"),
    [
        ("cpu_load:1", "does not offer cpu_load"),
        ("netem:loss=1.0%", "canonical form"),
        ("queue:2", "does not offer queue"),
        ("cpu_load:x", "not a whole number"),
        ("rm -rf /", "unknown condition"),
    ],
)
@pytest.mark.security
async def test_runner_refuses_what_it_did_not_advertise(
    tmp_path: Path, spec: str, needle: str
) -> None:
    runner = _runner(tmp_path, {ConditionKind.CACHE})
    active = runner.prepare("att_1", spec, timeout_s=60)
    with pytest.raises(ConditionFailedError, match=needle) as caught:
        await active.apply()
    assert caught.value.failure_type.value == "config_error"
    assert caught.value.retryable is False
    assert str(caught.value).startswith("condition_failed:")
    report = await active.finish()
    assert report.verified is False and report.reverted is True
    assert needle in report.reason
    assert runner.active == []


@pytest.mark.asyncio
async def test_runner_netem_end_to_end_and_marker(tmp_path: Path) -> None:
    tc = FakeTc()
    runner = _runner(
        tmp_path,
        {ConditionKind.NETEM},
        tc=tc,
        controller=lambda: ("10.0.0.1", 50051),
    )
    runner._settings = ConditionSettings(netem=True, netem_interface="eth9")
    active = runner.prepare("att_1", "netem:loss=1%", timeout_s=60)
    assert isinstance(active.controller, NetemController)
    active.controller._egress = lambda _h, _p: "eth9"
    await active.apply()
    assert runner.marker.load() == {
        "att_1": {
            "spec": "netem:loss=1%",
            "netem": "eth9",
            "netem_installed": True,
            "netem_expect": {"delay_ms": 0.0, "loss_pct": 1.0, "rate_mbit": None},
        }
    }
    report = await active.finish()
    assert report.verified and report.reverted
    assert json.loads(report.measured_json)["interface"] == "eth9"
    assert runner.marker.load() == {}
    assert await active.finish() is report  # idempotent


@pytest.mark.asyncio
async def test_sweep_undoes_a_crashed_runs_netem(tmp_path: Path) -> None:
    tc = FakeTc()
    tc.qdisc["eth9"] = "qdisc netem 8001: root delay 50ms"
    ConditionMarker(tmp_path / "conditions").put(
        "att_dead",
        {
            "spec": "netem:delay=50ms",
            "netem": "eth9",
            "netem_installed": True,
            "pids": [[999999, 1.0]],
        },
    )
    runner = _runner(tmp_path, set(), tc=tc)
    assert await runner.sweep() == 1
    assert "eth9" not in tc.qdisc
    assert runner.marker.load() == {}


@pytest.mark.asyncio
async def test_sweep_keeps_the_marker_when_the_delete_fails(tmp_path: Path) -> None:
    tc = FakeTc(fail={"del", "show"})
    ConditionMarker(tmp_path / "conditions").put(
        "att_dead", {"netem": "eth9", "netem_installed": True}
    )
    runner = _runner(tmp_path, set(), tc=tc)
    assert await runner.sweep() == 0
    assert set(runner.marker.load()) == {"att_dead"}


@pytest.mark.asyncio
async def test_probe_kinds(tmp_path: Path) -> None:
    off = ConditionSettings(enabled=False)
    assert await probe_kinds(off, has_gpu=True) == frozenset()
    base = await probe_kinds(ConditionSettings(), has_gpu=False)
    assert base == {ConditionKind.CPU_LOAD, ConditionKind.CACHE}
    netem = ConditionSettings(netem=True, netem_interface="eth9")
    assert ConditionKind.NETEM in await probe_kinds(
        netem, has_gpu=False, platform="linux", tc=FakeTc()
    )
    assert ConditionKind.NETEM not in await probe_kinds(
        netem, has_gpu=False, platform="linux", tc=FakeTc(fail={"show"})
    )
    assert ConditionKind.NETEM not in await probe_kinds(
        netem, has_gpu=False, platform="win32", tc=FakeTc()
    )


@pytest.mark.parametrize(
    "conditions",
    [
        {"netem": True},
        {"netem_interface": "lo"},
        {"netem_interface": "eth0; rm"},
        {"tc_command": []},
        {"tc_command": [""]},
        {"verify_window_s": 0.0},
    ],
)
def test_condition_settings_are_validated(
    tmp_path: Path, conditions: dict[str, Any]
) -> None:
    with pytest.raises((ConfigError, ValueError)):
        AgentSettings.load(
            data_dir=tmp_path,
            agent={"controller_address": "127.0.0.1:1", "conditions": conditions},
        )


def test_condition_settings_defaults(tmp_path: Path) -> None:
    settings = AgentSettings.load(
        data_dir=tmp_path, agent={"controller_address": "127.0.0.1:1"}
    )
    cfg = settings.agent.conditions
    assert cfg.enabled and not cfg.netem
    assert cfg.tc_command == ["sudo", "-n", "/usr/sbin/tc"]
