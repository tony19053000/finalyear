"""V0.7 G5 tester round 1: do the owner's caps really bind a hostile workload?

Real processes (Linux subprocess runtime) and real containers (Docker, when
the daemon and the pinned image exist). The workload tries to *undo* its limits,
pass them to children and slip under the boundary. Small on purpose: tight
limits, MiB not GiB, short timeouts.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import textwrap
from pathlib import Path

import pytest
from mf_runtime_harness import Capture, Target, new_label

from meshforge.agent.execution.runtime import RunLimits, RunResult, RunSpec, Stream
from meshforge.agent.execution.subprocess_runtime import SubprocessRuntime
from meshforge.agent.policy import not_enforced_caps
from meshforge.agent.settings import PolicySettings
from meshforge.common.errors import FailureType

pytestmark = [pytest.mark.timeout(120)]
MIB = 1024**2
linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="rlimits are the Linux mechanism"
)


def _target() -> Target:
    return Target(SubprocessRuntime(), "subprocess", None, new_label(), sys.executable)


def _run(
    tmp_path: Path, code: str, *, memory_mb: int, pids: int, enforce: bool
) -> tuple[RunResult, Capture]:
    target = _target()
    spec: RunSpec = dataclasses.replace(
        target.spec(tmp_path, code, memory_mb=memory_mb, pids=pids),
        limits=RunLimits(
            memory_bytes=memory_mb * MIB,
            pids=pids,
            timeout_s=60,
            enforce_memory=enforce,
        ),
    )
    capture = Capture()
    return asyncio.run(target.runtime.run(spec, capture)), capture


def _out(capture: Capture) -> str:
    return capture.data(Stream.STDOUT).decode()


UNDO = textwrap.dedent(
    """
    import resource
    res = {}
    for name in ("RLIMIT_AS", "RLIMIT_NPROC"):
        which = getattr(resource, name)
        soft, hard = resource.getrlimit(which)
        try:
            resource.setrlimit(which, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
            res[name] = "RAISED"
        except (ValueError, OSError):
            res[name] = "held"
        try:  # raising only the soft limit up to the hard one must not help either
            resource.setrlimit(which, (hard, hard))
            res[name + "_soft"] = "held" if hard == soft else "RAISED"
        except (ValueError, OSError):
            res[name + "_soft"] = "held"
    print(res, flush=True)
    """
)


@linux_only
def test_the_workload_cannot_lift_its_own_limits(tmp_path: Path) -> None:
    result, capture = _run(tmp_path, UNDO, memory_mb=300, pids=64, enforce=True)
    assert result.ok, capture.data(Stream.STDERR)
    text = _out(capture)
    assert "RAISED" not in text, text


CHILD_SEES = textwrap.dedent(
    """
    import subprocess, sys
    code = (
        "import resource;"
        "print(resource.getrlimit(resource.RLIMIT_AS)[0])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    print("child_as", out.stdout.strip(), out.returncode, flush=True)
    """
)


@linux_only
def test_a_child_process_inherits_the_address_space_cap(tmp_path: Path) -> None:
    result, capture = _run(tmp_path, CHILD_SEES, memory_mb=300, pids=64, enforce=True)
    assert result.ok, capture.data(Stream.STDERR)
    assert f"child_as {300 * MIB} 0" in _out(capture)


BOUNDARY = textwrap.dedent(
    """
    for mb in (16, 64, 200):
        try:
            data = bytearray(mb * 1024 * 1024)
            data[-1] = 1
            print("ok", mb, flush=True)
            del data
        except MemoryError:
            print("memerr", mb, flush=True)
    """
)


@linux_only
def test_allocations_below_the_cap_work_and_above_it_do_not(tmp_path: Path) -> None:
    result, capture = _run(tmp_path, BOUNDARY, memory_mb=160, pids=64, enforce=True)
    assert result.ok, capture.data(Stream.STDERR)
    lines = _out(capture).split("\n")
    assert "ok 16" in lines and "ok 64" in lines and "memerr 200" in lines, lines


@linux_only
def test_a_numpy_free_python_still_starts_at_the_smallest_allowed_cap(
    tmp_path: Path,
) -> None:
    """The validator allows max_ram_bytes down to 64 MiB: a trivial job must at
    least be able to print under it (else the floor is a silent footgun)."""
    result, capture = _run(
        tmp_path, "print('alive', flush=True)", memory_mb=64, pids=64, enforce=True
    )
    assert result.ok, capture.data(Stream.STDERR)
    assert "alive" in _out(capture)


THREADS = textwrap.dedent(
    """
    import threading, time
    stop = threading.Event()
    made = 0
    try:
        for _ in range(300):
            threading.Thread(target=stop.wait, daemon=True).start()
            made += 1
    except RuntimeError:
        pass
    print("threads", made, flush=True)
    stop.set()
    """
)


@linux_only
def test_threads_count_against_the_task_limit(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("RLIMIT_NPROC does not bind root (documented)")
    result, capture = _run(tmp_path, THREADS, memory_mb=1024, pids=12, enforce=False)
    assert result.ok or result.failure_type is FailureType.USER_ERROR
    made = int(_out(capture).split("threads")[1].split()[0])
    assert made <= 40, made  # other tasks of this user may end meanwhile; never 300


# ------------------------------------------------------------------ not_enforced


@pytest.mark.parametrize("platform", ["linux", "win32", "darwin", "freebsd14"])
@pytest.mark.parametrize("gpus", [0, 2])
def test_the_strict_policy_never_claims_what_docker_alone_cannot_enforce(
    platform: str, gpus: int
) -> None:
    """Sandbox-only: Docker enforces RAM and CPUs, nothing enforces VRAM."""
    caps = not_enforced_caps(
        PolicySettings(accept_jobs=True, allow_unsandboxed=False),
        gpu_count=gpus,
        platform=platform,
    )
    assert caps == ("max_vram_bytes:no_runtime_limit",)


def test_every_unsandboxed_cap_gap_is_listed_per_os() -> None:
    on = PolicySettings(accept_jobs=True, allow_unsandboxed=True)
    linux = set(not_enforced_caps(on, gpu_count=0, platform="linux"))
    assert "max_cpu_percent:unsandboxed_linux" in linux
    assert not any(c.startswith("max_ram_bytes") for c in linux)  # RLIMIT_AS
    assert "max_ram_bytes:unsandboxed_gpu_jobs" in not_enforced_caps(
        on, gpu_count=1, platform="linux"
    )
    other = set(not_enforced_caps(on, gpu_count=0, platform="darwin"))
    assert {
        "max_ram_bytes:unsandboxed_runtime",
        "max_cpu_percent:unsandboxed_runtime",
    } <= other
    win = set(not_enforced_caps(on, gpu_count=0, platform="win32"))
    assert win == {"max_vram_bytes:no_runtime_limit"}  # job object: RAM, CPU, procs
