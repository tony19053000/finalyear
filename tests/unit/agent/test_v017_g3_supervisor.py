"""V0.17 G3: the supervisor - the console stays up, the fabric part comes and goes.

A fake ``serve`` stands in for the real one (the component test runs the real
one against a controller): it hands out a client whose ``request_stop`` ends
it, or raises what a test asks for.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.console.setup_state import mark_setup_complete, setup_complete
from meshforge.agent.console.supervisor import FabricPhase, Supervisor
from meshforge.agent.session.errors import NodeRevokedError
from meshforge.agent.settings import AgentSection, AgentSettings
from meshforge.common.errors import ConfigError, ExitCode

pytestmark = [pytest.mark.unit, pytest.mark.asyncio, pytest.mark.timeout(30)]


class FakeClient:
    def __init__(self) -> None:
        self.stopped = asyncio.Event()
        self.state = type("S", (), {"value": "ONLINE"})()
        self.controller_target = "127.0.0.1:47100"
        self.session_id = "sess"
        self.peer_endpoint = None

    def request_stop(self) -> None:
        self.stopped.set()


class FakeServe:
    """``serve`` stand-in. ``plan`` = what each start does: ``"run"`` (until
    stopped), an exception to raise after the client exists, or ``"hang"``
    (never makes a client: stuck in hardware discovery)."""

    def __init__(self, *plan: object) -> None:
        self.plan = list(plan)
        self.starts = 0
        self.settings_seen: list[AgentSettings] = []

    async def __call__(
        self,
        settings: AgentSettings,
        *,
        install_signals: bool = True,
        on_client: Callable[[Any], None] | None = None,
    ) -> Any:
        assert install_signals is False
        self.starts += 1
        self.settings_seen.append(settings)
        step = self.plan.pop(0) if self.plan else "run"
        if step == "hang":
            await asyncio.Event().wait()
        client = FakeClient()
        assert on_client is not None
        on_client(client)
        if isinstance(step, BaseException):
            await asyncio.sleep(0)
            raise step
        await client.stopped.wait()
        return client


def _settings(tmp_path: Path) -> AgentSettings:
    return AgentSettings(
        data_dir=tmp_path / "data",
        agent=AgentSection(controller_address="127.0.0.1:47100"),
    )


def _enrol(settings: AgentSettings) -> None:
    ident = settings.identity_dir
    ident.mkdir(parents=True, exist_ok=True)
    for name in ("node.key", "node.json", "node.crt", "ca.crt"):
        (ident / name).write_text("x")


async def _start(sup: Supervisor) -> asyncio.Task[ExitCode]:
    return asyncio.create_task(sup.run())


async def test_waits_for_the_join_then_starts_by_itself(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    serve = FakeServe()
    sup = Supervisor(settings, serve_fn=serve, poll_s=0.05, install_signals=False)
    task = await _start(sup)
    await sup.wait_phase(FabricPhase.WAITING_FOR_JOIN)
    await asyncio.sleep(0.2)
    assert serve.starts == 0
    _enrol(settings)  # e.g. `sudo meshforge-agent join` in another process
    await sup.wait_phase(FabricPhase.RUNNING)
    assert serve.starts == 1 and sup.client is not None
    sup.request_shutdown()
    assert await task == ExitCode.OK
    assert sup.client is None


async def test_restart_rereads_the_settings_in_process(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _enrol(settings)
    reloaded = _settings(tmp_path)
    serve = FakeServe()
    sup = Supervisor(
        settings,
        reload_settings=lambda: reloaded,
        serve_fn=serve,
        poll_s=0.05,
        install_signals=False,
    )
    task = await _start(sup)
    await sup.wait_phase(FabricPhase.RUNNING)
    first = sup.client
    sup.request("restart")
    for _ in range(200):
        if serve.starts == 2 and sup.phase is FabricPhase.RUNNING:
            break
        await asyncio.sleep(0.01)
    assert serve.starts == 2 and sup.client is not first
    assert first is not None and first.stopped.is_set()  # Goodbye path used
    assert serve.settings_seen[1] is reloaded
    sup.request_shutdown()
    assert await task == ExitCode.OK


async def test_a_broken_config_on_restart_keeps_the_console(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _enrol(settings)

    def broken() -> AgentSettings:
        raise ConfigError("[agent] bogus: extra inputs are not permitted")

    sup = Supervisor(
        settings, reload_settings=broken, serve_fn=FakeServe(), install_signals=False
    )
    task = await _start(sup)
    await sup.wait_phase(FabricPhase.RUNNING)
    sup.request("restart")
    await sup.wait_phase(FabricPhase.STOPPED)
    assert sup.stop_reason is not None and sup.stop_reason.code == "config_reload"
    assert "bogus" in sup.stop_reason.message
    assert not task.done()  # the process (and its console) stays up
    sup.request_shutdown()
    assert await task == ExitCode.OK


async def test_typed_stop_keeps_the_process_and_says_why(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _enrol(settings)
    revoked = NodeRevokedError("node_x", "this node was revoked by an admin")
    serve = FakeServe(revoked, "run")
    sup = Supervisor(settings, serve_fn=serve, poll_s=0.05, install_signals=False)
    task = await _start(sup)
    await sup.wait_phase(FabricPhase.STOPPED)
    assert sup.stop_reason is not None and sup.stop_reason.code == "revoked"
    await asyncio.sleep(0.2)
    assert serve.starts == 1  # no restart loop
    sup.request("restart")
    await sup.wait_phase(FabricPhase.RUNNING)
    assert serve.starts == 2 and sup.stop_reason is None
    sup.request_shutdown()
    assert await task == ExitCode.OK


async def test_config_error_from_serve_is_a_typed_stop(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _enrol(settings)
    sup = Supervisor(
        settings,
        serve_fn=FakeServe(ConfigError("node.crt is unusable")),
        install_signals=False,
    )
    task = await _start(sup)
    await sup.wait_phase(FabricPhase.STOPPED)
    assert sup.stop_reason is not None and sup.stop_reason.code == "config"
    sup.request_shutdown()
    assert await task == ExitCode.OK


async def test_an_unexpected_crash_ends_the_process(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _enrol(settings)
    sup = Supervisor(
        settings, serve_fn=FakeServe(RuntimeError("bug")), install_signals=False
    )
    assert await asyncio.wait_for(sup.run(), 10) == ExitCode.FAILURE


async def test_leave_resets_the_identity_and_the_setup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _enrol(settings)
    mark_setup_complete(settings)
    (settings.data_dir / "node_name").write_text("kept")
    sup = Supervisor(settings, serve_fn=FakeServe(), poll_s=0.05, install_signals=False)
    task = await _start(sup)
    await sup.wait_phase(FabricPhase.RUNNING)
    client = sup.client
    sup.request("leave")
    await sup.wait_phase(FabricPhase.WAITING_FOR_JOIN)
    assert client is not None and client.stopped.is_set()
    assert not (settings.identity_dir / "node.key").exists()
    assert not (settings.identity_dir / "node.crt").exists()
    assert not setup_complete(settings)
    assert (settings.data_dir / "node_name").read_text() == "kept"
    sup.request_shutdown()
    assert await task == ExitCode.OK


async def test_shutdown_while_still_starting(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _enrol(settings)
    serve = FakeServe("hang")
    sup = Supervisor(settings, serve_fn=serve, install_signals=False)
    import meshforge.agent.console.supervisor as mod

    mod_stop_wait = mod.STOP_WAIT_S
    mod.STOP_WAIT_S = 0.3  # type: ignore[misc]
    try:
        task = await _start(sup)
        await sup.wait_phase(FabricPhase.STARTING)
        sup.request_shutdown()
        assert await asyncio.wait_for(task, 10) == ExitCode.OK
    finally:
        mod.STOP_WAIT_S = mod_stop_wait  # type: ignore[misc]


async def test_restart_process_exits_ok_for_the_service_manager(tmp_path: Path) -> None:
    sup = Supervisor(_settings(tmp_path), serve_fn=FakeServe(), install_signals=False)
    task = await _start(sup)
    await sup.wait_phase(FabricPhase.WAITING_FOR_JOIN)
    sup.restart_process()
    assert await task == ExitCode.OK


async def test_unknown_command_is_refused(tmp_path: Path) -> None:
    sup = Supervisor(_settings(tmp_path), serve_fn=FakeServe(), install_signals=False)
    with pytest.raises(ValueError, match="unknown"):
        sup.request("format-disk")
