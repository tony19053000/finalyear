"""V0.7 G6 fix round 2 (D163, the T-14 audit): what a worker thread creates
after its caller was cancelled is still cleaned up.

Each test holds the thread BEFORE it creates anything, cancels the caller,
then lets the thread go: a cleanup that does not wait for the thread (or that
needs the call's result) leaves the resource behind.
"""

# ruff: noqa: ASYNC240
from __future__ import annotations

import asyncio
import functools
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig
from meshforge.agent.execution import executor as executor_module
from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.subprocess_runtime import SubprocessRuntime
from meshforge.agent.threads import settled, uninterrupted

# The agent's own uid:gid: a non-root agent refuses any other run_as
# (prepare_workdir), and CI runners are not uid 1000.
_MY_UID_GID = "1000:1000" if sys.platform == "win32" else f"{os.getuid()}:{os.getgid()}"

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

DIGEST = "cd" * 32


class _Gate:
    """Hold a worker thread at its first line."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def hold(self) -> None:
        self.entered.set()
        assert self.release.wait(30)

    async def cancel_inside(self, task: asyncio.Task[Any]) -> None:
        """Cancel ``task`` while its thread is held, then let the thread go."""
        assert await asyncio.to_thread(self.entered.wait, 30)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done(), "the caller must wait for the thread it started"
        self.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_uninterrupted_ends_only_when_the_thread_has_and_then_cancels() -> None:
    gate = _Gate()
    made: list[int] = []

    def work() -> int:
        gate.hold()
        made.append(1)
        return 7

    task = asyncio.create_task(uninterrupted(asyncio.to_thread(work)))
    await gate.cancel_inside(task)
    assert made == [1]  # the thread had finished when the cancel came out
    assert await uninterrupted(asyncio.to_thread(lambda: 5)) == 5


async def test_settled_waits_and_swallows_the_outcome() -> None:
    async def boom() -> None:
        await asyncio.sleep(0.01)
        raise OSError("x")

    failing = asyncio.ensure_future(boom())
    await settled(failing)
    assert failing.done()
    cancelled = asyncio.ensure_future(asyncio.sleep(30))
    cancelled.cancel()
    await settled(cancelled)
    assert cancelled.cancelled()


class _Cgroups:
    def __init__(self, parent: Path, gate: _Gate) -> None:
        self.parent = parent
        self.gate = gate
        self.removed: list[Path] = []

    def path_for(self, attempt_id: str) -> Path:
        return self.parent / f"mf-{attempt_id}"

    def create(self, attempt_id: str, _cpus: float) -> Path:
        self.gate.hold()
        path = self.path_for(attempt_id)
        path.mkdir()
        return path

    def remove(self, path: Path) -> bool:
        self.removed.append(path)
        if path.is_dir():
            path.rmdir()
        return True


async def test_a_cancel_during_the_cgroup_create_removes_the_cgroup(
    tmp_path: Path,
) -> None:
    gate = _Gate()
    cgroups = _Cgroups(tmp_path, gate)
    runtime = SubprocessRuntime(cpu_cgroups=cgroups)  # type: ignore[arg-type]
    spec: Any = SimpleNamespace(attempt_id="att1", limits=SimpleNamespace(cpus=0.5))
    task = asyncio.create_task(runtime._job_cgroup(spec))
    await gate.cancel_inside(task)
    assert cgroups.removed == [tmp_path / "mf-att1"]
    assert list(tmp_path.iterdir()) == []


async def test_a_cancel_during_the_requirements_write_removes_the_staging_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = EnvBuilder(EnvsConfig(root=tmp_path / "envs"), uv_argv=("uv",))
    gate = _Gate()
    real = EnvBuilder._write_requirements

    def slow(self: EnvBuilder, digest: str, requirements: bytes) -> Path:
        gate.hold()
        return real(self, digest, requirements)

    monkeypatch.setattr(EnvBuilder, "_write_requirements", slow)
    task = asyncio.create_task(builder._staged_requirements(DIGEST, b"six==1.16.0\n"))
    await gate.cancel_inside(task)
    assert not builder.staging_dir(DIGEST).exists()
    # the happy path still hands the file over
    gate.entered.clear()
    path = await builder._staged_requirements(DIGEST, b"six==1.16.0\n")
    assert path.read_bytes() == b"six==1.16.0\n"


async def test_a_cancel_during_the_docker_requirements_staging_removes_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = EnvBuilder(EnvsConfig(root=tmp_path / "envs"), uv_argv=("uv",))
    runtime = DockerRuntime(
        DockerConfig(agent_label="node_x", run_as=_MY_UID_GID), envs=builder
    )
    gate = _Gate()
    real = DockerRuntime._stage_requirements

    def slow(self: DockerRuntime, digest: str, requirements: bytes) -> Path:
        gate.hold()
        return real(self, digest, requirements)

    monkeypatch.setattr(DockerRuntime, "_stage_requirements", slow)
    nothing: Any = None
    task = asyncio.create_task(
        runtime._build_env_image(
            nothing, nothing, "base", DIGEST, b"six==1.16.0\n", nothing, nothing, 1e18
        )
    )
    await gate.cancel_inside(task)
    assert not builder.staging_dir(DIGEST).exists()


async def test_a_stop_during_output_packing_leaves_no_archive_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Executor ``_collect``: the packing thread creates ``<attempt>.outputs``
    after the stopped step's ``finally`` has unlinked it."""
    Executor, stopped = executor_module.Executor, executor_module._StoppedError
    workdir = tmp_path / "work" / "att1"
    workdir.mkdir(parents=True)
    (workdir / "out.txt").write_text("result")
    real = executor_module.collect_outputs
    gate = _Gate()
    packed = threading.Event()

    def late(root: Path, patterns: Any, dest: Path, **kwargs: Any) -> Any:
        gate.hold()
        result = real(root, patterns, dest, **kwargs)
        assert dest.is_file()  # the archive exists, after the stop
        packed.set()
        return result

    monkeypatch.setattr(executor_module, "collect_outputs", late)
    fake: Any = SimpleNamespace(work_root=tmp_path / "work")
    fake._step = functools.partial(Executor._step, fake)
    attempt: Any = SimpleNamespace(
        attempt_id="att1", job_id="job1", workdir=workdir, stop_reason=None, step=None
    )
    plan: Any = SimpleNamespace(
        manifest=SimpleNamespace(outputs=("out.txt",)),
        upload_max_bytes=0,
        upload_token="",
    )
    task = asyncio.create_task(Executor._collect(fake, attempt, plan))
    assert await asyncio.to_thread(gate.entered.wait, 30)
    attempt.stop_reason = "cancel"  # what Executor._stop does
    attempt.step.cancel()
    with pytest.raises(stopped):
        await task  # the stop is not held up by the packing
    gate.release.set()
    assert await asyncio.to_thread(packed.wait, 30)
    for _ in range(200):
        if not list((tmp_path / "work").glob("*.outputs")):
            break
        await asyncio.sleep(0.01)
    assert list((tmp_path / "work").glob("*.outputs")) == []
