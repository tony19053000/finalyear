"""V0.17 G3 tester round 2: configuration re-read edge cases (G3-D2) and the
throttle's clock behaviour (G3-D1), against the REAL config loader."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.console.auth import MAX_FAILURES_PER_MINUTE, ConsoleAuth
from meshforge.agent.console.supervisor import FabricPhase, Supervisor
from meshforge.agent.settings import AgentSection, AgentSettings
from meshforge.common.errors import ExitCode

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _Client:
    def __init__(self) -> None:
        self.stopped = asyncio.Event()

    def request_stop(self) -> None:
        self.stopped.set()


def _enrol(settings: AgentSettings) -> None:
    ident = settings.identity_dir
    ident.mkdir(parents=True, exist_ok=True)
    for name in ("node.key", "node.json", "node.crt", "ca.crt"):
        (ident / name).write_text("x")


def _make(
    tmp_path: Path, cfg: Path
) -> tuple[Supervisor, list[AgentSettings], AgentSettings]:
    data = tmp_path / "data"
    base = AgentSettings(data_dir=data, agent=AgentSection(controller_address=""))
    seen: list[AgentSettings] = []

    async def serve(settings: AgentSettings, **kw: Any) -> _Client:
        seen.append(settings)
        client = _Client()
        kw["on_client"](client)
        await client.stopped.wait()
        return client

    sup = Supervisor(
        base,
        reload_settings=lambda: AgentSettings.load(cfg, data_dir=data),
        serve_fn=serve,  # type: ignore[arg-type]
        poll_s=0.05,
        install_signals=False,
    )
    return sup, seen, base


@pytest.mark.asyncio
async def test_real_toml_written_before_join_is_used(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    sup, seen, base = _make(tmp_path, cfg)
    task = asyncio.create_task(sup.run())
    await sup.wait_phase(FabricPhase.WAITING_FOR_JOIN)
    cfg.write_text('[agent]\ncontroller_address = "127.0.0.1:47321"\n')
    _enrol(base)
    await sup.wait_phase(FabricPhase.RUNNING)
    sup.request_shutdown()
    assert await task == ExitCode.OK
    assert seen[0].agent.controller_address == "127.0.0.1:47321"


@pytest.mark.asyncio
async def test_broken_toml_at_join_stops_with_reason_then_fix_and_restart_runs(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.toml"
    sup, seen, base = _make(tmp_path, cfg)
    task = asyncio.create_task(sup.run())
    await sup.wait_phase(FabricPhase.WAITING_FOR_JOIN)
    cfg.write_text("[agent\nthis is = = not toml")
    _enrol(base)
    await sup.wait_phase(FabricPhase.STOPPED)
    assert sup.stop_reason is not None
    assert sup.stop_reason.code == "config_reload"
    assert not seen, "fabric part must not start on a broken config"
    # the console (supervisor) is still alive and the owner fixes the file
    cfg.write_text('[agent]\ncontroller_address = "127.0.0.1:47322"\n')
    sup.request("restart")
    await sup.wait_phase(FabricPhase.RUNNING)
    assert sup.stop_reason is None
    sup.request_shutdown()
    assert await task == ExitCode.OK
    assert seen[0].agent.controller_address == "127.0.0.1:47322"


@pytest.mark.asyncio
async def test_invalid_value_at_join_is_config_reload_not_a_crash(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.toml"
    sup, seen, base = _make(tmp_path, cfg)
    task = asyncio.create_task(sup.run())
    await sup.wait_phase(FabricPhase.WAITING_FOR_JOIN)
    cfg.write_text("[agent]\nno_such_key = 1\ncontroller_address = 5\n")
    _enrol(base)
    await sup.wait_phase(FabricPhase.STOPPED)
    assert sup.stop_reason and sup.stop_reason.code == "config_reload"
    sup.request_shutdown()
    assert await task == ExitCode.OK
    assert not seen


@pytest.mark.asyncio
async def test_non_utf8_config_bytes_do_not_crash_the_supervisor(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.toml"
    sup, _seen, base = _make(tmp_path, cfg)
    task = asyncio.create_task(sup.run())
    await sup.wait_phase(FabricPhase.WAITING_FOR_JOIN)
    cfg.write_bytes(b"\xff\xfe\x00[agent]\n")
    _enrol(base)
    await asyncio.wait_for(sup.wait_phase(FabricPhase.STOPPED), 10)
    assert not task.done(), "supervisor died on undecodable config bytes"
    assert sup.stop_reason and sup.stop_reason.code == "config_reload"
    sup.request_shutdown()
    assert await task == ExitCode.OK


@pytest.mark.asyncio
async def test_config_path_that_is_a_directory_does_not_crash_the_supervisor(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.toml"
    sup, _seen, base = _make(tmp_path, cfg)
    task = asyncio.create_task(sup.run())
    await sup.wait_phase(FabricPhase.WAITING_FOR_JOIN)
    cfg.mkdir()
    _enrol(base)
    await asyncio.wait_for(sup.wait_phase(FabricPhase.STOPPED), 10)
    assert not task.done()
    sup.request_shutdown()
    assert await task == ExitCode.OK


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs a non-root POSIX user to make a file unreadable",
)
async def test_unreadable_config_does_not_crash_the_supervisor(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.toml"
    sup, _seen, base = _make(tmp_path, cfg)
    task = asyncio.create_task(sup.run())
    await sup.wait_phase(FabricPhase.WAITING_FOR_JOIN)
    cfg.write_text("[agent]\n")
    cfg.chmod(0)
    try:
        _enrol(base)
        await asyncio.wait_for(sup.wait_phase(FabricPhase.STOPPED), 10)
        assert not task.done()
        assert sup.stop_reason and sup.stop_reason.code == "config_reload"
    finally:
        cfg.chmod(0o600)
        sup.request_shutdown()
        assert await task == ExitCode.OK


@pytest.mark.asyncio
async def test_explicit_config_deleted_between_start_and_join_is_a_visible_stop(
    tmp_path: Path,
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[agent]\ncontroller_address = "127.0.0.1:1"\n')
    sup, seen, base = _make(tmp_path, cfg)
    task = asyncio.create_task(sup.run())
    await sup.wait_phase(FabricPhase.WAITING_FOR_JOIN)
    cfg.unlink()
    _enrol(base)
    await sup.wait_phase(FabricPhase.STOPPED)
    assert sup.stop_reason and sup.stop_reason.code == "config_reload"
    assert not seen
    sup.request_shutdown()
    assert await task == ExitCode.OK


@pytest.mark.asyncio
async def test_each_new_join_after_leave_rereads_the_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[agent]\ncontroller_address = "127.0.0.1:1111"\n')
    sup, seen, base = _make(tmp_path, cfg)
    task = asyncio.create_task(sup.run())
    await sup.wait_phase(FabricPhase.WAITING_FOR_JOIN)
    _enrol(base)
    await sup.wait_phase(FabricPhase.RUNNING)
    assert seen[0].agent.controller_address == "127.0.0.1:1111"
    sup.request("leave")
    await sup.wait_phase(FabricPhase.WAITING_FOR_JOIN)
    cfg.write_text('[agent]\ncontroller_address = "127.0.0.1:2222"\n')
    _enrol(base)
    await asyncio.wait_for(sup.wait_phase(FabricPhase.RUNNING), 15)
    assert seen[-1].agent.controller_address == "127.0.0.1:2222"
    sup.request_shutdown()
    assert await task == ExitCode.OK


# ------------------------------------------------------------- throttle clock


def test_throttle_counts_only_failures_and_window_resets() -> None:
    now = [100.0]
    auth = ConsoleAuth(
        port=1, session_ttl_s=60, launch_code_ttl_s=60, clock=lambda: now[0]
    )
    for _ in range(MAX_FAILURES_PER_MINUTE * 3):
        assert not auth.token_matches("nope")
        auth.throttled()  # the endpoints ask after every failed try
    assert auth.throttled()
    assert auth.token_matches(auth.token), "right token must pass while throttled"
    assert auth.redeem_launch_code(auth.new_launch_code())
    now[0] += 61
    assert not auth.throttled()


def test_successes_never_feed_the_throttle() -> None:
    auth = ConsoleAuth(port=1, session_ttl_s=60, launch_code_ttl_s=60)
    for _ in range(MAX_FAILURES_PER_MINUTE * 3):
        assert auth.token_matches(auth.token)
        assert auth.redeem_launch_code(auth.new_launch_code())
    assert not auth.throttled()


def test_empty_and_none_credentials_do_not_panic() -> None:
    auth = ConsoleAuth(port=1, session_ttl_s=60, launch_code_ttl_s=60)
    assert not auth.token_matches("")
    assert not auth.token_matches(None)
    assert not auth.redeem_launch_code("")
    assert not auth.redeem_launch_code(None)
    assert not auth.redeem_launch_code("é" * 50)  # non-ASCII must not raise
    assert not auth.token_matches("é" * 50)
