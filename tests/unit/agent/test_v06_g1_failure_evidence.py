"""V0.6 G1 (D116): the failure-classification rules, without processes.

The real causes (a kernel OOM kill, a MemoryError, a CUDA-OOM message, a full
tmpfs, a failed env build) are in tests/component/agent/test_v06_g1_failure_types.py;
these pin the rule table and its bounds.
"""

from __future__ import annotations

import errno
import shutil
from pathlib import Path
from typing import Any

import pytest
from test_executor import FakeRuntime, Reports, _executor, _settle, _start

from meshforge.agent.execution.failure_evidence import (
    STDERR_TAIL_BYTES,
    WINDOWS_OOM_EXIT_CODES,
    OomEvidence,
    OutputTail,
    disk_full_cause,
    refine_result,
    windows_oom_exit,
)
from meshforge.agent.execution.runtime import RunObserver, RunResult
from meshforge.common.errors import FailureType
from meshforge.common.proto_codec import failure_type_from_proto

pytestmark = pytest.mark.unit

USER = RunResult(exit_code=1, failure_type=FailureType.USER_ERROR, detail="exit code 1")


# ------------------------------------------------------------------ tail


def test_the_tail_keeps_exactly_the_last_limit_bytes() -> None:
    tail = OutputTail(8)
    tail.add(b"12345678")  # max accepted: all of it
    assert tail.data == b"12345678"
    tail.add(b"9")  # max + 1: the oldest byte goes
    assert tail.data == b"23456789"
    tail.add(b"x" * 100)  # one huge chunk
    assert tail.data == b"x" * 8
    assert OutputTail().data == b""
    assert STDERR_TAIL_BYTES == 64 * 1024


# ------------------------------------------------------------- refinement


@pytest.mark.parametrize(
    "line",
    [
        b"torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB",
        b"torch.cuda.OutOfMemoryError: CUDA out of memory.",
        b"RuntimeError: CUDA error: out of memory",
    ],
)
def test_a_cuda_oom_marker_makes_a_user_error_vram_oom(line: bytes) -> None:
    refined = refine_result(
        USER, b"Traceback (most recent call last):\n" + line + b"\n"
    )
    assert refined.failure_type is FailureType.VRAM_OOM
    assert refined.reason == "cuda_out_of_memory"
    assert "exit code 1" in refined.detail


def test_a_final_memory_error_line_is_host_oom() -> None:
    tail = b'Traceback (most recent call last):\n  File "x"\nMemoryError\n\n'
    refined = refine_result(USER, tail)
    assert refined.failure_type is FailureType.OOM and refined.oom_killed
    assert refined.reason == "memory_error"


def test_memory_error_that_is_not_the_last_line_stays_user_error() -> None:
    tail = b"MemoryError\nhandled it, then failed for another reason\n"
    assert refine_result(USER, tail) is USER


@pytest.mark.parametrize(
    "failure",
    [
        None,
        FailureType.DEADLINE_EXCEEDED,
        FailureType.CANCELLED,
        FailureType.ENV_ERROR,
        FailureType.OOM,
    ],
)
def test_only_a_user_error_is_ever_refined(failure: FailureType | None) -> None:
    result = RunResult(exit_code=0 if failure is None else 1, failure_type=failure)
    assert refine_result(result, b"CUDA out of memory\nMemoryError\n") is result


def test_windows_out_of_memory_exit_statuses() -> None:
    assert {0xC0000017, 0xC000012D} == WINDOWS_OOM_EXIT_CODES
    assert windows_oom_exit(0xC0000017)
    assert windows_oom_exit(-1073741801)  # the same status as a signed int
    assert not windows_oom_exit(1) and not windows_oom_exit(0)
    assert not windows_oom_exit(-9) and not windows_oom_exit(None)


# --------------------------------------------------------------- disk full


def test_disk_full_is_found_anywhere_in_the_chain() -> None:
    enospc = OSError(errno.ENOSPC, "No space left on device")
    assert disk_full_cause(enospc) is enospc
    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = enospc
    assert disk_full_cause(wrapped) is enospc
    during = ValueError("raised while handling it")
    during.__context__ = enospc
    assert disk_full_cause(during) is enospc
    quota = OSError(errno.EDQUOT, "Disk quota exceeded")
    assert disk_full_cause(quota) is quota
    assert disk_full_cause(OSError(errno.EACCES, "denied")) is None
    assert disk_full_cause(None) is None
    loop = RuntimeError("a")
    loop.__context__ = RuntimeError("b")
    loop.__context__.__context__ = loop  # a cycle never hangs
    assert disk_full_cause(loop) is None


# ---------------------------------------------------------------- evidence


