# ruff: noqa: T201, ASYNC240
"""V0.7 G6 tester r3 (T-14 audit): cancel at each audited thread hand-off, with
the thread held AFTER it created its resource (the implementer's unit tests hold
it before), then released; nothing may be left. Plus the two sites the
implementer called "benign, by reading": `EnvBuilder._seal` against the
shielded remove, and the Docker `container.start`.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mf_envs_support import make_wheel

from meshforge.agent.envs import builder as builder_module
from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig, default_uv_argv
from meshforge.agent.envs.errors import EnvError
from meshforge.agent.execution import executor as executor_module
from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.subprocess_runtime import SubprocessRuntime

pytestmark = [
    pytest.mark.component,
    pytest.mark.asyncio,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX"),
]


class Hold:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def park(self) -> None:
        self.entered.set()
        assert self.release.wait(60)

    async def cancel_while_parked(self, task: asyncio.Task[Any]) -> None:
        assert await asyncio.to_thread(self.entered.wait, 30)
        task.cancel()
        await asyncio.sleep(0.2)
        self.release.set()
        with pytest.raises((asyncio.CancelledError, EnvError)):
            await asyncio.wait_for(task, 30)


async def test_cgroup_create_then_cancel_leaves_no_cgroup(tmp_path: Path) -> None:
    hold = Hold()
    removed: list[Path] = []

    class Cgroups:
        def path_for(self, attempt_id: str) -> Path:
            return tmp_path / f"mf-{attempt_id}"

        def create(self, attempt_id: str, _cpus: float) -> Path:
            path = self.path_for(attempt_id)
            path.mkdir()  # created FIRST ...
            hold.park()  # ... and only then the thread is slow
            return path

        def remove(self, path: Path) -> bool:
            removed.append(path)
            if path.is_dir():
                path.rmdir()
            return True

    runtime = SubprocessRuntime(cpu_cgroups=Cgroups())  # type: ignore[arg-type]
    spec: Any = SimpleNamespace(attempt_id="att9", limits=SimpleNamespace(cpus=0.5))
    task = asyncio.create_task(runtime._job_cgroup(spec))
    await hold.cancel_while_parked(task)
    assert [p.name for p in tmp_path.iterdir()] == []


async def test_requirements_staging_created_then_cancelled_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = EnvBuilder(EnvsConfig(root=tmp_path / "envs"), uv_argv=("uv",))
    hold = Hold()
    real = EnvBuilder._write_requirements

    def slow(self: EnvBuilder, digest: str, requirements: bytes) -> Path:
        path = real(self, digest, requirements)  # file exists ...
        hold.park()  # ... then the thread lingers
        return path

    monkeypatch.setattr(EnvBuilder, "_write_requirements", slow)
    digest = "ab" * 32
    task = asyncio.create_task(builder._staged_requirements(digest, b"x==1\n"))
    await hold.cancel_while_parked(task)
    assert not builder.staging_dir(digest).exists()
    assert not list((tmp_path / "envs").rglob("requirements.txt"))


async def test_docker_staging_created_then_cancelled_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = EnvBuilder(EnvsConfig(root=tmp_path / "envs"), uv_argv=("uv",))
    runtime = DockerRuntime(
        DockerConfig(agent_label="node_x", run_as="1000:1000"), envs=builder
    )
    hold = Hold()
    real = DockerRuntime._stage_requirements

    def slow(self: DockerRuntime, digest: str, requirements: bytes) -> Path:
        path = real(self, digest, requirements)
        hold.park()
        return path

    monkeypatch.setattr(DockerRuntime, "_stage_requirements", slow)
    nothing: Any = None
    digest = "cd" * 32
    task = asyncio.create_task(
        runtime._build_env_image(
            nothing, nothing, "base", digest, b"x==1\n", nothing, nothing, 1e18
        )
    )
    await hold.cancel_while_parked(task)
    assert not builder.staging_dir(digest).exists()
    assert not list((tmp_path / "envs").rglob("requirements.txt"))


@pytest.mark.skipif(not default_uv_argv(), reason="the uv wheel is not installed")
@pytest.mark.parametrize(
    "park_in", ["after_read_only", "before_marker", "during_remove"]
)
async def test_env_seal_against_the_shielded_remove_leaves_no_partial_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, park_in: str
) -> None:
    """Cancel while `_seal` runs in its thread; the shielded `remove_tree(building)`
    runs concurrently. Afterwards: no `.building`, and no env directory that is
    'ready' by its marker yet incomplete."""
    wheels = tmp_path / "wheels"
    make_wheel(wheels, "mfdemo", "1.0")
    builder = EnvBuilder(
        EnvsConfig(
            root=tmp_path / "envs",
            free_disk_floor_bytes=0,
            find_links=wheels,
            no_index=True,
            only_binary=True,
        )
    )
    hold = Hold()
    if park_in == "after_read_only":
        real_ro = builder_module.make_read_only

        def ro(path: Path) -> None:
            real_ro(path)
            hold.park()

        monkeypatch.setattr(builder_module, "make_read_only", ro)
    elif park_in == "before_marker":
        real_seal_summary = builder_module.tree_summary

        def summary(*a: Any, **k: Any) -> Any:
            out = real_seal_summary(*a, **k)
            hold.park()
            return out

        monkeypatch.setattr(builder_module, "tree_summary", summary)
    else:  # the remove itself is slow: seal runs while it deletes
        real_remove = builder_module.remove_tree
        calls = {"n": 0}

        def slow_remove(path: Path) -> None:
            calls["n"] += 1
            if path.name.endswith(".building") and hold.entered.is_set():
                time.sleep(0.5)  # seal's thread is released meanwhile
                hold.release.set()
            real_remove(path)

        real_ro = builder_module.make_read_only

        def ro(path: Path) -> None:
            real_ro(path)
            hold.park()

        monkeypatch.setattr(builder_module, "make_read_only", ro)
        monkeypatch.setattr(builder_module, "remove_tree", slow_remove)

    task = asyncio.create_task(
        builder.ensure_target(requirements=b"mfdemo==1.0\n", deadline=1e18)
    )
    assert await asyncio.to_thread(hold.entered.wait, 60)
    task.cancel()
    if park_in != "during_remove":
        await asyncio.sleep(0.2)
        hold.release.set()
    try:
        await asyncio.wait_for(task, 60)
        outcome = "completed"
    except asyncio.CancelledError:
        outcome = "cancelled"
    except EnvError as exc:
        outcome = f"EnvError:{exc.reason}"
    except TypeError as exc:
        # T-20 (see test_v07_g6_tester_r3_unreadable_dir.py): rmtree's error
        # hook retries os.open(path) without flags when the tree vanishes under
        # it; the TypeError replaces the CancelledError.
        outcome = f"TypeError:{exc}"
    except OSError as exc:
        # OBSERVATION (T-20, minor): the shielded remove of `.building` raised
        # FileNotFoundError because `_seal` renamed the tree under it; that
        # error replaces the CancelledError. No leftover results (asserted).
        outcome = f"OSError:{exc!r}"
    await asyncio.sleep(0.5)
    entries = sorted(
        p.name
        for p in (tmp_path / "envs").rglob("*")
        if p.parent.name in ("subprocess", "venv", "target")
    )
    print(park_in, outcome, entries)
    finals = [
        (
            f.parent.name,
            f.name,
            builder.env_state(f.parent.name, f.name),
            sorted(x.name for x in f.iterdir()),
        )
        for f in (tmp_path / "envs").glob("*/*")
        if f.is_dir() and not f.name.endswith(".building")
    ]
    print("   finals:", finals)
    assert not [e for e in entries if e.endswith(".building")], entries
    for final in (tmp_path / "envs").glob("*/*"):
        if final.is_dir() and not final.name.endswith(".evicted"):
            state = builder.env_state(final.parent.name, final.name)
            assert state in ("ready", "missing", "damaged"), state
            if state == "ready":
                assert (final / "mfdemo" / "__init__.py").exists(), final


async def test_output_archive_created_then_cancelled_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Executor `_collect`: the thread has written the archive when the stop comes."""
    import functools

    Executor, stopped = executor_module.Executor, executor_module._StoppedError
    workdir = tmp_path / "work" / "att1"
    workdir.mkdir(parents=True)
    (workdir / "out.txt").write_text("result" * 1000)
    real = executor_module.collect_outputs
    hold = Hold()

    def late(root: Path, patterns: Any, dest: Path, **kwargs: Any) -> Any:
        result = real(root, patterns, dest, **kwargs)  # archive exists now
        hold.park()
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
    assert await asyncio.to_thread(hold.entered.wait, 30)
    attempt.stop_reason = "cancel"
    attempt.step.cancel()
    with pytest.raises(stopped):
        await asyncio.wait_for(task, 10)
    hold.release.set()
    for _ in range(100):
        if not list((tmp_path / "work").glob("*.outputs")):
            break
        await asyncio.sleep(0.1)
    assert list((tmp_path / "work").glob("*.outputs")) == []
