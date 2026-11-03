"""V0.4 G4 (D45/D51): what an agent advertises for execution, and when it warns."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
import structlog

from meshforge.agent.execution import offer as offer_module
from meshforge.agent.execution.offer import build_execution
from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.agent.settings import AgentSettings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


class FakeDocker:
    name = "docker"

    def __init__(self, found: RuntimeAvailability | None, *, hang: bool = False):
        self.found = found
        self.hang = hang
        self.probed = 0
        self.config: Any = None

    async def probe(self) -> RuntimeAvailability:
        self.probed += 1
        if self.hang:
            await asyncio.Event().wait()
        assert self.found is not None
        return self.found


def _settings(tmp_path: Path, **execution: Any) -> AgentSettings:
    return AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={"controller_address": "127.0.0.1:1", "execution": execution},
    )


def _factory(fake: FakeDocker) -> Any:
    def make(config: Any) -> FakeDocker:
        fake.config = config
        return fake

    return make


@pytest.mark.asyncio
async def test_jobs_off_at_start_still_probes_and_advertises_what_it_can_run(
    tmp_path: Path,
) -> None:
    """V1.0 G8 F5 (D428): CAN (capabilities) is separate from ALLOWS (the live
    policy) - an agent started with accept_jobs off advertises its runtimes,
    so turning Run jobs on later needs no restart."""
    fake = FakeDocker(RuntimeAvailability("docker", True, sandboxed=True))
    setup = await build_execution(
        _settings(tmp_path),
        node_id="node_x",
        host_has_nvidia_gpu=True,
        docker_factory=_factory(fake),
    )
    assert fake.probed == 1
    assert {"exec", "exec-docker"} <= set(setup.offer.capabilities)
    assert "docker" in setup.runtimes
    assert setup.enabled


@pytest.mark.asyncio
async def test_docker_only_offers_exec_and_exec_docker_with_a_per_node_label(
    tmp_path: Path,
) -> None:
    fake = FakeDocker(RuntimeAvailability("docker", True, sandboxed=True))
    setup = await build_execution(
        _settings(tmp_path, enabled=True),
        node_id="node_abc",
        host_has_nvidia_gpu=False,
        docker_factory=_factory(fake),
    )
    assert {"exec", "exec-docker"} <= set(setup.offer.capabilities)
    assert "exec-gpu" not in setup.offer.capabilities
    assert "docker" in setup.runtimes
    assert fake.config.agent_label == "node_abc"  # D63-6: never shared across agents


@pytest.mark.asyncio
@pytest.mark.extras("agent")
async def test_unsandboxed_runtimes_are_built_without_the_opt_in(
    tmp_path: Path,
) -> None:
    """V1.0 G8 F5 (D428): the subprocess/venv runtimes exist whatever
    allow_unsandboxed says at start; the live policy decides per attempt."""
    missing = FakeDocker(
        RuntimeAvailability("docker", False, reason="docker_sdk_missing")
    )
    for allow in (False, True):
        setup = await build_execution(
            _settings(tmp_path, enabled=True, allow_unsandboxed=allow),
            node_id="node_x",
            host_has_nvidia_gpu=True,
            docker_factory=_factory(missing),
        )
        # V0.5 G2: the venv runtime (uv wheel, agent extra) is unsandboxed too
        assert setup.offer.capabilities == (
            "exec",
            "exec-gpu",
            "exec-subprocess",
            "exec-venv",
        )
        assert set(setup.runtimes) == {"subprocess", "venv"}


@pytest.mark.asyncio
async def test_enabled_with_mdns_discovery_warns_unpinned(tmp_path: Path) -> None:
    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={"controller_address": "", "execution": {"enabled": True}},
    )
    fake = FakeDocker(RuntimeAvailability("docker", True, sandboxed=True))
    with structlog.testing.capture_logs() as logs:
        await build_execution(
            settings,
            node_id="node_x",
            host_has_nvidia_gpu=False,
            docker_factory=_factory(fake),
        )
    assert [e for e in logs if e["event"] == "agent.execution.unpinned_controller"]


@pytest.mark.asyncio
async def test_pinned_controller_does_not_warn(tmp_path: Path) -> None:
    fake = FakeDocker(RuntimeAvailability("docker", True, sandboxed=True))
    with structlog.testing.capture_logs() as logs:
        await build_execution(
            _settings(tmp_path, enabled=True),
            node_id="node_x",
            host_has_nvidia_gpu=False,
            docker_factory=_factory(fake),
        )
    assert not [e for e in logs if e["event"] == "agent.execution.unpinned_controller"]


@pytest.mark.asyncio
async def test_a_wedged_docker_probe_is_bounded() -> None:
    fake = FakeDocker(None, hang=True)
    found = await offer_module._probe(fake, 0.02)  # type: ignore[arg-type]
    assert not found.available
    assert found.reason == "docker_probe_timeout"
