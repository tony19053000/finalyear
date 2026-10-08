"""Shared helpers for the V0.3 G3 agent component tests (imported by
``tests/component/agent/conftest.py`` and its test modules as ``mf_agent_harness``).

Kept out of ``conftest.py`` on purpose: test modules that ``from conftest import``
break as soon as two test directories with a ``conftest.py`` are collected in one
run (the first one wins the ``conftest`` module name).

Timing comes from ``mf_controller_harness`` (heartbeat 0.2 s -> SUSPECT at 0.25 s,
OFFLINE at 0.6 s); agent backoff base 0.1 s / cap 2 s so a reconnect never waits long.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mf_controller_harness import FAST_LIVENESS, make_settings
from mf_fabric import enroll_agent

from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.settings import AgentSection, AgentSettings
from meshforge.controller.settings import ControllerSettings

if TYPE_CHECKING:
    from meshforge.controller.store.settings import DatabaseSettings

__all__ = [
    "AGENT_HELLO_TIMEOUT_S",
    "AGENT_RECONNECT",
    "FAST_LIVENESS",
    "AgentFactory",
    "RunningAgent",
    "make_agent_settings",
    "make_controller_settings",
]

AGENT_RECONNECT = {"base_s": 0.1, "cap_s": 2.0, "reset_after_s": 30.0}
AGENT_HELLO_TIMEOUT_S = 2.0
"""Longer than the controller's 0.5 s Hello deadline: admission does DB writes."""


def make_controller_settings(
    database: DatabaseSettings, **controller_overrides: Any
) -> ControllerSettings:
    """The controller test settings (fast liveness) with mDNS advertising off
    unless a test turns it on (G3 tests it once, on purpose)."""
    overrides: dict[str, Any] = {"discovery": {"advertise": False}}
    overrides.update(controller_overrides)
    return make_settings(database, **overrides)


def make_agent_settings(
    data_dir: Path,
    controller_address: str,
    *,
    enroll: bool = True,
    **agent_overrides: Any,
) -> AgentSettings:
    """Agent test settings. ``enroll`` (default): the agent's identity is
    created and enrolled in the test fabric (``mf_fabric``) without the join
    dance - a V0.7 G3 agent in ``mtls`` mode refuses to start otherwise."""
    agent: dict[str, Any] = {
        "controller_address": controller_address,
        "reconnect": dict(AGENT_RECONNECT),
        "hello_timeout_s": AGENT_HELLO_TIMEOUT_S,
        "identity_lock_timeout_s": 0.5,
    }
    agent.update(agent_overrides)
    # Keyword arguments beat env/TOML, so a developer's config.toml cannot leak in.
    settings = AgentSettings(data_dir=data_dir, agent=AgentSection(**agent))
    if enroll:
        enroll_agent(settings)
    return settings


class RunningAgent:
    """An ``AgentSessionClient`` plus the task running it."""

    def __init__(self, client: AgentSessionClient) -> None:
        self.client = client
        self.task: asyncio.Task[None] = asyncio.create_task(
            client.run(), name=f"agent-under-test:{client.node_id}"
        )

    async def stop(self, timeout_s: float = 10.0) -> BaseException | None:
        """Request a stop and wait for ``run()``; returns its exception, if any."""
        self.client.request_stop()
        return await self.outcome(timeout_s)

    async def outcome(self, timeout_s: float = 10.0) -> BaseException | None:
        await asyncio.wait({self.task}, timeout=timeout_s)
        if not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            raise AssertionError("agent run() did not finish in time")
        if self.task.cancelled():
            return None
        return self.task.exception()


AgentFactory = Callable[..., Coroutine[Any, Any, RunningAgent]]
