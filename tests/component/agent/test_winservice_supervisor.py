"""V0.17 SP1 (D386): the service host's child supervision with real processes.

The same :class:`~meshforge.agent.winservice_supervisor.Supervisor` runs inside the
Windows service; here (any OS) it supervises real Python children: the exit-code
mapping end to end, the stdin-EOF graceful stop, the grace-expired tree kill
(grandchildren included), a spawn failure - and the real ``meshforge-agent run``
behind ``winservice child``, stopped gracefully.
"""

from __future__ import annotations

import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from meshforge.agent.winservice import StopKind
from meshforge.agent.winservice_supervisor import Supervisor

psutil = pytest.importorskip("psutil", reason="needs the agent extra")

pytestmark = pytest.mark.component

READY_TIMEOUT_S = 60.0


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "child.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _wait_for(log: Path, marker: str, timeout_s: float = READY_TIMEOUT_S) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if log.exists():
            text = log.read_text(encoding="utf-8", errors="replace")
            if marker in text:
                return text
        time.sleep(0.1)
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    raise AssertionError(f"{marker!r} not in log after {timeout_s} s:\n{text[-3000:]}")


def _run_in_thread(sup: Supervisor) -> tuple[threading.Thread, list[object]]:
    result: list[object] = []
    thread = threading.Thread(target=lambda: result.append(sup.run()), daemon=True)
    thread.start()
    return thread, result


def _gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@pytest.mark.parametrize(
    ("code", "kind"), [(4, StopKind.STOPPED), (5, StopKind.STOPPED)]
)
def test_no_restart_codes_become_a_service_specific_stop(
    tmp_path: Path, code: int, kind: StopKind
) -> None:
    child = _script(tmp_path, f"import sys; sys.exit({code})")
    sup = Supervisor(
        [sys.executable, str(child)], log_path=tmp_path / "a.log", grace_s=5
    )
    decision = sup.run()
    assert decision.kind is kind
    assert (decision.win32_exit_code, decision.service_exit_code) == (1066, code)


def test_a_crashing_agent_is_restarted(tmp_path: Path) -> None:
    child = _script(tmp_path, "raise SystemExit(1)")
    sup = Supervisor(
        [sys.executable, str(child)], log_path=tmp_path / "a.log", grace_s=5
    )
    decision = sup.run()
    assert decision.kind is StopKind.RESTART
    assert decision.process_exit_code == 1


def test_a_missing_program_is_a_restartable_spawn_failure(tmp_path: Path) -> None:
    sup = Supervisor(
        [str(tmp_path / "no-such-python")], log_path=tmp_path / "a.log", grace_s=5
    )
    decision = sup.run()
    assert (decision.kind, decision.reason) == (StopKind.RESTART, "spawn_failed")


def test_stop_closes_stdin_and_the_child_exits_gracefully(tmp_path: Path) -> None:
    child = _script(
        tmp_path,
        """
        import signal, sys, time
        from meshforge.agent.winservice import start_stop_watcher, stop_signal
        stopped = []
        signal.signal(stop_signal(), lambda *_: stopped.append(1))
        start_stop_watcher(sys.stdin.buffer.raw)
        print("ready", flush=True)
        while not stopped:
            time.sleep(0.05)
        print("graceful-exit", flush=True)
        """,
    )
    log = tmp_path / "a.log"
    progress: list[float] = []
    sup = Supervisor(
        [sys.executable, str(child)],
        log_path=log,
        grace_s=20,
        on_progress=lambda: progress.append(time.monotonic()),
    )
    thread, result = _run_in_thread(sup)
    _wait_for(log, "ready")
    started = time.monotonic()
    sup.request_stop()
    thread.join(30)
    assert not thread.is_alive()
    assert time.monotonic() - started < 10  # graceful, not the 20 s grace expiry
    assert "graceful-exit" in log.read_text(encoding="utf-8")
    decision = result[0]
    assert getattr(decision, "kind", None) is StopKind.STOPPED
    assert getattr(decision, "reason", None) == "stop_requested"
    assert progress  # the SCM checkpoint callback ran during the stop


