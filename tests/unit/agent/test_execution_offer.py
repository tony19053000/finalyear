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
async def test_disabled_advertises_nothing_and_probes_nothing(tmp_path: Path) -> None:
    fake = FakeDocker(RuntimeAvailability("docker", True, sandboxed=True))
    setup = await build_execution(
        _settings(tmp_path),
        node_id="node_x",
        host_has_nvidia_gpu=True,
        docker_factory=_factory(fake),
    )
    assert setup.offer.capabilities == ()
    assert setup.runtimes == {}
    assert fake.probed == 0


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
    assert setup.offer.capabilities == ("exec", "exec-docker")
    assert set(setup.runtimes) == {"docker"}
    assert fake.config.agent_label == "node_abc"  # D63-6: never shared across agents


@pytest.mark.asyncio
async def test_unsandboxed_needs_its_own_opt_in(tmp_path: Path) -> None:
    missing = FakeDocker(
        RuntimeAvailability("docker", False, reason="docker_sdk_missing")
    )
    without = await build_execution(
        _settings(tmp_path, enabled=True),
        node_id="node_x",
        host_has_nvidia_gpu=False,
        docker_factory=_factory(missing),
    )
    assert without.offer.capabilities == ()
    with_opt_in = await build_execution(
        _settings(tmp_path, enabled=True, allow_unsandboxed=True),
        node_id="node_x",
        host_has_nvidia_gpu=True,
        docker_factory=_factory(missing),
    )
    # V0.5 G2: the venv runtime (uv wheel, agent extra) is unsandboxed too
    assert with_opt_in.offer.capabilities == (
        "exec",
        "exec-gpu",
        "exec-subprocess",
        "exec-venv",
    )
    assert set(with_opt_in.runtimes) == {"subprocess", "venv"}


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
