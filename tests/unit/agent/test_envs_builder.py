"""V0.5 G2 (D86): build-once, crash-safety, bounds and read-only of the env builder.

The build step here is an in-process coroutine writing files, so these tests check
the builder's own guarantees (lock, marker, cleanup, caps) without a real ``uv``;
``tests/component/agent/test_envs_build.py`` runs real ``uv`` builds and the
two-process / kill-mid-build cases.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from meshforge.agent.envs import EnvError
from meshforge.agent.envs.builder import MARKER_NAME, EnvBuilder, EnvsConfig
from meshforge.agent.envs.fsutil import env_lock, remove_tree
from meshforge.agent.settings import AgentSettings, EnvsSettings
from meshforge.common.errors import FailureType

pytestmark = pytest.mark.unit

DIGEST = "ab" * 32
FAR = 1e18  # a deadline that never passes


def _builder(tmp_path: Path, **over: object) -> EnvBuilder:
    free = over.pop("free", 10**15)
    over.setdefault("free_disk_floor_bytes", 0)
    config = EnvsConfig(root=tmp_path / "envs", **over)  # type: ignore[arg-type]
    return EnvBuilder(config, uv_argv=("uv-not-used",), disk_free=lambda _p: free)  # type: ignore[arg-type,return-value]


def _write_tree(target: Path, data: bytes) -> None:
    (target / "lib").mkdir(parents=True)
    (target / "lib" / "mod.py").write_bytes(data)


class Recorder:
    """A build step that writes ``size`` bytes and counts its calls."""

    def __init__(self, size: int = 10, *, gate: asyncio.Event | None = None) -> None:
        self.size = size
        self.calls = 0
        self.gate = gate
        self.started = asyncio.Event()

    async def __call__(self, target: Path) -> None:
        self.calls += 1
        _write_tree(target, b"x" * self.size)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()


def _visible(builder: EnvBuilder, kind: str = "venv") -> list[str]:
    folder = builder.root / kind
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# ------------------------------------------------------------- build once
@pytest.mark.asyncio
async def test_first_call_builds_second_call_hits(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    step = Recorder()
    first = await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    second = await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    assert (first.cache, second.cache) == ("built", "hit")
    assert step.calls == 1
    assert first.path == second.path == builder.env_dir("venv", DIGEST)
    assert first.size_bytes == 10
    assert builder.is_ready("venv", DIGEST)
    assert _visible(builder) == [DIGEST]


@pytest.mark.asyncio
async def test_concurrent_requests_for_one_hash_build_once(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    gate = asyncio.Event()
    step = Recorder(gate=gate)
    one = asyncio.create_task(
        builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    )
    await step.started.wait()
    two = asyncio.create_task(
        builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    )
    await asyncio.sleep(0.02)
    assert not two.done()  # waiting on the lock, not building
    gate.set()
    results = await asyncio.gather(one, two)
    assert sorted(r.cache for r in results) == ["built", "waited"]
    assert step.calls == 1


@pytest.mark.asyncio
async def test_different_hashes_do_not_wait_for_each_other(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    gate = asyncio.Event()
    slow = Recorder(gate=gate)
    one = asyncio.create_task(
        builder.ensure_dir_env("venv", DIGEST, slow, deadline=FAR)
    )
    await slow.started.wait()
    other = await builder.ensure_dir_env("venv", "cd" * 32, Recorder(), deadline=FAR)
    assert other.cache == "built"
    gate.set()
    await one


@pytest.mark.asyncio
async def test_lock_wait_past_the_deadline_is_env_timeout(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    builder.lock_path(DIGEST).parent.mkdir(parents=True)
    async with env_lock(builder.lock_path(DIGEST), deadline=FAR):
        with pytest.raises(EnvError) as info:
            await builder.ensure_dir_env("venv", DIGEST, Recorder(), deadline=0.0)
    assert info.value.reason == "env_timeout"
    assert info.value.failure_type is FailureType.TIMEOUT


# ------------------------------------------------------- failure leaves nothing
@pytest.mark.asyncio
async def test_failed_build_leaves_nothing_and_the_next_one_rebuilds(
    tmp_path: Path,
) -> None:
    builder = _builder(tmp_path)

    async def broken(target: Path) -> None:
        _write_tree(target, b"half")
        raise EnvError("requirements install failed (exit 1)", reason="setup_failed")

    with pytest.raises(EnvError) as info:
        await builder.ensure_dir_env("venv", DIGEST, broken, deadline=FAR)
    assert info.value.failure_type is FailureType.ENV_ERROR
    assert _visible(builder) == []
    step = Recorder()
    again = await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    assert again.cache == "built" and step.calls == 1


@pytest.mark.asyncio
async def test_cancelled_build_leaves_nothing(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    step = Recorder(gate=asyncio.Event())  # never released
    task = asyncio.create_task(
        builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    )
    await step.started.wait()
    assert _visible(builder) == [f"{DIGEST}.building"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _visible(builder) == []
    # the lock was released with the task: a new build is not blocked
    rebuilt = await builder.ensure_dir_env("venv", DIGEST, Recorder(), deadline=1e18)
    assert rebuilt.cache == "built"


@pytest.mark.asyncio
async def test_crash_leftovers_are_cleared_before_a_rebuild(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    final = builder.env_dir("venv", DIGEST)
    building = final.with_name(f"{DIGEST}.building")
    building.mkdir(parents=True)
    (building / "stale.py").write_bytes(b"from a killed build")
    final.mkdir()  # a damaged env: no marker
    (final / "partial.py").write_bytes(b"?")
    assert not builder.is_ready("venv", DIGEST)
    built = await builder.ensure_dir_env("venv", DIGEST, Recorder(), deadline=FAR)
    assert built.cache == "built"
    assert not (final / "partial.py").exists()
    assert _visible(builder) == [DIGEST]


@pytest.mark.asyncio
async def test_a_marker_for_another_hash_is_not_ready(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    await builder.ensure_dir_env("venv", DIGEST, Recorder(), deadline=FAR)
    other = builder.env_dir("venv", "ef" * 32)
    os.replace(builder.env_dir("venv", DIGEST), other)
    assert not builder.is_ready("venv", "ef" * 32)
    assert not builder.is_ready("subprocess", DIGEST)


# -------------------------------------------------------------- disk bounds
@pytest.mark.asyncio
async def test_free_disk_floor_exactly_met_is_accepted(tmp_path: Path) -> None:
    builder = _builder(tmp_path, free=1000, free_disk_floor_bytes=1000)
    built = await builder.ensure_dir_env("venv", DIGEST, Recorder(), deadline=FAR)
    assert built.cache == "built"


@pytest.mark.asyncio
async def test_free_disk_one_byte_under_the_floor_is_disk_full(tmp_path: Path) -> None:
    builder = _builder(tmp_path, free=999, free_disk_floor_bytes=1000)
    step = Recorder()
    with pytest.raises(EnvError) as info:
        await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    assert info.value.reason == "disk_full"
    assert info.value.failure_type is FailureType.DISK_FULL
    assert step.calls == 0
    assert _visible(builder) == []


@pytest.mark.asyncio
async def test_size_cap_max_accepted(tmp_path: Path) -> None:
    builder = _builder(tmp_path, max_env_bytes=100)
    built = await builder.ensure_dir_env("venv", DIGEST, Recorder(100), deadline=FAR)
    assert built.size_bytes == 100


@pytest.mark.asyncio
async def test_size_cap_max_plus_one_rejected_and_removed(tmp_path: Path) -> None:
    builder = _builder(tmp_path, max_env_bytes=100)
    with pytest.raises(EnvError) as info:
        await builder.ensure_dir_env("venv", DIGEST, Recorder(101), deadline=FAR)
    assert info.value.reason == "env_too_large"
    assert info.value.failure_type is FailureType.ENV_ERROR
    assert _visible(builder) == []


# ---------------------------------------------------------------- read-only
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permission bits"
)
@pytest.mark.asyncio
async def test_a_built_env_is_read_only(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    built = await builder.ensure_dir_env("venv", DIGEST, Recorder(), deadline=FAR)
    with pytest.raises(PermissionError):
        (built.path / "lib" / "mod.py").write_bytes(b"poisoned")
    with pytest.raises(PermissionError):
        (built.path / "lib" / "new.py").write_bytes(b"planted")
    with pytest.raises(PermissionError):
        (built.path / MARKER_NAME).write_bytes(b"{}")
    # and the agent can still remove it (G5 GC, a manual cleanup)
    remove_tree(built.path)
    assert not built.path.exists()


# ------------------------------------------------------------- requirements
def _project(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    return work


def test_read_requirements_normalises(tmp_path: Path) -> None:
    work = _project(tmp_path)
    (work / "req.txt").write_bytes(b"b==1\n# c\na==2\n")
    assert _builder(tmp_path).read_requirements(work, "req.txt") == b"a==2\nb==1\n"


def test_read_requirements_size_max_and_max_plus_one(tmp_path: Path) -> None:
    work = _project(tmp_path)
    (work / "ok.txt").write_bytes(b"a" * 49 + b"\n")
    (work / "big.txt").write_bytes(b"a" * 50 + b"\n")
    builder = _builder(tmp_path, max_requirements_bytes=50)
    assert builder.read_requirements(work, "ok.txt") == b"a" * 49 + b"\n"
    with pytest.raises(EnvError) as info:
        builder.read_requirements(work, "big.txt")
    assert info.value.reason == "requirements_rejected"


def test_read_requirements_line_bound(tmp_path: Path) -> None:
    work = _project(tmp_path)
    (work / "r.txt").write_bytes(b"a\nb\nc\n")
    assert _builder(tmp_path, max_requirements_lines=3).read_requirements(work, "r.txt")
    with pytest.raises(EnvError, match="more than 2"):
        _builder(tmp_path, max_requirements_lines=2).read_requirements(work, "r.txt")


def test_missing_requirements_file(tmp_path: Path) -> None:
    with pytest.raises(EnvError) as info:
        _builder(tmp_path).read_requirements(_project(tmp_path), "nope.txt")
    assert info.value.reason == "requirements_missing"


def test_requirements_directory_is_refused(tmp_path: Path) -> None:
    work = _project(tmp_path)
    (work / "dir").mkdir()
    with pytest.raises(EnvError) as info:
        _builder(tmp_path).read_requirements(work, "dir")
    assert info.value.reason == "requirements_missing"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges there")
def test_requirements_symlink_out_of_the_project_is_refused(tmp_path: Path) -> None:
    work = _project(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"agent-only\n")
    (work / "req.txt").symlink_to(secret)
    with pytest.raises(EnvError) as info:
        _builder(tmp_path).read_requirements(work, "req.txt")
    assert info.value.reason == "requirements_missing"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges there")
def test_requirements_symlink_inside_the_project_is_fine(tmp_path: Path) -> None:
    work = _project(tmp_path)
    (work / "real.txt").write_bytes(b"a\n")
    (work / "req.txt").symlink_to(work / "real.txt")
    assert _builder(tmp_path).read_requirements(work, "req.txt") == b"a\n"


# ---------------------------------------------------------- uv and settings
@pytest.mark.asyncio
async def test_no_uv_binary_is_uv_missing(tmp_path: Path) -> None:
    builder = EnvBuilder(EnvsConfig(root=tmp_path / "envs"), uv_argv=())
    with pytest.raises(EnvError) as info:
        await builder.ensure_target(requirements=b"a\n", deadline=FAR)
    assert info.value.reason == "uv_missing"
    assert info.value.failure_type is FailureType.ENV_ERROR


def test_config_from_settings(tmp_path: Path) -> None:
    settings = AgentSettings.model_validate(
        {
            "data_dir": tmp_path / "data",
            "agent": {
                "controller_address": "127.0.0.1:47100",
                "envs": {
                    "free_disk_floor_mb": 3,
                    "find_links": str(tmp_path / "wheels"),
                    "no_index": True,
                    "index_url": "https://mirror.example/simple",
                },
            },
        }
    )
    config = EnvsConfig.from_settings(settings)
    assert config.root == tmp_path / "data" / "envs"
    assert config.free_disk_floor_bytes == 3 * 1024 * 1024
    assert config.index_options() == [
        "--index-url",
        "https://mirror.example/simple",
        "--find-links",
        str(tmp_path / "wheels"),
        "--no-index",
    ]


def test_envs_dir_setting_overrides_the_default(tmp_path: Path) -> None:
    settings = AgentSettings.model_validate(
        {
            "data_dir": tmp_path / "data",
            "agent": {
                "controller_address": "127.0.0.1:1",
                "envs": {"dir": str(tmp_path / "e")},
            },
        }
    )
    assert settings.envs_dir == tmp_path / "e"


@pytest.mark.parametrize(
    "bad",
    [
        {"no_index": True},
        {"index_url": "https://user:pw@mirror.example/simple"},
        {"index_url": "ftp://mirror.example/simple"},
        {"index_url": "https://mirror.example/" + "a" * 300},
        {"max_env_bytes": 0},
        {"max_requirements_bytes": 1024 * 1024 + 1},
        {"free_disk_floor_mb": -1},
    ],
)
def test_bad_envs_settings_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EnvsSettings(**bad)  # type: ignore[arg-type]


def test_blank_index_url_means_pypi() -> None:
    assert EnvsSettings(index_url="  ").index_url is None


# ------------------------------------------------------ integrity summary (D1)
@pytest.mark.asyncio
async def test_bytecode_written_into_an_env_does_not_make_it_damaged(
    tmp_path: Path,
) -> None:
    # Windows ignores the read-only bit on folders: an interpreter may add
    # __pycache__ there. That must not turn every later job into a rebuild.
    builder = _builder(tmp_path)
    step = Recorder()
    built = await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    cache = built.path / "lib" / "__pycache__"
    os.chmod(built.path / "lib", 0o700)  # what Windows effectively allows
    cache.mkdir()
    (cache / "mod.cpython-312.pyc").write_bytes(b"\x00" * 50)
    assert builder.env_state("venv", DIGEST) == "ready"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
async def test_an_added_file_is_damaged_then_evicted_and_rebuilt(
    tmp_path: Path,
) -> None:
    builder = _builder(tmp_path)
    step = Recorder()
    built = await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    os.chmod(built.path / "lib", 0o700)
    (built.path / "lib" / "planted.py").write_bytes(b"x")
    assert builder.env_state("venv", DIGEST) == "damaged"
    again = await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    assert again.cache == "built" and step.calls == 2
    assert not (again.path / "lib" / "planted.py").exists()
    assert builder.env_state("venv", DIGEST) == "ready"


def test_a_marker_without_a_summary_is_damaged(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    final = builder.env_dir("venv", DIGEST)
    final.mkdir(parents=True)
    (final / MARKER_NAME).write_text(
        '{"v": 1, "env_hash": "' + DIGEST + '", "kind": "venv"}'
    )
    assert builder.env_state("venv", DIGEST) == "damaged"
