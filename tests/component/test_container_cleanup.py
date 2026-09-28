"""Regression tests for DEFECT-0.1.8-01: test containers must never outlive a run.

A child pytest (a copy of the suite's conftests and ``tests/_support`` in a temp
dir) holds ``pg_settings`` and is interrupted — one SIGINT during the test, a second
SIGINT inside the ``pg_settings`` finalizer (what a terminal Ctrl-C forwarded by
``uv`` can do), a pytest-timeout abort (``signal`` and ``thread`` methods), or
SIGKILL. Afterwards no container with the child's ``meshforge.test.run-id`` label
may remain. The stale sweep is tested against real containers (dead-owner and
PID-reused-owner containers go, everything else stays) and end to end: a child with
Ryuk disabled is SIGKILLed and the next child run's sweep removes its container.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pytest
from mf_test_containers import (
    LABEL_OWNER_HOST,
    LABEL_OWNER_NS,
    LABEL_OWNER_PID,
    LABEL_OWNER_START,
    LABEL_RUN_ID,
    LABEL_TEST,
    RUN_ID_ENV,
    pid_namespace_id,
    process_start_time,
    sweep_stale_test_containers,
)

pytestmark = pytest.mark.docker

COMPONENT_DIR: Final = Path(__file__).resolve().parent
TESTS_DIR: Final = COMPONENT_DIR.parent
#: Same image as tests/component/conftest.py (already pulled; no extra download).
POSTGRES_IMAGE: Final = "postgres:16.15-alpine3.24"
_READY: Final = "MF-CHILD-READY"
_DROPPING: Final = "MF-CHILD-DROPPING"
#: Child start-up may include the first Postgres image pull.
_MARKER_TIMEOUT_S: Final = 120.0
_CHILD_TIMEOUT_S: Final = 120.0
#: Ryuk reaps ~RYUK_RECONNECTION_TIMEOUT (10 s) after its client socket drops.
_LEAK_DEADLINE_S: Final = 20.0

_CHILD_INI: Final = """\
[pytest]
timeout = 300
asyncio_mode = strict
asyncio_default_fixture_loop_scope = function
markers =
    component: tier
    docker: capability
"""

_CHILD_SLEEP_TEST: Final = f"""\
import time


def test_hold_db(pg_settings):
    print("{_READY}", flush=True)
    time.sleep(60)
"""

# Failure injection: the DROP DATABASE in pg_settings' finalizer announces itself
# and stalls, so the second SIGINT deterministically lands inside a function-scoped
# finalizer (the DEFECT-0.1.8-01 path; a terminal Ctrl-C forwarded by `uv` does
# the same by chance).
_CHILD_TEARDOWN_TEST: Final = f"""\
import time


def test_hold_db(pg_settings, postgres_server):
    cls = type(postgres_server)
    original = cls.execute_admin

    def slow_drop(self, *statements, **kwargs):
        if any("DROP DATABASE" in s for s in statements):
            print("{_DROPPING}", flush=True)
            time.sleep(30)
        return original(self, *statements, **kwargs)

    cls.execute_admin = slow_drop
    print("{_READY}", flush=True)
    time.sleep(60)
"""


def _child_timeout_test(method: str) -> str:
    return f"""\
import time

import pytest


@pytest.mark.timeout(3, method="{method}")
def test_hold_db_past_timeout(pg_settings):
    print("{_READY}", flush=True)
    time.sleep(60)
"""


_CHILD_PASSING_TEST: Final = """\
def test_uses_server(postgres_server):
    assert postgres_server.port > 0
