"""Fixtures for the V0.3 G2 controller component tests.

Real everything: PostgreSQL from ``pg_settings`` (fresh database per test), the real
:class:`~meshforge.controller.server.ControllerRuntime` (grpc.aio server + uvicorn in
this test's event loop) on ephemeral loopback ports, and a hand-rolled agent built
on the generated ``AgentControlStub`` - there is no agent runtime yet (that is G3).

Timing is short (heartbeat 0.2 s -> SUSPECT at 0.25 s, OFFLINE at 0.6 s) so the
whole directory stays well under a minute.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("fastapi", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

import grpc
from mf_session_client import FakeAgent

from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import (
    ControllerSection,
    ControllerSettings,
    LivenessSettings,
)
from meshforge.controller.store.engine import create_engine

if TYPE_CHECKING:
    from meshforge.controller.store.settings import DatabaseSettings

# heartbeat 0.2 s, SUSPECT at 0.25 s, OFFLINE at 0.6 s (x3)
FAST_LIVENESS = LivenessSettings(
    heartbeat_interval_s=0.2, miss_allowance_s=0.05, lease_multiplier=3
)
HELLO_TIMEOUT_S = 0.5
SHUTDOWN_DRAIN_S = 2.0


def controller_section(**overrides: Any) -> ControllerSection:
    base: dict[str, Any] = {
        "grpc_bind": "127.0.0.1:0",
        "http_bind": "127.0.0.1:0",
        "liveness": FAST_LIVENESS,
        "hello_timeout_s": HELLO_TIMEOUT_S,
        "shutdown_drain_s": SHUTDOWN_DRAIN_S,
    }
    base.update(overrides)
    return ControllerSection(**base)


def make_settings(
    database: DatabaseSettings, **controller_overrides: Any
) -> ControllerSettings:
    # Keyword arguments beat env/TOML, so a developer's config.toml cannot leak in.
    return ControllerSettings(
        controller=controller_section(**controller_overrides), database=database
    )


@pytest.fixture(autouse=True)
def _no_meshforge_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import os

    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_settings(pg_settings)


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


@pytest_asyncio.fixture
async def channel(grpc_target: str) -> AsyncIterator[grpc.aio.Channel]:
    async with grpc.aio.insecure_channel(grpc_target) as ch:
        yield ch


@pytest_asyncio.fixture
async def agents(channel: grpc.aio.Channel) -> AsyncIterator[Any]:
    """Factory: ``agents(node_id=..., epoch=...)``; every agent is closed at teardown."""
    made: list[FakeAgent] = []

    def factory(**kwargs: Any) -> FakeAgent:
        agent = FakeAgent(channel, **kwargs)
        made.append(agent)
        return agent

    yield factory
    for agent in made:
        await agent.close()
