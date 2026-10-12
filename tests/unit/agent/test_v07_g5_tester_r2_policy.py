"""V0.7 G5 tester round 2: attacks on the fix-r1 policy changes (D160).

T-8 start-up policy (hostile / odd ``policy.toml`` at start), T-9 restart note,
T-6 the file stamp (a chmod hidden behind a restored mtime).
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from meshforge.agent.execution.offer import build_execution
from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.agent.main import app
from meshforge.agent.policy import LivePolicy, startup_policy, write_overlay
from meshforge.agent.runtime import build_live_policy
from meshforge.agent.settings import AgentSettings
from meshforge.common import owner_policy as op

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX file semantics"),
    pytest.mark.timeout(30),
]
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

    async def probe(self) -> RuntimeAvailability:
        return RuntimeAvailability("docker", False, reason="docker_sdk_missing")


def _break_file(path: Path, how: str, tmp_path: Path) -> None:
    if how == "directory":
        path.mkdir()
    elif how == "symlink":
        real = tmp_path / "real.toml"
        write_overlay(real, {"accept_jobs": True, "allow_unsandboxed": True})
        path.symlink_to(real)
    elif how == "huge":
        path.write_text("# " + "x" * 2_000_000 + "\naccept_jobs = true\n")
        path.chmod(0o600)
    elif how == "unknown_key":
        path.write_text("accept_jobs = true\nsudo = true\n")
        path.chmod(0o600)
    elif how == "unreadable":
        write_overlay(path, {"accept_jobs": True})
        path.chmod(0o000)
    elif how == "bad_type":
        path.write_text('accept_jobs = "yes please"\n')
        path.chmod(0o600)
    elif how == "nan_limit":
        path.write_text("accept_jobs = true\nmax_cpu_percent = -3\n")
        path.chmod(0o600)
    elif how == "empty_world_readable":
        path.write_text("")
        path.chmod(0o604)
    else:  # pragma: no cover
        raise AssertionError(how)


BROKEN = [
    "directory",
    "symlink",
    "huge",
    "unknown_key",
    "bad_type",
    "nan_limit",
    "empty_world_readable",
]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode 000 files")
@pytest.mark.parametrize("how", [*BROKEN, "unreadable"])
@pytest.mark.asyncio
async def test_a_broken_policy_file_at_start_never_widens_and_denies_all_jobs(
    tmp_path: Path, how: str
) -> None:
    """The overlay would turn everything on; broken, it must grant NOTHING at
    start (config alone: both false) and the live policy denies all jobs."""
    settings = _settings(tmp_path, accept_jobs=False, allow_unsandboxed=False)
    path = settings.data_dir / "policy.toml"
    _break_file(path, how, tmp_path)
    assert startup_policy(settings) == settings.agent.policy
    setup = await build_execution(
        settings,
        node_id="node_x",
        host_has_nvidia_gpu=False,
        docker_factory=lambda _c: _NoDocker(),  # type: ignore[arg-type,return-value]
    )
    assert not setup.enabled and setup.runtimes == {}
    live = build_live_policy(settings, gpu_count=0)
    assert live.policy == op.DENY_ALL


@pytest.mark.parametrize("how", BROKEN)
@pytest.mark.asyncio
async def test_a_broken_file_with_jobs_on_in_the_config_still_denies_every_job(
    tmp_path: Path, how: str
) -> None:
    """Config true + broken file: a runtime exists (config) but the live policy
    refuses everything until the file is fixed, then applies live."""
    settings = _settings(tmp_path, accept_jobs=True, allow_unsandboxed=False)
    path = settings.data_dir / "policy.toml"
    _break_file(path, how, tmp_path)
    live = build_live_policy(settings, gpu_count=0)
    assert live.policy == op.DENY_ALL
    if path.is_symlink() or path.is_dir():
        path.unlink() if path.is_symlink() else path.rmdir()
    else:
        path.unlink()
    write_overlay(path, {"max_cpu_percent": 40})
    assert live.reload()
    assert live.policy != op.DENY_ALL
    assert live.policy.max_cpu_percent == 40


@pytest.mark.asyncio
async def test_a_fifo_as_the_policy_file_does_not_hang_the_start(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, accept_jobs=False)
    os.mkfifo(settings.data_dir / "policy.toml", 0o600)
    assert startup_policy(settings) == settings.agent.policy
    live = build_live_policy(settings, gpu_count=0)
    assert live.policy == op.DENY_ALL


@pytest.mark.asyncio
async def test_the_overlay_cannot_turn_on_what_the_config_leaves_the_node_without(
    tmp_path: Path,
) -> None:
    """Overlay keys outside the live table (e.g. an execution setting) refuse."""
    settings = _settings(tmp_path, accept_jobs=False)
    path = settings.data_dir / "policy.toml"
    path.write_text("accept_jobs = true\n[execution]\nenabled = true\n")
    path.chmod(0o600)
    assert startup_policy(settings) == settings.agent.policy


def test_startup_and_live_policy_agree_on_a_good_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path, accept_jobs=False, allow_unsandboxed=False)
    write_overlay(
        settings.data_dir / "policy.toml",
        {"accept_jobs": True, "allow_unsandboxed": True, "max_cpu_jobs": 1},
    )
    start = startup_policy(settings)
    live = build_live_policy(settings, gpu_count=0)
    for key in ("accept_jobs", "allow_unsandboxed"):
        assert getattr(start, key) == getattr(live.policy, key), key


# ------------------------------------------------------------------ T-9


def _set(*items: str) -> str:
    result = runner.invoke(app, ["policy", "set", *items], prog_name="meshforge-agent")
    assert result.exit_code == 0, result.output
    return result.stdout


def test_both_notes_come_with_an_invalid_old_file_and_the_other_keys_stay_quiet(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data" / "policy.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("max_cpu_percent = 500\n")
    path.chmod(0o600)
    out = _set(
        "max_cpu_percent=50",
        "accept_jobs=true",
        "allow_unsandboxed=true",
        "allow_network=true",
    )
    assert "accept_jobs = false" in out
    assert "allow_unsandboxed = false" in out
    assert out.count("note:") == 2


def test_off_then_on_again_repeats_the_note_each_time() -> None:
    assert "restart" in _set("allow_unsandboxed=true")
    _set("allow_unsandboxed=false")
    assert "restart" in _set("allow_unsandboxed=true")


def test_the_note_survives_a_value_that_is_a_string_true() -> None:
    result = runner.invoke(
        app, ["policy", "set", "allow_unsandboxed=maybe"], prog_name="meshforge-agent"
    )
    assert result.exit_code != 0
    assert "restart" not in result.output


# ------------------------------------------------------------------ T-6


def _live(tmp_path: Path) -> LivePolicy:
    return LivePolicy(
        _settings(tmp_path, accept_jobs=True).agent.policy,
        tmp_path / "policy.toml",
        gpu_count=0,
        max_concurrent_attempts=4,
    )


def test_a_chmod_hidden_behind_a_restored_mtime_is_still_seen(tmp_path: Path) -> None:
    live = _live(tmp_path)
    write_overlay(live.path, {"allow_network": True})
    assert live.reload() and live.policy.allow_network
    before = os.stat(live.path)
    os.chmod(live.path, 0o666)  # noqa: S103 - the attack under test
    os.utime(live.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert live.reload()
    assert live.policy == op.DENY_ALL


def test_a_same_size_content_swap_behind_a_restored_mtime_is_seen(
    tmp_path: Path,
) -> None:
    live = _live(tmp_path)
    write_overlay(live.path, {"allow_network": True})
    assert live.reload() and live.policy.allow_network
    before = os.stat(live.path)
    raw = live.path.read_bytes().replace(b"true", b"1234")
    with open(live.path, "r+b") as fh:
        fh.write(raw)
    os.utime(live.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert os.stat(live.path).st_size == before.st_size
    assert os.stat(live.path).st_mtime_ns == before.st_mtime_ns
    assert os.stat(live.path).st_ino == before.st_ino
    assert live.reload()  # an invalid value now: refused, not silently kept
    assert live.policy == op.DENY_ALL


def test_the_stamp_is_stable_when_nothing_changes(tmp_path: Path) -> None:
    live = _live(tmp_path)
    write_overlay(live.path, {"allow_network": True})
    assert live.reload()
    revision = live.revision
    for _ in range(5):
        assert not live.reload()
    assert live.revision == revision
    assert stat.S_IMODE(os.stat(live.path).st_mode) == 0o600