"""


@pytest.fixture
def docker_client() -> Iterator[Any]:
    pytest.importorskip("asyncpg", reason="needs the controller extra")
    pytest.importorskip("testcontainers", reason="needs the dev dependency group")
    import docker

    client = docker.from_env()
    try:
        yield client
    finally:
        client.close()


def _labelled(client: Any, label: str) -> list[Any]:
    """Containers with ``label`` that still exist.

    ``containers.list`` lists ids, then inspects each one; a container Ryuk (or
    a finalizer) removes between the two calls used to raise ``NotFound`` out of
    this *observer* - the flake D62 carried to V0.4 G4. A container removed
    while we look is simply not there: ``ignore_removed`` says exactly that.
    """
    return list(
        client.containers.list(all=True, filters={"label": label}, ignore_removed=True)
    )


def _force_remove_all(client: Any, label: str) -> None:
    import docker

    for container in _labelled(client, label):
        with contextlib.suppress(docker.errors.NotFound):
            container.remove(force=True, v=True)


def _child_project(tmp_path: Path, test_source: str) -> Path:
    tests = tmp_path / "tests"
    (tests / "component").mkdir(parents=True)
    shutil.copy(TESTS_DIR / "conftest.py", tests / "conftest.py")
    shutil.copytree(
        TESTS_DIR / "_support",
        tests / "_support",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy(COMPONENT_DIR / "conftest.py", tests / "component" / "conftest.py")
    (tests / "component" / "test_child.py").write_text(test_source)
    (tmp_path / "pytest.ini").write_text(_CHILD_INI)
    return tmp_path


class _Child:
    """A child pytest whose output is pumped by a reader thread (never blocks us)."""

    def __init__(
        self, project: Path, run_id: str, extra_env: dict[str, str] | None = None
    ) -> None:
        env = {**os.environ, RUN_ID_ENV: run_id, **(extra_env or {})}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider"],
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._seen: list[str] = []
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        with self.proc.stdout:  # the reader owns the pipe and closes it at EOF
            for line in self.proc.stdout:
                self._lines.put(line)
        self._lines.put(None)

    @property
    def output(self) -> str:
        return "".join(self._seen)

    def wait_for(self, marker: str, timeout: float = _MARKER_TIMEOUT_S) -> None:
        """Return once the child printed ``marker``; fail fast otherwise."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.kill()
                pytest.fail(
                    f"child did not print {marker!r} within {timeout:.0f} s "
                    f"(killed); output so far:\n{self.output}"
                )
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                self.proc.wait()
                pytest.fail(
                    f"child exited (rc={self.proc.returncode}) before printing "
                    f"{marker!r}; output:\n{self.output}"
                )
            self._seen.append(line)
            if marker in line:
                return

    def finish(self, timeout: float = _CHILD_TIMEOUT_S) -> str:
        """Wait for exit and return the complete output."""
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            pytest.fail(f"child still running after {timeout:.0f} s:\n{self.output}")
        self._reader.join(timeout=10)
        while True:
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                break
            if line is not None:
                self._seen.append(line)
        return self.output

    def signal_group(self, signum: int) -> None:
        os.killpg(self.proc.pid, signum)

    def kill(self) -> None:
        """SIGKILL / TerminateProcess the child (its whole group on POSIX)."""
        if self.proc.poll() is None:
            if sys.platform == "win32":
                self.proc.kill()
            else:
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait()


def _assert_no_leak(client: Any, run_id: str, child_output: str) -> None:
    label = f"{LABEL_RUN_ID}={run_id}"
    deadline = time.monotonic() + _LEAK_DEADLINE_S
    while _labelled(client, label) and time.monotonic() < deadline:
        time.sleep(0.5)
    left = [f"{c.name} ({c.status})" for c in _labelled(client, label)]
    assert not left, f"leaked test containers {left}; child output:\n{child_output}"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX signals only; Windows CTRL_BREAK handling is out of scope",
)
@pytest.mark.parametrize(
    "in_teardown", [False, True], ids=["sigint-in-test", "sigint-in-teardown"]
)
def test_sigint_during_pg_settings_leaks_no_container(
    docker_client: Any, tmp_path: Path, in_teardown: bool
) -> None:
    run_id = uuid4().hex
    source = _CHILD_TEARDOWN_TEST if in_teardown else _CHILD_SLEEP_TEST
    child = _Child(_child_project(tmp_path, source), run_id)
    try:
        child.wait_for(_READY)
        assert _labelled(docker_client, f"{LABEL_RUN_ID}={run_id}"), (
            "child holds pg_settings but no labelled container exists"
        )
        child.signal_group(signal.SIGINT)
        if in_teardown:
            child.wait_for(_DROPPING)
            child.signal_group(signal.SIGINT)
        output = child.finish()
        assert child.proc.returncode != 0, output
        _assert_no_leak(docker_client, run_id, output)
    finally:
        child.kill()
        _force_remove_all(docker_client, f"{LABEL_RUN_ID}={run_id}")


@pytest.mark.parametrize("method", ["signal", "thread"])
def test_pytest_timeout_during_pg_settings_leaks_no_container(
    docker_client: Any, tmp_path: Path, method: str
) -> None:
    # `signal` raises in the test (finalizers run); `thread` (the Windows default)
    # calls os._exit(1), so no Python cleanup runs and Ryuk must reap.
    if method == "signal" and not hasattr(signal, "SIGALRM"):
        pytest.skip("pytest-timeout's signal method needs SIGALRM (POSIX)")
    run_id = uuid4().hex
    child = _Child(_child_project(tmp_path, _child_timeout_test(method)), run_id)
    try:
        child.wait_for(_READY)
        output = child.finish()
        assert child.proc.returncode != 0, output
        assert "Timeout" in output, output
        _assert_no_leak(docker_client, run_id, output)
    finally:
        child.kill()
        _force_remove_all(docker_client, f"{LABEL_RUN_ID}={run_id}")


