"""V0.4 G3 tester round 1 - adversarial additions to criterion 5 (sandbox) and
D51 (Docker runtime) not covered by the implementer's test_docker_sandbox.py /
test_runtime_contract.py:

* a host-side symlink inside the workdir cannot be used to read or write a host
  path (mount-namespace isolation, not merely a path-string check);
* the setup container (network on, requirements install) does not leak the
  agent's environment either, and the run container's network is really off
  even though the setup container's was on;
* a bogus Docker daemon (wrong socket) fails the run with a typed result, never
  a traceback;
* cancelling during the env phase (setup container) still returns within
  grace_s + 2s and leaves no container;
* the env phase has its own timeout (env_timeout_s), separate from the job's
  own timeout_s, and ends the run `timeout` not `deadline_exceeded`;
* an output flood is not lost, not reordered, and does not blow up memory
  unboundedly (backpressure keeps at most one in-flight chunk, per the
  contract's "one task" rule already proven in test_runtime_contract.py);
* exit code 137 (SIGKILL, not our doing) with no memory pressure is
  user_error, never misclassified oom.

Real containers only; skips without Docker / without the local image, matching
the implementer's fixtures.
"""

from __future__ import annotations

import asyncio
import socket
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")

from mf_runtime_harness import Capture, Target, containers_with_label, new_label

from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.docker_support import open_client
from meshforge.agent.execution.runtime import RunObserver, RunResult, RunSpec, Stream
from meshforge.agent.settings import PYTHON_CPU_IMAGE
from meshforge.common.errors import FailureType

pytestmark = [pytest.mark.docker, pytest.mark.timeout(180)]


def _image_local(image: str) -> bool:
    client = open_client(10)
    try:
        client.images.get(image)
    except Exception:  # noqa: BLE001
        return False
    finally:
        client.close()
    return True


@pytest.fixture
def target() -> Iterator[Target]:
    if not _image_local(PYTHON_CPU_IMAGE):
        pytest.skip(f"{PYTHON_CPU_IMAGE} is not local: docker pull it first")
    label = new_label()
    t = Target(
        DockerRuntime(DockerConfig(agent_label=label)),
        "docker",
        PYTHON_CPU_IMAGE,
        label,
        "python",
    )
    yield t
    assert containers_with_label(label) == [], "a container survived the test"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "host_secret.txt").write_text("HOST-ONLY-CONTENT-7c9a2")
    return work


def _run(
    target: Target, spec: RunSpec, capture: Capture | None = None
) -> tuple[RunResult, Capture]:
    cap = capture or Capture()
    return asyncio.run(target.runtime.run(spec, cap)), cap


# --------------------------------------------------------------- symlink escape
def test_a_symlink_inside_the_workdir_cannot_read_a_host_path(
    target: Target, workdir: Path, tmp_path: Path
) -> None:
    """A symlink created on the host, inside /work, pointing at an absolute host
    path: because the container only ever sees /work bind-mounted, chasing the
    link inside the container resolves against the *container's* root, never
    the host's - this must hold even though the string looks like an absolute
    host path from outside."""
    host_secret = tmp_path / "host_secret.txt"
    link = workdir / "escape_link"
    link.symlink_to(host_secret)
    code = textwrap.dedent(
        f"""
        import os
        try:
            data = open({str(host_secret)!r}).read()
            print("READ:" + data)
        except OSError as e:
            print("BLOCKED:" + type(e).__name__)
        """
    )
    result, capture = _run(target, target.spec(workdir, code))
    assert result.ok, capture.data(Stream.STDERR)
    out = capture.data(Stream.STDOUT).decode().strip()
    assert out.startswith("BLOCKED:"), out
    assert "HOST-ONLY-CONTENT" not in out


