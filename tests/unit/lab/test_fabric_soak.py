"""``fabric_check soak``: transitions + session end reasons are recorded, OFFLINE
events are attributed to the session end that caused them, and an interrupted soak
(Ctrl-C, or a real SIGKILL of the process) resumes from its checkpoint."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import fabric_check as cli
from _fabric import checkpoint, soak
from _fabric.api import FabricApi
from _fabric.testing import FakeClock, ScriptedOperator, StubController, node_doc

A = "node_01JAAAAAAAAAAAAAAAAAAAAAAA"
B = "node_01JBBBBBBBBBBBBBBBBBBBBBBB"
LAB = Path(cli.__file__).resolve().parent


def _fabric(stub: StubController) -> None:
    stub.nodes[A] = node_doc(A, "lap1", session_id="sa1")
    stub.nodes[B] = node_doc(B, "lap2", session_id="sb1")
    stub.add_session(
        "old", A, ended_at="2026-09-23T10:00:00+00:00", end_reason="agent_closed"
    )
    stub.add_session("sa1", A)
    stub.add_session("sb1", B)


def _wall(clock: FakeClock, dt: float) -> str:
    return (clock.wall() + timedelta(seconds=dt)).isoformat()


def _blip_and_loss(stub: StubController, clock: FakeClock) -> None:
    """A: 2 s Wi-Fi blip (SUSPECT, back ONLINE, stream_lost). B: lease expiry -> OFFLINE."""
    blip_end = _wall(clock, 10.5)
    loss_end = _wall(clock, 20.0)

    def a_blip() -> None:
        stub.set_node(A, state="suspect", connected=False)
        stub.end_session("sa1", ended_at=blip_end, reason="stream_lost")

    def a_back() -> None:
        stub.set_node(A, state="online", connected=True, last_session_id="sa2")
        stub.add_session("sa2", A, epoch=2)

    def b_lost() -> None:
        stub.set_node(B, state="offline", connected=False)
        stub.end_session("sb1", ended_at=loss_end, reason="lease_expired")

    clock.after(10.0, a_blip)
    clock.after(12.0, a_back)
    clock.after(20.0, b_lost)


def _cfg(hours: float = 60 / 3600) -> soak.SoakConfig:
    return soak.SoakConfig(
        hours=hours, link="ethernet", poll_s=1.0, sessions_every_s=5.0
    )


def test_transitions_and_reasons_are_recorded(stub_controller: StubController) -> None:
    _fabric(stub_controller)
    clock = FakeClock()
    _blip_and_loss(stub_controller, clock)
    saves: list[dict[str, Any]] = []
    op = ScriptedOperator(clock, [])
    state = soak.new_state(_cfg(), clock.wall())
    runner = soak.Soak(
        FabricApi(stub_controller.url),
        clock,
        op,
        _cfg(),
        state,
        lambda s: saves.append(json.loads(json.dumps(s))),
    )
    runner.run()
    summary = soak.summarize(state)
    assert summary["done"] is True
    assert summary["observed_h"] >= 60 / 3600
    assert summary["offline_events"] == 1
    assert summary["offline_by_reason"] == {"lease_expired": 1}
    assert summary["offline_by_node"] == {"lap2": 1}
    assert summary["suspect_events"] == 1  # the blip is a SUSPECT, never an OFFLINE
    # the session that ended BEFORE the soak is not counted
    assert summary["session_ends_by_reason"] == {"lease_expired": 1, "stream_lost": 1}
    assert summary["outages"] == 0
    # saved at once on each event, not only every 5 minutes
    assert len(saves) >= 4


def test_a_controller_outage_is_recorded_with_its_duration(
    stub_controller: StubController,
) -> None:
    _fabric(stub_controller)
    clock = FakeClock()
    clock.after(5.0, stub_controller.stop)
    clock.after(9.0, stub_controller.start)
    state = soak.new_state(_cfg(), clock.wall())
    soak.Soak(
        FabricApi(stub_controller.url),
        clock,
        ScriptedOperator(clock, []),
        _cfg(),
        state,
        lambda _s: None,
    ).run()
    (outage,) = state["outages"]
    assert 3.9 <= outage["duration_s"] <= 4.1
    assert outage["to"] is not None


def test_ctrl_c_saves_and_a_rerun_resumes(
    stub_controller: StubController, tmp_path: Path
) -> None:
    _fabric(stub_controller)
    ckpt = tmp_path / "soak.json"
    argv = [
        "--controller", stub_controller.url, "--reports-dir", str(tmp_path), "--name", "L1",
        "soak", "--hours", str(60 / 3600), "--link", "ethernet", "--checkpoint", str(ckpt),
    ]  # fmt: skip
    clock = FakeClock()
    _blip_and_loss(stub_controller, clock)

    def ctrl_c() -> None:
        raise KeyboardInterrupt

    clock.after(25.0, ctrl_c)
    assert (
        cli.main(argv, clock=clock, operator=ScriptedOperator(clock, []))
        == cli.EXIT_INTERRUPTED
    )
    saved = checkpoint.load(ckpt, soak.KIND)
    assert saved is not None and not saved["done"]
    assert 24.0 <= saved["elapsed_s"] <= 25.5
    assert len(saved["transitions"]) >= 3
    # while the tool is down: B comes back, and A's session ends (found via /v1/sessions)
    stub_controller.set_node(B, state="online", connected=True)
    stub_controller.end_session(
        "sa2", ended_at="2026-09-24T10:01:00+00:00", reason="stream_lost"
    )
    later = FakeClock(start_wall=datetime(2026, 9, 24, 11, 0, tzinfo=UTC))
    code = cli.main(argv, clock=later, operator=ScriptedOperator(later, []))
    assert code == cli.EXIT_INCOMPLETE  # finished, with an OFFLINE event on record
    state = checkpoint.load(ckpt, soak.KIND)
    assert state is not None and state["done"]
    assert len(state["runs"]) == 2 and state["runs"][1]["resumed"] is True
    (gap,) = state["gaps"]
    assert gap["to"].startswith("2026-09-24T11:00")
    gap_changes = [t for t in state["transitions"] if t["during_gap"]]
    assert [(t["hostname"], t["new"]) for t in gap_changes] == [
        ("lap2", "online"),
        ("lap2", True),
    ]
    assert soak.summarize(state)["session_ends_by_reason"] == {
        "lease_expired": 1,
        "stream_lost": 2,
    }
    # total observed time is the target, the gap is not counted
    assert 60.0 <= state["elapsed_s"] <= 61.5
    report = (tmp_path / "2026-09-24-V0.3-fabric-soak.md").read_text(encoding="utf-8")
    assert "lease_expired" in report and "noticed after a tool gap" not in report


def test_a_soak_process_killed_for_real_resumes_from_its_checkpoint(
    stub_controller: StubController, tmp_path: Path
) -> None:
    """SIGKILL (TerminateProcess on Windows) - no handler runs; the checkpoint survives."""
    _fabric(stub_controller)
    ckpt = tmp_path / "soak.json"
    common = [
        "--controller",
        stub_controller.url,
        "--reports-dir",
        str(tmp_path),
        "--name",
        "L1",
    ]
    sub = ["soak", "--hours", str(3600 / 3600), "--link", "ethernet", "--checkpoint", str(ckpt),
           "--poll", "0.05"]  # fmt: skip
    proc = subprocess.Popen(
        [sys.executable, str(LAB / "fabric_check.py"), *common, *sub],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        deadline = time.monotonic() + 20.0
        while not ckpt.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ckpt.exists(), "the soak never wrote its first checkpoint"
        stub_controller.set_node(
            B, state="offline", connected=False
        )  # recorded + saved at once
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            saved = checkpoint.load(ckpt, soak.KIND)
            if saved and saved["transitions"]:
                break
            time.sleep(0.05)
        proc.kill()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    saved = checkpoint.load(ckpt, soak.KIND)
    assert saved is not None and not saved["done"]
    assert [
        (t["hostname"], t["new"]) for t in saved["transitions"] if t["field"] == "state"
    ] == [("lap2", "offline")]
    # resume in-process with a fake clock and let it finish the (1 h) target quickly
    later = FakeClock(start_wall=datetime(2026, 9, 24, 12, 0, tzinfo=UTC))
    argv = [*common, *sub[:-2], "--poll", "30"]
    assert (
        cli.main(argv, clock=later, operator=ScriptedOperator(later, []))
        == cli.EXIT_INCOMPLETE
    )
    state = checkpoint.load(ckpt, soak.KIND)
    assert state is not None and state["done"] and len(state["gaps"]) == 1
    assert soak.summarize(state)["offline_events"] == 1


def test_a_checkpoint_for_another_soak_is_refused(
    stub_controller: StubController, tmp_path: Path
) -> None:
    _fabric(stub_controller)
    ckpt = tmp_path / "soak.json"
    checkpoint.save(
        ckpt,
        soak.KIND,
        soak.new_state(_cfg(hours=24.0), datetime(2026, 9, 24, tzinfo=UTC)),
    )
    argv = ["--controller", stub_controller.url, "--reports-dir", str(tmp_path),
            "soak", "--hours", "12", "--link", "ethernet", "--checkpoint", str(ckpt)]  # fmt: skip
    clock = FakeClock()
    assert (
        cli.main(argv, clock=clock, operator=ScriptedOperator(clock, []))
        == cli.EXIT_USAGE
    )


def test_a_corrupt_checkpoint_is_never_silently_overwritten(
    stub_controller: StubController, tmp_path: Path
) -> None:
    ckpt = tmp_path / "soak.json"
    ckpt.write_text("{not json", encoding="utf-8")
    argv = ["--controller", stub_controller.url, "--reports-dir", str(tmp_path),
            "soak", "--hours", "1", "--link", "ethernet", "--checkpoint", str(ckpt)]  # fmt: skip
    clock = FakeClock()
    assert (
        cli.main(argv, clock=clock, operator=ScriptedOperator(clock, []))
        == cli.EXIT_WRITE
    )
    assert ckpt.read_text(encoding="utf-8") == "{not json"


def test_offline_attribution_window() -> None:
    ends = [
        {
            "node_id": A,
            "ended_at": "2026-09-24T10:00:00+00:00",
            "end_reason": "lease_expired",
        },
        {
            "node_id": B,
            "ended_at": "2026-09-24T10:00:01+00:00",
            "end_reason": "agent_closed",
        },
    ]
    near = {"node_id": A, "at": "2026-09-24T10:00:02+00:00"}
    far = {"node_id": A, "at": "2026-09-24T10:05:00+00:00"}
    assert soak.attribute(near, ends) == "lease_expired"
    assert soak.attribute(far, ends) == "unknown"
    assert soak.attribute({"node_id": A, "at": "garbage"}, ends) == "unknown"