def test_sigkill_during_pg_settings_leaks_no_container(
    docker_client: Any, tmp_path: Path
) -> None:
    # SIGKILL runs no Python code: Ryuk (layer 3) must reap within the deadline.
    run_id = uuid4().hex
    child = _Child(_child_project(tmp_path, _CHILD_SLEEP_TEST), run_id)
    try:
        child.wait_for(_READY)
        assert _labelled(docker_client, f"{LABEL_RUN_ID}={run_id}")
        child.kill()
        output = child.finish()
        _assert_no_leak(docker_client, run_id, output)
    finally:
        child.kill()
        _force_remove_all(docker_client, f"{LABEL_RUN_ID}={run_id}")


def test_next_run_sweeps_container_of_killed_run_without_ryuk(
    docker_client: Any, tmp_path: Path
) -> None:
    # Layer 4 end to end: with Ryuk disabled nothing reaps a SIGKILLed run's
    # container, so it must survive the kill — and the next run's sweep removes it.
    if pid_namespace_id() is None:
        pytest.skip("no PID-namespace identity on this platform: the sweep is off")
    if sys.platform == "win32" and importlib.util.find_spec("psutil") is None:
        pytest.skip("owner liveness on Windows needs psutil (the agent extra)")
    killed_run, next_run = uuid4().hex, uuid4().hex
    killed_label = f"{LABEL_RUN_ID}={killed_run}"
    first = _Child(
        _child_project(tmp_path / "first", _CHILD_SLEEP_TEST),
        killed_run,
        {"TESTCONTAINERS_RYUK_DISABLED": "true"},
    )
    second: _Child | None = None
    try:
        first.wait_for(_READY)
        first.kill()
        output = first.finish()
        assert _labelled(docker_client, killed_label), (
            f"container vanished without Ryuk or a sweep; output:\n{output}"
        )

        second = _Child(
            _child_project(tmp_path / "second", _CHILD_PASSING_TEST), next_run
        )
        second_output = second.finish()
        assert second.proc.returncode == 0, second_output
        assert not _labelled(docker_client, killed_label), (
            f"next run did not sweep the killed run's container:\n{second_output}"
        )
        _assert_no_leak(docker_client, next_run, second_output)
    finally:
        first.kill()
        if second is not None:
            second.kill()
        _force_remove_all(docker_client, killed_label)
        _force_remove_all(docker_client, f"{LABEL_RUN_ID}={next_run}")


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _sleeper(client: Any, labels: dict[str, str]) -> Any:
    # The postgres image is already needed by this suite; no extra pull.
    return client.containers.run(
        POSTGRES_IMAGE, ["sleep", "300"], labels=labels, detach=True
    )


def test_stale_sweep_removes_only_dead_owner_containers(docker_client: Any) -> None:
    namespace = pid_namespace_id()
    if namespace is None:
        pytest.skip("no PID-namespace identity on this platform: the sweep is off")
    marker = f"meshforge.sweeptest={uuid4().hex}"
    key, value = marker.split("=")
    host = socket.gethostname()
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    live_start = process_start_time(live.pid)
    assert live_start is not None

    def labelled(
        owner_pid: int,
        *,
        start: str | None = "",
        owner_host: str = host,
        owner_ns: str = namespace,
    ) -> dict[str, str]:
        labels = {
            key: value,
            LABEL_TEST: "1",
            LABEL_OWNER_PID: str(owner_pid),
            LABEL_OWNER_HOST: owner_host,
            LABEL_OWNER_NS: owner_ns,
            LABEL_RUN_ID: "sweep-test",
        }
        if start is not None:
            labels[LABEL_OWNER_START] = start
        return labels

    try:
        dead = _sleeper(docker_client, labelled(_dead_pid()))
        reused = _sleeper(docker_client, labelled(live.pid, start=f"1{live_start}"))
        alive = _sleeper(docker_client, labelled(live.pid, start=live_start))
        alive_no_start = _sleeper(docker_client, labelled(live.pid, start=None))
        other_host = _sleeper(
            docker_client,
            labelled(_dead_pid(), owner_host=f"not-{host}-{uuid4().hex[:6]}"),
        )
        # Same host name, other PID namespace (e.g. WSL vs Windows on one daemon).
        other_ns = _sleeper(
            docker_client, labelled(_dead_pid(), owner_ns=f"{namespace}-other")
        )
        unlabelled = _sleeper(docker_client, {key: value})
        survivors = {
            alive.id,
            alive_no_start.id,
            other_host.id,
            other_ns.id,
            unlabelled.id,
        }

        # NB: this is the real, global sweep — it also removes any genuinely
        # stale MeshForge test container of this namespace on the daemon, which is
        # harmless by design. Assertions only look at this test's containers.
        removed = sweep_stale_test_containers(docker_client)

        remaining = {c.id for c in _labelled(docker_client, marker)}
        assert {dead.id, reused.id} <= set(removed)
        assert not {dead.id, reused.id} & remaining
        assert survivors <= remaining
        assert not survivors & set(removed)
    finally:
        live.kill()
        live.wait()
        _force_remove_all(docker_client, marker)