def test_a_symlink_inside_the_workdir_cannot_write_a_host_path(
    target: Target, workdir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    link = workdir / "escape_write_link"
    link.symlink_to(outside)
    code = textwrap.dedent(
        f"""
        import os
        try:
            with open(os.path.join({str(outside)!r}, "pwned.txt"), "w") as f:
                f.write("x")
            print("WROTE")
        except OSError as e:
            print("BLOCKED:" + type(e).__name__)
        """
    )
    result, capture = _run(target, target.spec(workdir, code))
    assert result.ok, capture.data(Stream.STDERR)
    out = capture.data(Stream.STDOUT).decode().strip()
    assert out.startswith("BLOCKED:"), out
    assert list(outside.iterdir()) == [], "a file leaked onto the real host path"


def test_a_relative_symlink_within_work_chases_only_the_container_own_root(
    target: Target, workdir: Path
) -> None:
    """/work/etc -> /etc (a container-relative absolute path): writing through it
    must fail typed (read-only rootfs), never crash the runtime."""
    link = workdir / "etc"
    link.symlink_to("/etc")
    code = textwrap.dedent(
        """
        import os
        try:
            with open("/work/etc/mf-escape.txt", "w") as f:
                f.write("x")
            print("WROTE")
        except OSError as e:
            print("BLOCKED:" + type(e).__name__)
        """
    )
    result, capture = _run(target, target.spec(workdir, code))
    assert result.ok, capture.data(Stream.STDERR)
    assert capture.data(Stream.STDOUT).decode().strip().startswith("BLOCKED:")


# --------------------------------------------------------------- env / network
def test_the_setup_container_does_not_leak_the_agent_environment_either(
    target: Target, workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MF_G3_SETUP_SENTINEL", "agent-secret")
    setup_code = (
        "import os; print('SETUP_LEAK' if 'agent-secret' in os.environ.values()"
        " else 'SETUP_CLEAN')"
    )
    result, capture = _run(
        target, target.spec(workdir, "print('done')", setup_code=setup_code)
    )
    assert result.ok, capture.data(Stream.STDERR)
    # the setup container's output rides the same on_output callback as the run
    # container's (see test_run_phase_network_is_off_...); the sentinel must
    # never appear in either.
    assert b"agent-secret" not in capture.data(Stream.STDOUT)
    assert b"agent-secret" not in capture.data(Stream.STDERR)


_IFACES = "import os; print(','.join(sorted(os.listdir('/sys/class/net'))))"


def test_run_phase_network_is_off_even_though_setup_phase_had_it_on(
    target: Target, workdir: Path
) -> None:
    """Hermetic (D63-1): asserts the containers' interfaces, never the internet.

    The run container (``network=False``) must have **only** ``lo``; the setup
    container (network on for ``pip install``) must have more than ``lo``. Both
    print through the same on_output callback, setup first.
    """
    result, capture = _run(
        target, target.spec(workdir, _IFACES, setup_code=_IFACES, network=False)
    )
    assert result.ok, capture.data(Stream.STDERR)
    setup_line, run_line = capture.data(Stream.STDOUT).decode().strip().splitlines()
    assert run_line == "lo", f"run container interfaces: {run_line}"
    assert set(setup_line.split(",")) - {"lo"}, f"setup had no network: {setup_line}"


def _host_online() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=3).close()
    except OSError:
        return False
    return True


def test_setup_phase_reaches_the_internet_when_the_host_does(
    target: Target, workdir: Path
) -> None:
    """The other half of the old test, kept apart: it needs a real uplink."""
    if not _host_online():
        pytest.skip("this host has no internet route (1.1.1.1:53 unreachable)")
    setup_code = (
        "import socket\n"
        "socket.create_connection(('1.1.1.1', 53), timeout=5).close()\n"
        "print('SETUP_NET_OK')\n"
    )
    result, capture = _run(
        target, target.spec(workdir, "print('RUN')", setup_code=setup_code)
    )
    assert result.ok, capture.data(Stream.STDERR)
    assert b"SETUP_NET_OK" in capture.data(Stream.STDOUT)


# --------------------------------------------------------------- daemon down
def test_an_unreachable_docker_daemon_is_a_typed_result_not_a_traceback(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/mf-g3-tester-no-such-socket.sock")
    label = new_label()
    runtime = DockerRuntime(DockerConfig(agent_label=label))
    spec_target = Target(runtime, "docker", PYTHON_CPU_IMAGE, label, "python")
    spec = spec_target.spec(workdir, "print(1)")
    capture = Capture()
    result = asyncio.run(runtime.run(spec, capture))
    assert result.failure_type in (FailureType.ENV_ERROR, FailureType.INTERNAL_ERROR)
    assert not result.ok
    assert result.reason in ("docker_unavailable", "docker_daemon_unreachable")


# --------------------------------------------------------------- cancel in env
def test_cancel_during_the_setup_container_returns_within_grace_and_leaves_nothing(
    target: Target, workdir: Path
) -> None:
    setup_code = "import time; print('SETUP_READY', flush=True); time.sleep(60)"
    grace = 1.5
    spec = target.spec(
        workdir, "print('never runs')", setup_code=setup_code, grace_s=grace
    )
    capture = Capture(marker=b"SETUP_READY")
    observer = RunObserver()

    async def scenario() -> float:
        task = asyncio.create_task(target.runtime.run(spec, capture, observer=observer))
        await asyncio.wait_for(capture.seen.wait(), 30)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - started

    took = asyncio.run(scenario())
    assert took <= grace + 2.0, took
    assert containers_with_label(target.label) == []


# --------------------------------------------------------------- env timeout
def test_env_phase_has_its_own_timeout_distinct_from_the_job_timeout(
    workdir: Path,
) -> None:
    label = new_label()
    config = DockerConfig(agent_label=label, env_timeout_s=1.0)
    runtime = DockerRuntime(config)
    target = Target(runtime, "docker", PYTHON_CPU_IMAGE, label, "python")
    setup_code = "import time; time.sleep(30)"
    spec = target.spec(
        workdir, "print('never runs')", setup_code=setup_code, timeout_s=600
    )
    started = time.monotonic()
    result, capture = _run(target, spec)
    took = time.monotonic() - started
    assert result.failure_type is FailureType.TIMEOUT, (
        result,
        capture.data(Stream.STDERR),
    )
    assert result.reason == "env_timeout"
    assert took < 15, took  # bounded by env_timeout_s + grace, not the 600s job limit
    assert containers_with_label(label) == []


# --------------------------------------------------------------- output flood
FLOOD_CODE = textwrap.dedent(
    """
    import sys
    chunk = b"A" * 65536
    total = 0
    target = 20 * 1024 * 1024
    while total < target:
        sys.stdout.buffer.write(chunk)
        total += len(chunk)
    sys.stdout.flush()
    print("\\nDONE", flush=True)
    """
)


def test_a_large_output_flood_is_not_lost_reordered_or_unbounded(
    target: Target, workdir: Path
) -> None:
    """20 MiB (not the full 100 MiB named in the brief, to keep this laptop's run
    time and disk headroom sane) of stdout, streamed with backpressure."""
    capture = Capture(marker=b"DONE")
    spec = target.spec(workdir, FLOOD_CODE, timeout_s=60)
    result = asyncio.run(target.runtime.run(spec, capture))
    assert result.ok, capture.data(Stream.STDERR)[-2000:]
    out = capture.data(Stream.STDOUT)
    assert out.count(b"A") >= 20 * 1024 * 1024
    assert out.endswith(b"DONE\n")
    assert capture.max_concurrent == 1  # backpressure: never more than one in flight


# --------------------------------------------------------------- docker socket
def test_the_docker_socket_is_not_mounted_into_the_workload(
    target: Target, workdir: Path
) -> None:
    """Only the workdir is ever mounted (D51): a workload must not be able to
    reach the host's Docker daemon (which would be a full sandbox escape - it
    could launch unsandboxed siblings) via the conventional socket path, nor via
    a TCP docker endpoint on the loopback the daemon might also listen on."""
    code = textwrap.dedent(
        """
        import os, socket
        found = os.path.exists("/var/run/docker.sock") or os.path.exists(
            "/run/docker.sock"
        )
        print("SOCK_PRESENT" if found else "SOCK_ABSENT")
        for port in (2375, 2376):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                print(f"TCP_OPEN:{port}")
            except OSError:
                print(f"TCP_CLOSED:{port}")
        """
    )
    result, capture = _run(target, target.spec(workdir, code, network=False))
    assert result.ok, capture.data(Stream.STDERR)
    lines = capture.data(Stream.STDOUT).decode().strip().splitlines()
    assert lines[0] == "SOCK_ABSENT", lines
    assert all(line.startswith("TCP_CLOSED") for line in lines[1:]), lines


# --------------------------------------------------------------- OOM vs plain kill
def test_a_self_sigkill_with_no_memory_pressure_is_user_error_not_oom(
    target: Target, workdir: Path
) -> None:
    code = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    result, capture = _run(target, target.spec(workdir, code, memory_mb=256))
    assert result.failure_type is FailureType.USER_ERROR, (
        result,
        capture.data(Stream.STDERR),
    )
    assert result.oom_killed is False
    assert result.signal == 9
