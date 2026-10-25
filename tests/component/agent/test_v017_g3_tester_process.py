"""V0.17 G3 tester: the real ``python -m meshforge.agent run`` process with and
without the console - exit codes, SIGTERM, a taken port, a leftover token file."""

from __future__ import annotations

import os
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="needs the agent extra")

pytestmark = [
    pytest.mark.component,
    pytest.mark.timeout(180),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX signals"),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env["PYTHONUTF8"] = "1"
    return env


def _config(tmp: Path, *, console: bool, port: int, extra: str = "") -> Path:
    cfg = tmp / "agent.toml"
    cfg.write_text(
        f'data_dir = "{(tmp / "data").as_posix()}"\n'
        "[agent]\n"
        'controller_address = "127.0.0.1:1"\n'
        "[agent.console]\n"
        f"enabled = {'true' if console else 'false'}\nport = {port}\n"
        f'token_dir = "{(tmp / "console").as_posix()}"\n' + extra,
        encoding="utf-8",
    )
    return cfg


def _run(cfg: Path, log: Path) -> subprocess.Popen[bytes]:
    with log.open("wb") as out:
        return subprocess.Popen(
            [sys.executable, "-m", "meshforge.agent", "run", "--config", str(cfg)],
            env=_env(),
            stdout=out,
            stderr=subprocess.STDOUT,
        )


def _wait_token(tmp: Path, proc: subprocess.Popen[bytes]) -> Path:
    deadline = time.monotonic() + 60
    token = tmp / "console" / "token"
    while not token.is_file():
        assert proc.poll() is None, "agent exited early"
        assert time.monotonic() < deadline
        time.sleep(0.1)
    return token


def test_console_off_and_not_enrolled_keeps_the_old_exit_code(tmp_path: Path) -> None:
    port = _free_port()
    proc = _run(_config(tmp_path, console=False, port=port), tmp_path / "log")
    rc = proc.wait(60)
    assert rc == 4, (rc, (tmp_path / "log").read_text()[-800:])  # not_enrolled
    assert not (tmp_path / "console" / "token").exists()


def test_console_on_not_enrolled_stays_up_and_sigterm_is_a_clean_exit(
    tmp_path: Path,
) -> None:
    port = _free_port()
    proc = _run(_config(tmp_path, console=True, port=port), tmp_path / "log")
    try:
        token = _wait_token(tmp_path, proc)
        assert stat.S_IMODE(token.stat().st_mode) == 0o600
        time.sleep(1.5)
        assert proc.poll() is None, (
            "agent exited although the console should wait for a join"
        )
        with urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{port}/console/v1/health")
        ) as r:
            assert r.status == 200
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not token.exists(), "token file left behind after a clean stop"


def test_console_on_second_sigint_path_and_stale_token_is_replaced(
    tmp_path: Path,
) -> None:
    port = _free_port()
    (tmp_path / "console").mkdir()
    stale = tmp_path / "console" / "token"
    stale.write_text("STALE-TOKEN\n")
    stale.chmod(0o644)  # a hostile / sloppy leftover
    proc = _run(_config(tmp_path, console=True, port=port), tmp_path / "log")
    try:
        deadline = time.monotonic() + 60
        while stale.read_text().strip() == "STALE-TOKEN":
            assert proc.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.1)
        time.sleep(0.3)
        assert stat.S_IMODE(stale.stat().st_mode) == 0o600, "stale file mode survived"
        proc.send_signal(signal.SIGINT)
        assert proc.wait(60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_port_taken_agent_still_runs_without_a_console(tmp_path: Path) -> None:
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        proc = _run(_config(tmp_path, console=True, port=port), tmp_path / "log")
        try:
            time.sleep(4)
            # Console unavailable: the agent then behaves like console-off and
            # exits not_enrolled(4) OR keeps waiting; it must not crash with a traceback exit 1.
            rc = proc.poll()
            assert rc in (None, 4), (rc, (tmp_path / "log").read_text()[-1500:])
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(30)
    text = (tmp_path / "log").read_text()
    assert "Traceback" not in text, text[-1500:]


def test_non_loopback_bind_in_config_is_a_config_error_exit_4(tmp_path: Path) -> None:
    port = _free_port()
    cfg = _config(tmp_path, console=True, port=port, extra='bind = "0.0.0.0"\n')
    proc = _run(cfg, tmp_path / "log")
    rc = proc.wait(60)
    assert rc == 4, (rc, (tmp_path / "log").read_text()[-800:])
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
