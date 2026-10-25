"""V0.17 G3 tester: configuration edited while the agent waits for a join."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.console.supervisor import FabricPhase, Supervisor
from meshforge.agent.settings import AgentSection, AgentSettings
from meshforge.common.errors import ExitCode

pytestmark = [pytest.mark.unit, pytest.mark.asyncio, pytest.mark.timeout(30)]


class _Client:
    def __init__(self) -> None:
        self.stopped = asyncio.Event()

    def request_stop(self) -> None:
        self.stopped.set()


async def test_config_written_before_the_join_is_used_when_the_fabric_part_starts(
    tmp_path: Path,
) -> None:
    old = AgentSettings(
        data_dir=tmp_path / "data", agent=AgentSection(controller_address="")
    )
    new = AgentSettings(
        data_dir=tmp_path / "data",
        agent=AgentSection(controller_address="127.0.0.1:47199"),
    )
    seen: list[AgentSettings] = []

    async def serve(settings: AgentSettings, **kw: Any) -> _Client:
        seen.append(settings)
        client = _Client()
        kw["on_client"](client)
        await client.stopped.wait()
        return client

    sup = Supervisor(
        old,
        reload_settings=lambda: new,
        serve_fn=serve,  # type: ignore[arg-type]
        poll_s=0.05,
        install_signals=False,
    )
    task = asyncio.create_task(sup.run())
    await sup.wait_phase(FabricPhase.WAITING_FOR_JOIN)
    ident = old.identity_dir
    ident.mkdir(parents=True, exist_ok=True)
    for name in ("node.key", "node.json", "node.crt", "ca.crt"):
        (ident / name).write_text("x")
    await sup.wait_phase(FabricPhase.RUNNING)
    sup.request_shutdown()
    assert await task == ExitCode.OK
    assert seen[0].agent.controller_address == "127.0.0.1:47199"
