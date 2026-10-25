"""V0.17 G3: the D388 checks (reports, never gates) and the app-menu launcher.

Checks run the real code paths with the environment faked at its edges
(``which``, the Docker probe, ``disk_usage``, ``ufw.conf``). The launcher is
exercised against a real console server on a free loopback port.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="needs the agent extra")

from meshforge.agent.console import checks as checks_mod
from meshforge.agent.console import launcher
from meshforge.agent.console.checks import CheckStatus, run_checks
from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.agent.settings import AgentSection, AgentSettings
from meshforge.common.config import INSTALL_MODE_ENV_VAR

pytestmark = [pytest.mark.unit]


def _settings(tmp_path: Path, **agent: Any) -> AgentSettings:
    return AgentSettings(
        data_dir=tmp_path / "data",
        agent=AgentSection(controller_address="127.0.0.1:47100", **agent),
    )


# ------------------------------------------------------------------- docker


def _docker(monkeypatch: pytest.MonkeyPatch, result: RuntimeAvailability) -> None:
    import meshforge.agent.execution.docker_support as ds

    monkeypatch.setattr(checks_mod, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ds, "probe_docker", lambda **_: result)


def test_docker_missing_is_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checks_mod, "which", lambda _name: None)
    monkeypatch.setattr(checks_mod.Path, "exists", lambda _self: False)
    check = checks_mod.check_docker(_settings(tmp_path))
    assert check.status is CheckStatus.MISSING and "not installed" in check.why


@pytest.mark.skipif(sys.platform == "win32", reason="Linux service user")
def test_docker_no_access_offers_the_owner_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _docker(
        monkeypatch,
        RuntimeAvailability("docker", False, reason="docker_permission_denied"),
    )
    monkeypatch.setattr(checks_mod, "_in_docker_group_on_disk", lambda: False)
    monkeypatch.setenv(INSTALL_MODE_ENV_VAR, "system")
    check = checks_mod.check_docker(_settings(tmp_path))
    assert check.status is CheckStatus.WARN
    assert check.action == "docker-access"
    assert "admin rights" in check.why and "Give Docker" in check.fix
    # added to the group but not restarted: a restart is the fix
    monkeypatch.setattr(checks_mod, "_in_docker_group_on_disk", lambda: True)
    again = checks_mod.check_docker(_settings(tmp_path))
    assert again.action == "restart"


@pytest.mark.parametrize(
    ("result", "status", "words"),
    [
        (RuntimeAvailability("docker", True, version="27.1"), "ok", "27.1"),
        (
            RuntimeAvailability("docker", False, reason="docker_daemon_unreachable"),
            "warn",
            "not running",
        ),
        (
            RuntimeAvailability("docker", False, reason="docker_seccomp_off"),
            "warn",
            "seccomp",
        ),
    ],
)
def test_docker_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: RuntimeAvailability,
    status: str,
    words: str,
) -> None:
    _docker(monkeypatch, result)
    check = checks_mod.check_docker(_settings(tmp_path))
    assert check.status.value == status and words in check.why


# ---------------------------------------------------------------- GPU, disk


def test_no_nvidia_driver_means_cpu_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checks_mod, "which", lambda _name: None)
    monkeypatch.setattr(checks_mod, "NVIDIA_PROC", Path("/nonexistent/nvidia"))
    gpu, cuda = checks_mod.check_gpu()
    assert gpu.status is CheckStatus.MISSING and "CPU only" in gpu.why
    assert cuda.status is CheckStatus.MISSING


def test_disk_thresholds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    gib = 1024**3
    for free, status in ((50 * gib, "ok"), (5 * gib, "warn"), (gib // 2, "missing")):
        monkeypatch.setattr(
            shutil,
            "disk_usage",
            lambda _p, f=free: shutil._ntuple_diskusage(0, 0, f),  # type: ignore[attr-defined]
        )
        assert checks_mod.check_disk(_settings(tmp_path)).status.value == status


# ---------------------------------------------------------------- firewall


@pytest.mark.skipif(sys.platform != "linux", reason="ufw is Linux")
def test_firewall_reads_ufw_conf_without_root(tmp_path: Path) -> None:
    conf = tmp_path / "ufw.conf"
    settings = _settings(tmp_path)
    peer = _settings(tmp_path, peer={"bind": "192.168.1.5"})
    assert checks_mod.check_firewall(settings, ufw_conf=conf).status is CheckStatus.OK
    conf.write_text("# comment\nENABLED=no\n")
    assert checks_mod.check_firewall(peer, ufw_conf=conf).status is CheckStatus.OK
    conf.write_text('ENABLED="yes"\nLOGLEVEL=low\n')
    assert checks_mod.check_firewall(settings, ufw_conf=conf).status is CheckStatus.OK
    blocked = checks_mod.check_firewall(peer, ufw_conf=conf)
    assert blocked.status is CheckStatus.WARN and "47120" in blocked.fix


def test_peer_check(tmp_path: Path) -> None:
    assert checks_mod.check_peer(_settings(tmp_path), None).status is CheckStatus.OK
    peer = _settings(tmp_path, peer={"bind": "127.0.0.1"})
    assert checks_mod.check_peer(peer, None).status is CheckStatus.WARN
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        ok = checks_mod.check_peer(peer, f"127.0.0.1:{port}")
        assert ok.status is CheckStatus.OK
    dead = checks_mod.check_peer(peer, f"127.0.0.1:{port}")
    assert dead.status is CheckStatus.WARN and dead.action == "restart"


def test_run_checks_never_raises_and_keeps_the_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_settings: AgentSettings) -> Any:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(checks_mod, "check_docker", boom)
    found = asyncio.run(run_checks(_settings(tmp_path), peer_endpoint=None))
    assert [c.id for c in found] == [
        "docker",
        "gpu",
        "cuda",
        "disk",
        "firewall",
        "peer",
    ]
    assert found[0].status is CheckStatus.WARN and "RuntimeError" in found[0].why


# ---------------------------------------------------------------- launcher


@pytest.fixture
def console_server(tmp_path: Path) -> Iterator[tuple[Path, int, Any]]:
    """A real console (uvicorn) on a free loopback port, in its own loop."""
    from meshforge.agent.console.app import ConsoleContext
    from meshforge.agent.console.auth import ConsoleAuth
    from meshforge.agent.console.server import ConsoleServer, bind_console_socket
    from meshforge.agent.console.supervisor import Supervisor

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    sock = bind_console_socket(port)
    auth = ConsoleAuth(port=port, session_ttl_s=600, launch_code_ttl_s=60)
    token_dir = tmp_path / "tok"
    auth.publish(token_dir)
    ctx = ConsoleContext(supervisor=Supervisor(_settings(tmp_path)), auth=auth)
    loop = asyncio.new_event_loop()
    server = ConsoleServer.__new__(ConsoleServer)

    def run() -> None:
        asyncio.set_event_loop(loop)

        async def main() -> None:
            nonlocal server
            server = ConsoleServer(ctx, sock)
            server.start()
            assert server._task is not None
            await server._task

        loop.run_until_complete(main())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield token_dir / "token", port, auth
    finally:
        loop.call_soon_threadsafe(lambda: setattr(server._server, "should_exit", True))
        thread.join(10)
        loop.close()


def test_launcher_trades_the_token_for_a_one_time_url(
    console_server: tuple[Path, int, Any],
) -> None:
    token_file, port, auth = console_server
    url = launcher.launch_url(token_file, port, wait_s=10)
    assert url.startswith(f"http://127.0.0.1:{port}/#c=")
    assert auth.token not in url
    assert auth.redeem_launch_code(url.split("#c=")[1])


def test_launcher_messages(
    tmp_path: Path, console_server: tuple[Path, int, Any]
) -> None:
    token_file, port, _ = console_server
    with pytest.raises(launcher.LaunchError) as missing:
        launcher.read_token(tmp_path / "nope")
    assert "not running" in str(missing.value)
    stale = tmp_path / "stale"
    stale.write_text("old-token\n")
    with pytest.raises(launcher.LaunchError) as old:
        launcher.launch_url(stale, port, wait_s=5)
    assert "out of date" in str(old.value)
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        nobody = free.getsockname()[1]
    with pytest.raises(launcher.LaunchError) as down:
        launcher.launch_url(token_file, nobody, wait_s=0.5)
    assert "did not answer" in str(down.value)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_launcher_says_ask_an_admin_without_the_group(tmp_path: Path) -> None:
    import os

    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    token = tmp_path / "token"
    token.write_text("t")
    token.chmod(0)
    with pytest.raises(launcher.LaunchError) as info:
        launcher.read_token(token)
    assert "meshforge-admin" in str(info.value) and info.value.code == 4


def test_launcher_main_prints_the_url(
    console_server: tuple[Path, int, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_file, port, _ = console_server
    config = tmp_path / "config.toml"
    config.write_text(
        f"[agent.console]\nport = {port}\ntoken_dir = '{token_file.parent.as_posix()}'\n"
    )
    with pytest.raises(SystemExit) as done:
        launcher.main(["--config", str(config), "--print-url", "--wait", "5"])
    assert done.value.code == 0
    assert capsys.readouterr().out.startswith(f"http://127.0.0.1:{port}/#c=")
