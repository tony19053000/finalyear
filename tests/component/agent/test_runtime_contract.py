"""V0.4 G3 (roadmap 0.4.7, brief D51): the ONE runtime contract, run against both
runtimes for real - the Docker sandbox and the unsandboxed subprocess fallback.

Docker cases skip with the reason when no daemon answers (conftest ``docker``
marker) or when the python-cpu image is not local (``docker pull`` it - these tests
never pull ~50 MB on their own). Every check is on real processes/containers: by
pid (psutil) and by the per-test Docker label.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

psutil = pytest.importorskip("psutil", reason="needs the agent extra (pid checks)")
pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")

from mf_runtime_harness import (  # noqa: E402
    Capture,
    Target,
    alive,
    containers_with_label,
    new_label,
)

from meshforge.agent.execution.docker_runtime import (  # noqa: E402
    DockerConfig,
    DockerRuntime,
)
from meshforge.agent.execution.runtime import (  # noqa: E402
    RunObserver,
    RunResult,
    Stream,
)
from meshforge.agent.execution.subprocess_runtime import SubprocessRuntime  # noqa: E402
from meshforge.agent.settings import PYTHON_CPU_IMAGE  # noqa: E402
from meshforge.common.errors import FailureType  # noqa: E402

pytestmark = pytest.mark.timeout(120)

SENTINEL_ENV = "MF_AGENT_ONLY_SENTINEL_G3"


def _image_local(image: str) -> bool:
    from meshforge.agent.execution.docker_support import open_client

    client = open_client(10)
    try:
        client.images.get(image)
    except Exception:  # noqa: BLE001
        return False
    finally:
        client.close()
    return True


@pytest.fixture(
    params=[
        pytest.param("docker", marks=pytest.mark.docker),
        "subprocess",
    ]
)
def target(request: pytest.FixtureRequest) -> Iterator[Target]:
    label = new_label()
    if request.param == "docker":
        if not _image_local(PYTHON_CPU_IMAGE):
            pytest.skip(f"{PYTHON_CPU_IMAGE} is not local: docker pull it first")
        yield Target(
            DockerRuntime(DockerConfig(agent_label=label)),
            "docker",
            PYTHON_CPU_IMAGE,
            label,
            "python",
        )
        assert containers_with_label(label) == [], "a container survived the test"
    else:
        yield Target(SubprocessRuntime(), "subprocess", None, label, sys.executable)


def _run(target: Target, spec: Any, capture: Capture | None = None) -> RunResult:
    cap = capture or Capture()
    return asyncio.run(target.runtime.run(spec, cap))


# ----------------------------------------------------------------- endings
def test_exit_zero_is_success(target: Target, tmp_path: Path) -> None:
    result = _run(target, target.spec(tmp_path, "print('ok')"))
    assert result.ok and result.exit_code == 0 and result.failure_type is None
    assert result.run_s > 0


def test_nonzero_exit_is_user_error(target: Target, tmp_path: Path) -> None:
    result = _run(target, target.spec(tmp_path, "raise SystemExit(3)"))
    assert (result.exit_code, result.failure_type) == (3, FailureType.USER_ERROR)


def test_death_by_signal_is_user_error_with_the_signal(
    target: Target, tmp_path: Path
) -> None:
    if sys.platform == "win32":  # pragma: no cover
        pytest.skip("POSIX signals")
    result = _run(target, target.spec(tmp_path, "import os; os.kill(os.getpid(), 9)"))
    assert result.failure_type is FailureType.USER_ERROR
    assert result.signal == 9


def test_timeout_is_deadline_exceeded_within_2s_of_the_limit(
    target: Target, tmp_path: Path
) -> None:
    limit = 2.0
    spec = target.spec(
        tmp_path,
        "import time; print('READY', flush=True); time.sleep(60)",
        timeout_s=limit,
    )
    started = time.monotonic()
    result = _run(target, spec)
    took = time.monotonic() - started
    assert result.failure_type is FailureType.DEADLINE_EXCEEDED and result.timed_out
    assert limit <= result.run_s <= limit + 2.0, result.run_s
    assert took <= limit + 2.0 + 1.0, took  # + container start
    print(  # noqa: T201 - Laptop 1 timings for the handoff
        f"[{target.kind}] timeout overshoot run_s-limit={result.run_s - limit:.3f}s"
    )


def test_timeout_kills_a_workload_that_ignores_sigterm(
    target: Target, tmp_path: Path
) -> None:
    code = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "print('READY', flush=True); time.sleep(60)"
    )
    spec = target.spec(tmp_path, code, timeout_s=1.0, grace_s=1.0)
    started = time.monotonic()
    result = _run(target, spec)
    assert result.failure_type is FailureType.DEADLINE_EXCEEDED
    # limit + grace + ~1 s for the container start: the kill must follow the grace
    assert time.monotonic() - started <= 1.0 + 1.0 + 1.5


# ------------------------------------------------------------ cancellation
PIDS_CODE = """
import os, subprocess, sys, time
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
open(".pids", "w").write(f"{os.getpid()} {g.pid}")
print("READY", flush=True)
time.sleep(600)
"""


@pytest.mark.parametrize("ignore_sigterm", [False, True])
def test_cancel_mid_run_returns_within_grace_plus_2s_and_nothing_survives(
    target: Target, tmp_path: Path, ignore_sigterm: bool
) -> None:
    code = PIDS_CODE
    if ignore_sigterm:
        code = "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + code
    grace = 1.5
    spec = target.spec(tmp_path, code, grace_s=grace)
    capture = Capture()
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
    print(  # noqa: T201 - Laptop 1 timings for the handoff
        f"[{target.kind}] cancel latency (ignore_sigterm={ignore_sigterm}): {took:.3f}s"
    )
    assert took <= grace + 2.0, took
    result = observer.cancelled_result()
    assert result.cancelled and result.failure_type is FailureType.CANCELLED
    assert result.run_s > 0
    if target.is_docker:
        assert containers_with_label(target.label) == []
    else:
        pids = [int(p) for p in (tmp_path / ".pids").read_text().split()]
        deadline = time.monotonic() + 2.0
        while any(alive(p) for p in pids) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert [p for p in pids if alive(p)] == [], "a workload process survived"


def test_leftover_background_processes_do_not_survive_a_normal_exit(
    target: Target, tmp_path: Path
) -> None:
    code = (
        "import os, subprocess, sys;"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=False);"
        "open('.pids', 'w').write(str(g.pid))"
    )
    started = time.monotonic()
    result = _run(target, target.spec(tmp_path, code))
    assert result.ok
    assert time.monotonic() - started < 10
    if not target.is_docker:
        pid = int((tmp_path / ".pids").read_text())
        deadline = time.monotonic() + 2.0
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not alive(pid)


# ------------------------------------------------------------------- output
STREAM_CODE = r"""
import sys, time
for i in range(200):
    sys.stdout.write(f"o{i}\n"); sys.stdout.flush()
    sys.stderr.write(f"e{i}\n"); sys.stderr.flush()