def test_oom_evidence_fires_only_when_the_counter_moved() -> None:
    counts = iter([7, 7])
    assert not OomEvidence.snapshot(lambda: next(counts)).fired()
    counts = iter([7, 8])
    assert OomEvidence.snapshot(lambda: next(counts)).fired()
    assert not OomEvidence.snapshot(lambda: None).fired()
    counts2 = iter([7, None])
    assert not OomEvidence.snapshot(lambda: next(counts2)).fired()


def test_oom_evidence_reads_the_real_counter_on_linux() -> None:
    import sys

    evidence = OomEvidence.snapshot()
    if sys.platform.startswith("linux") and Path("/proc/vmstat").exists():
        assert evidence.before is not None and evidence.before >= 0
    else:
        assert evidence.before is None and not evidence.fired()


# ----------------------------------------------------------------- executor


class _StderrRuntime(FakeRuntime):
    def __init__(self, stderr: bytes, *, env_hash: str | None = None) -> None:
        super().__init__(exit_code=1)
        self.stderr = stderr
        self.env_hash = env_hash

    async def run(
        self, spec: Any, on_output: Any, *, observer: RunObserver | None = None
    ) -> RunResult:
        assert observer is not None
        observer.enter_run()
        for start in range(0, len(self.stderr), 1000):  # arrives in pieces
            await on_output("stderr", self.stderr[start : start + 1000])
        observer.leave_run()
        return RunResult(
            exit_code=1,
            failure_type=FailureType.USER_ERROR,
            detail="exit code 1",
            env_hash=self.env_hash,
        )


@pytest.mark.asyncio
async def test_the_executor_reports_vram_oom_from_the_stderr_tail(
    tmp_path: Path,
) -> None:
    noise = b"x" * (2 * STDERR_TAIL_BYTES)  # the marker is in the tail only
    runtime = _StderrRuntime(noise + b"\ntorch.OutOfMemoryError: CUDA out of memory\n")
    reports = Reports()
    executor, digest = _executor(tmp_path, runtime, reports)
    executor.handle(_start(digest))
    await _settle(executor)
    final = reports.sent[-1][1]
    assert failure_type_from_proto(final.failure.failure_type) is FailureType.VRAM_OOM
    assert final.failure.retryable  # another node may have more VRAM


@pytest.mark.asyncio
async def test_a_marker_scrolled_out_of_the_tail_is_not_used(tmp_path: Path) -> None:
    runtime = _StderrRuntime(
        b"CUDA out of memory\n" + b"y" * (STDERR_TAIL_BYTES + 10) + b"\n"
    )
    reports = Reports()
    executor, digest = _executor(tmp_path, runtime, reports)
    executor.handle(_start(digest))
    await _settle(executor)
    final = reports.sent[-1][1]
    assert failure_type_from_proto(final.failure.failure_type) is FailureType.USER_ERROR


@pytest.mark.asyncio
async def test_the_terminal_report_carries_the_env_hash(tmp_path: Path) -> None:
    runtime = _StderrRuntime(b"boom\n", env_hash="ab" * 32)
    reports = Reports()
    executor, digest = _executor(tmp_path, runtime, reports)
    executor.handle(_start(digest))
    await _settle(executor)
    assert reports.sent[-1][1].env_hash == "ab" * 32
    assert all(s.env_hash == "" for _, s in reports.sent[:-1])  # terminal only


@pytest.mark.asyncio
async def test_enospc_anywhere_in_the_executor_is_disk_full(tmp_path: Path) -> None:
    async def full_disk(_base: str, _digest: Any, _token: str, dest: Path) -> int:
        raise OSError(errno.ENOSPC, "No space left on device", str(dest))

    reports = Reports()
    executor, digest = _executor(tmp_path, FakeRuntime(), reports, fetch=full_disk)
    executor.handle(_start(digest))
    await _settle(executor)
    final = reports.sent[-1][1]
    assert failure_type_from_proto(final.failure.failure_type) is FailureType.DISK_FULL
    assert "No space left on device" in final.failure.message

    # any other crash is still internal_error
    async def broken(_base: str, _digest: Any, _token: str, dest: Path) -> int:
        raise OSError(errno.EIO, "I/O error")

    reports2 = Reports()
    (tmp_path / "b").mkdir()
    executor2, digest2 = _executor(
        tmp_path / "b", FakeRuntime(), reports2, fetch=broken
    )
    executor2.handle(_start(digest2))
    await _settle(executor2)
    final2 = reports2.sent[-1][1]
    assert (
        failure_type_from_proto(final2.failure.failure_type)
        is FailureType.INTERNAL_ERROR
    )
    shutil.rmtree(tmp_path / "b", ignore_errors=True)