def test_grace_expiry_kills_the_whole_tree(tmp_path: Path) -> None:
    child = _script(
        tmp_path,
        """
        import subprocess, sys, time
        kids = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
                for _ in range(2)]
        print("pids", *[k.pid for k in kids], flush=True)
        while True:          # ignores stdin: only the tree kill can end it
            time.sleep(1)
        """,
    )
    log = tmp_path / "a.log"
    sup = Supervisor([sys.executable, str(child)], log_path=log, grace_s=1)
    thread, result = _run_in_thread(sup)
    text = _wait_for(log, "pids ")
    pids = [int(p) for p in text.split("pids ", 1)[1].split()[:2]]
    assert sup.child_pid is not None
    tree = [sup.child_pid, *pids]
    assert all(psutil.pid_exists(p) for p in tree)
    sup.request_stop()
    thread.join(30)
    assert not thread.is_alive()
    deadline = time.monotonic() + 10
    while not all(_gone(p) for p in tree) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert [p for p in tree if not _gone(p)] == []
    assert getattr(result[0], "kind", None) is StopKind.STOPPED


def test_grandchildren_are_swept_even_after_the_child_exits_by_itself(
    tmp_path: Path,
) -> None:
    child = _script(
        tmp_path,
        """
        import subprocess, sys
        k = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        print("pid", k.pid, flush=True)
        sys.exit(1)
        """,
    )
    log = tmp_path / "a.log"
    decision = Supervisor([sys.executable, str(child)], log_path=log, grace_s=5).run()
    assert decision.kind is StopKind.RESTART
    pid = int(log.read_text(encoding="utf-8").split("pid ", 1)[1].split()[0])
    deadline = time.monotonic() + 10
    while not _gone(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _gone(pid)


def test_the_real_agent_stops_gracefully_behind_the_child_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # insecure-dev + a loopback controller that is not there: the agent starts,
    # keeps reconnecting, and must still stop cleanly (Goodbye path, exit 0).
    data = tmp_path / "data"
    config = tmp_path / "config.toml"
    config.write_text(
        textwrap.dedent(
            f"""
            data_dir = {data.as_posix()!r}
            [security]
            mode = "insecure-dev"
            [agent]
            controller_address = "127.0.0.1:9"
            [agent.scores]
            enabled = false
            """
        ),
        encoding="utf-8",
    )
    log = tmp_path / "agent.log"
    argv = [
        sys.executable, "-I", "-m", "meshforge.agent.winservice", "child",
        "--", "run", "--config", str(config),
    ]  # fmt: skip
    sup = Supervisor(argv, log_path=log, grace_s=30, cwd=tmp_path)
    thread, result = _run_in_thread(sup)
    _wait_for(log, "agent.started", timeout_s=90)
    sup.request_stop()
    thread.join(60)
    assert not thread.is_alive()
    text = log.read_text(encoding="utf-8", errors="replace")
    assert "agent.session.stop_requested" in text
    assert '"exit_code": 0' in text, text[-3000:]
    assert "grace_expired" not in text
    assert getattr(result[0], "kind", None) is StopKind.STOPPED


def test_the_real_agent_refusing_its_config_exits_4_not_a_shutdown_crash(
    tmp_path: Path,
) -> None:
    # SP1 run 40097330471: with the stop watcher blocked in a *buffered* stdin read,
    # the agent's own exit 4 died at interpreter shutdown ("_enter_buffered_busy")
    # with a crash code, so the SCM restarted it. Must be a clean 4 -> STOPPED.
    config = tmp_path / "config.toml"
    config.write_text(
        textwrap.dedent(
            f"""
            data_dir = {(tmp_path / "data").as_posix()!r}
            [security]
            mode = "insecure-dev"
            [agent]
            controller_address = "10.255.255.1:47100"
            """
        ),
        encoding="utf-8",
    )
    log = tmp_path / "agent.log"
    argv = [
        sys.executable, "-I", "-m", "meshforge.agent.winservice", "child",
        "--", "run", "--config", str(config),
    ]  # fmt: skip
    decision = Supervisor(argv, log_path=log, grace_s=10, cwd=tmp_path).run()
    text = log.read_text(encoding="utf-8", errors="replace")
    assert "Fatal Python error" not in text, text[-3000:]
    assert (decision.kind, decision.service_exit_code) == (StopKind.STOPPED, 4), text[
        -3000:
    ]
