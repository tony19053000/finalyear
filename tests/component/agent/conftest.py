"""Fixtures for the V0.3 G3 agent component tests.

Real everything: PostgreSQL from ``pg_settings``, the real
:class:`~meshforge.controller.server.ControllerRuntime` on ephemeral loopback ports
(mDNS advertisement off unless a test turns it on), and the real
:class:`~meshforge.agent.session.client.AgentSessionClient` running as a task in the
test's event loop against a real identity directory under ``tmp_path``. Tests may
import both ``controller`` and ``agent``; ``src/`` never does.

Helpers (settings builders, ``RunningAgent``) live in
``tests/_support/mf_agent_harness.py`` - import them from there, never from
``conftest``. Every test binds loopback only and asserts nothing about this
machine's hostname or addresses.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("fastapi", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from mf_agent_harness import (
    AgentFactory,
    RunningAgent,
    make_agent_settings,
    make_controller_settings,
)

from meshforge.agent.identity import NodeIdentity, load_or_create
from meshforge.agent.session.client import AgentSessionClient
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine

if TYPE_CHECKING:
    from meshforge.controller.store.settings import DatabaseSettings


@pytest.fixture(autouse=True)
def _no_meshforge_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(pg_settings)


@pytest_asyncio.fixture
async def runtime(settings: ControllerSettings) -> AsyncIterator[ControllerRuntime]:
    async with ControllerRuntime.start(settings) as rt:
        yield rt


@pytest_asyncio.fixture
async def registry(pg_settings: DatabaseSettings) -> AsyncIterator[Registry]:
    """An independent read/write path to the same database (not the runtime's)."""
    engine = create_engine(pg_settings)
    try:
        yield Registry(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def grpc_target(runtime: ControllerRuntime) -> str:
    return f"127.0.0.1:{runtime.grpc_port}"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "agent-data"


@pytest.fixture
def identity(data_dir: Path) -> NodeIdentity:
    return load_or_create(data_dir / "identity")


@pytest_asyncio.fixture
async def agents(data_dir: Path) -> AsyncIterator[AgentFactory]:
    """Factory: ``await agents(target, identity=..., **agent_overrides)``; every
    agent is stopped at teardown."""
    made: list[RunningAgent] = []

    async def factory(
        controller_address: str,
        *,
        identity: NodeIdentity | None = None,
        directory: Path | None = None,
        **overrides: Any,
    ) -> RunningAgent:
        directory = directory or data_dir
        settings = make_agent_settings(directory, controller_address, **overrides)
        ident = identity or load_or_create(settings.identity_dir)
        client = AgentSessionClient(settings, ident)
        running = RunningAgent(client)
        made.append(running)
        return running

    yield factory
    for running in made:
        if not running.task.done():
            running.client.request_stop()
    for running in made:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(running.task, 10.0)
        if not running.task.done():
            running.task.cancel()
            await asyncio.gather(running.task, return_exceptions=True)