print(f"FIRST {time.time()!r}", flush=True)
time.sleep(1.5)
print("LAST", flush=True)
"""


def test_output_arrives_in_order_per_stream_promptly_and_from_one_task(
    target: Target, tmp_path: Path
) -> None:
    capture = Capture(marker=b"FIRST")
    result = _run(target, target.spec(tmp_path, STREAM_CODE), capture)
    assert result.ok
    out = capture.data(Stream.STDOUT).decode()
    err = capture.data(Stream.STDERR).decode()
    assert out.splitlines()[:200] == [f"o{i}" for i in range(200)]
    assert err.splitlines() == [f"e{i}" for i in range(200)]
    assert out.splitlines()[-1] == "LAST"
    assert capture.max_concurrent == 1
    printed_at = float(out.split("FIRST ", 1)[1].split("\n", 1)[0])
    seen_at = next(t for s, d, t in capture.chunks if b"FIRST" in d)
    last_at = next(t for s, d, t in capture.chunks if b"LAST" in d)
    latency = seen_at - printed_at
    print(  # noqa: T201 - Laptop 1 timings for the handoff
        f"[{target.kind}] print -> on_output latency {latency * 1000:.1f} ms"
    )
    assert latency < 0.5
    assert last_at - seen_at > 1.0  # streamed while running, not at exit


def test_binary_and_invalid_utf8_pass_through_untouched(
    target: Target, tmp_path: Path
) -> None:
    payload = bytes(range(256)) * 300 + b"\xff\xfe\xc3\x28\x00"
    code = (
        "import sys; data = bytes(range(256)) * 300 + b'\\xff\\xfe\\xc3\\x28\\x00';"
        "sys.stdout.buffer.write(data); sys.stdout.flush(); sys.stderr.buffer.write(data)"
    )
    capture = Capture()
    result = _run(target, target.spec(tmp_path, code), capture)
    assert result.ok
    assert capture.data(Stream.STDOUT) == payload
    assert capture.data(Stream.STDERR) == payload


def test_a_failing_output_consumer_stops_the_run_as_internal_error(
    target: Target, tmp_path: Path
) -> None:
    async def explode(stream: Stream, data: bytes) -> None:
        raise RuntimeError("consumer broke")

    code = "import time\nwhile True:\n    print('x', flush=True); time.sleep(0.05)"
    spec = target.spec(tmp_path, code, grace_s=1.0)
    started = time.monotonic()
    result = asyncio.run(target.runtime.run(spec, explode))
    assert result.failure_type is FailureType.INTERNAL_ERROR
    assert result.reason == "output_consumer_failed"
    assert time.monotonic() - started < 10


# ------------------------------------------------------------- environment
def test_the_agent_environment_is_not_visible(
    target: Target, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SENTINEL_ENV, "agent-secret")
    code = (
        "import os; print('LEAK' if any('agent-secret' in v for v in os.environ.values())"
        f" or {SENTINEL_ENV!r} in os.environ else 'CLEAN'); print(os.environ.get('JOB_VAR'))"
    )
    capture = Capture()
    result = _run(
        target, target.spec(tmp_path, code, env=target.env(JOB_VAR="given")), capture
    )
    assert result.ok
    assert capture.data(Stream.STDOUT).decode().split() == ["CLEAN", "given"]


def test_workdir_is_the_current_directory_and_writable(
    target: Target, tmp_path: Path
) -> None:
    (tmp_path / "input.txt").write_text("in")
    code = "print(open('input.txt').read()); open('out.txt', 'w').write('done')"
    capture = Capture()
    result = _run(target, target.spec(tmp_path, code), capture)
    assert result.ok, result
    assert capture.data(Stream.STDOUT).strip() == b"in"
    assert (tmp_path / "out.txt").read_text() == "done"


# ------------------------------------------------------------------- setup
SETUP_CODE = (
    "import os; os.makedirs('.meshforge/deps', exist_ok=True);"
    "open('.meshforge/deps/mfdep.py', 'w').write('VALUE = 42')"
)


def test_setup_step_is_the_env_phase_and_its_deps_are_importable(
    target: Target, tmp_path: Path
) -> None:
    capture = Capture()
    spec = target.spec(
        tmp_path, "import mfdep; print(mfdep.VALUE)", setup_code=SETUP_CODE
    )
    result = _run(target, spec, capture)
    assert result.ok, result
    assert capture.data(Stream.STDOUT).strip() == b"42"
    assert result.env_s > 0 and result.run_s > 0


def test_a_failing_setup_is_env_error_and_the_workload_never_runs(
    target: Target, tmp_path: Path
) -> None:
    spec = target.spec(
        tmp_path,
        "open('ran', 'w').write('x')",
        setup_code="import sys; print('pip says no', file=sys.stderr); sys.exit(1)",
    )
    capture = Capture()
    result = _run(target, spec, capture)
    assert result.failure_type is FailureType.ENV_ERROR
    assert result.reason == "setup_failed"
    assert b"pip says no" in capture.data(Stream.STDERR)
    assert not (tmp_path / "ran").exists()


def test_a_missing_entrypoint_program_is_typed(target: Target, tmp_path: Path) -> None:
    spec = target.spec(tmp_path, "", argv=("no-such-program-mf", "x"))
    result = _run(target, spec)
    assert result.failure_type in (FailureType.USER_ERROR, FailureType.INTERNAL_ERROR)
    assert not result.ok


def test_a_missing_workdir_is_internal_error(target: Target, tmp_path: Path) -> None:
    result = _run(target, target.spec(tmp_path / "gone", "print(1)"))
    assert (result.failure_type, result.reason) == (
        FailureType.INTERNAL_ERROR,
        "workdir_missing",
    )


def test_env_is_not_the_process_environment_even_when_empty_for_docker(
    target: Target, tmp_path: Path
) -> None:
    """The contract adds only documented variables (HOME, CUDA/NVIDIA hiding)."""
    code = "import os, json; print(json.dumps(sorted(os.environ)))"
    capture = Capture()
    result = _run(target, target.spec(tmp_path, code), capture)
    assert result.ok
    names = set(__import__("json").loads(capture.data(Stream.STDOUT)))
    allowed = set(target.env()) | {
        "HOME",
        "NVIDIA_VISIBLE_DEVICES",
        "CUDA_VISIBLE_DEVICES",
        "LC_CTYPE",  # PEP 538, added by the workload's own Python
        "HOSTNAME",  # Docker
        "GPG_KEY",  # image ENV (python:3.12-slim)
        "PYTHON_VERSION",
        "PYTHON_SHA256",
    }
    assert names <= allowed, names - allowed
    assert os.environ.get(SENTINEL_ENV) is None
