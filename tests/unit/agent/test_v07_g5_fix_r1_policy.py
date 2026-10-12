"""V0.7 G5 fix round 1 (D160): T-8 the runtimes are probed from the effective
policy at start; T-9 ``policy set`` names every change that needs a restart;
T-6 a ``chmod``/``chown`` of the live file is noticed without a content change.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import structlog
from typer.testing import CliRunner

from meshforge.agent.execution.offer import build_execution
from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.agent.main import app
from meshforge.agent.policy import LivePolicy, startup_policy, write_overlay
from meshforge.agent.settings import AgentSettings, PolicySettings
from meshforge.common import owner_policy as op
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit
runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("MESHFORGE_DATA_DIR", str(tmp_path / "data"))


def _settings(tmp_path: Path, **policy: Any) -> AgentSettings:
    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={"controller_address": "127.0.0.1:1", "policy": policy},
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings


class _NoDocker:
    name = "docker"

    def __init__(self) -> None:
        self.probed = 0

    async def probe(self) -> RuntimeAvailability:
        self.probed += 1
        return RuntimeAvailability("docker", False, reason="docker_sdk_missing")


# ------------------------------------------------------------------ T-8


def test_startup_policy_is_the_configuration_plus_the_live_file(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, accept_jobs=False, allow_unsandboxed=False)
    assert startup_policy(settings) == settings.agent.policy  # no file
    write_overlay(
        settings.data_dir / "policy.toml",
        {"accept_jobs": True, "allow_unsandboxed": True},
    )
    effective = startup_policy(settings)
    assert effective.accept_jobs and effective.allow_unsandboxed


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize("how", ["world_writable", "invalid_value", "garbage"])
def test_a_broken_live_file_at_start_probes_from_the_configuration(
    tmp_path: Path, how: str
) -> None:
    settings = _settings(tmp_path, accept_jobs=False)
    path = settings.data_dir / "policy.toml"
    if how == "world_writable":
        write_overlay(path, {"accept_jobs": True})
        path.chmod(0o666)
    elif how == "invalid_value":
        path.write_text("accept_jobs = true\nmax_cpu_percent = 500\n")
        path.chmod(0o600)
    else:
        path.write_bytes(b"\x00\xff")
        path.chmod(0o600)
    with structlog.testing.capture_logs() as logs:
        assert startup_policy(settings) == settings.agent.policy
    assert any(e["event"] == "agent.policy.invalid_at_start" for e in logs)
    # and the live policy fails the same file closed
    live = LivePolicy(
        settings.agent.policy, path, gpu_count=0, max_concurrent_attempts=4
    )
    assert live.policy == op.DENY_ALL


@pytest.mark.asyncio
async def test_a_restart_after_policy_set_probes_what_the_live_file_enables(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, accept_jobs=False, allow_unsandboxed=False)
    write_overlay(
        settings.data_dir / "policy.toml",
        {"accept_jobs": True, "allow_unsandboxed": True},
    )
    docker = _NoDocker()
    setup = await build_execution(
        settings,
        node_id="node_x",
        host_has_nvidia_gpu=False,
        docker_factory=lambda _c: docker,  # type: ignore[arg-type,return-value]
    )
    assert docker.probed == 1
    assert setup.enabled
    assert {"subprocess", "venv"} <= set(setup.runtimes)


@pytest.mark.asyncio
async def test_a_live_file_that_turns_jobs_off_probes_nothing_at_start(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, accept_jobs=True, allow_unsandboxed=True)
    write_overlay(settings.data_dir / "policy.toml", {"accept_jobs": False})
    docker = _NoDocker()
    setup = await build_execution(
        settings,
        node_id="node_x",
        host_has_nvidia_gpu=False,
        docker_factory=lambda _c: docker,  # type: ignore[arg-type,return-value]
    )
    assert docker.probed == 0
    assert not setup.enabled and setup.runtimes == {}


@pytest.mark.asyncio
async def test_a_live_file_that_turns_unsandboxed_off_builds_no_subprocess(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, accept_jobs=True, allow_unsandboxed=True)
    write_overlay(settings.data_dir / "policy.toml", {"allow_unsandboxed": False})
    setup = await build_execution(
        settings,
        node_id="node_x",
        host_has_nvidia_gpu=False,
        docker_factory=lambda _c: _NoDocker(),  # type: ignore[arg-type,return-value]
    )
    assert "subprocess" not in setup.runtimes and "venv" not in setup.runtimes


# ------------------------------------------------------------------ T-9


def _set(*items: str) -> str:
    result = runner.invoke(app, ["policy", "set", *items], prog_name="meshforge-agent")
    assert result.exit_code == ExitCode.OK, result.output
    return result.stdout


@pytest.mark.parametrize("key", ["accept_jobs", "allow_unsandboxed"])
def test_turning_a_restart_key_on_names_that_key(key: str) -> None:
    out = _set(f"{key}=true")
    assert "restart" in out
    assert f"{key} = false" in out


def test_both_keys_on_at_once_give_both_notes() -> None:
    out = _set("accept_jobs=true", "allow_unsandboxed=true")
    assert "accept_jobs = false" in out
    assert "allow_unsandboxed = false" in out


def test_no_note_when_the_key_was_already_on_or_is_turned_off() -> None:
    _set("allow_unsandboxed=true")
    assert "restart" not in _set("allow_unsandboxed=true")
    assert "restart" not in _set("allow_unsandboxed=false")
    assert "restart" not in _set("max_cpu_percent=50")


def test_a_note_after_an_invalid_old_file_is_given_to_be_safe(tmp_path: Path) -> None:
    path = tmp_path / "data" / "policy.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("max_cpu_percent = 500\n")
    path.chmod(0o600)
    out = _set("max_cpu_percent=50", "allow_unsandboxed=true")
    assert "restart" in out


# ------------------------------------------------------------------ T-6


def _live(tmp_path: Path) -> LivePolicy:
    return LivePolicy(
        PolicySettings(accept_jobs=True),
        tmp_path / "policy.toml",
        gpu_count=0,
        max_concurrent_attempts=4,
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize("mode", [0o666, 0o644, 0o620, 0o640])
def test_a_chmod_alone_is_seen_and_fails_closed(tmp_path: Path, mode: int) -> None:
    live = _live(tmp_path)
    write_overlay(live.path, {"allow_network": True})
    assert live.reload() and live.policy.allow_network
    before = os.stat(live.path)
    os.chmod(live.path, mode)
    after = os.stat(live.path)
    assert (after.st_mtime_ns, after.st_size, after.st_ino) == (
        before.st_mtime_ns,
        before.st_size,
        before.st_ino,
    )
    assert live.reload()
    assert live.policy == op.DENY_ALL
    os.chmod(live.path, 0o600)  # put back: trusted again, without a content change
    assert live.reload()
    assert live.policy.allow_network


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_stamp_holds_mode_owner_and_ctime(tmp_path: Path) -> None:
    live = _live(tmp_path)
    write_overlay(live.path, {"allow_network": True})
    info = os.stat(live.path)
    stamp = live._file_stamp()
    assert stamp is not None
    assert info.st_mode in stamp
    assert info.st_uid in stamp
    assert info.st_ctime_ns in stamp
