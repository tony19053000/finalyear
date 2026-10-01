"""V0.5 G2 tester (D85-D87): env hash stability, refused requirement lines, bounds,
damaged/partial cached envs, ENOSPC mid-build, read-only, concurrency, base->runtime
selection. Derived from the roadmap criterion and D85-D87, not from the code."""

from __future__ import annotations

import asyncio
import errno
import itertools
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from meshforge.agent.envs import EnvError, EnvInputs, env_hash, normalize_requirements
from meshforge.agent.envs.builder import MARKER_NAME, EnvBuilder, EnvsConfig
from meshforge.agent.execution import docker_runtime as dr
from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.agent.execution.selection import GpuNeed, choose_runtime, execution_offer
from meshforge.common.errors import FailureType

pytestmark = pytest.mark.unit

FAR = 1e18
DIGEST = "cd" * 32


def _hash(reqs: bytes, **over: str) -> str:
    fields = {
        "runtime_kind": "venv",
        "base": "cpython-3.12.3",
        "python": "",
        "platform": "linux-x86_64",
    }
    fields.update(over)
    return env_hash(EnvInputs(requirements=normalize_requirements(reqs), **fields))


# ------------------------------------------------------------------ D85 hash
BASE_REQ = b"numpy==1.26.4\nrequests==2.31.0\n"


@pytest.mark.parametrize(
    "variant",
    [
        b"numpy==1.26.4\r\nrequests==2.31.0\r\n",
        b"numpy==1.26.4\rrequests==2.31.0\r",
        b"\xef\xbb\xbfnumpy==1.26.4\nrequests==2.31.0\n",
        b"\xef\xbb\xbfnumpy==1.26.4\r\nrequests==2.31.0",
        b"numpy==1.26.4   \t\nrequests==2.31.0  \n\n\n",
        b"  numpy==1.26.4\n\trequests==2.31.0\n",
        b"requests==2.31.0\nnumpy==1.26.4\n",
        b"numpy==1.26.4\nnumpy==1.26.4\nrequests==2.31.0\n",
        b"# pinned\nnumpy==1.26.4  # fast\nrequests==2.31.0\n",
        b"numpy==1.26.4\nrequests==\\\n2.31.0\n",
        b"numpy==1.26.4\r\nrequests==\\\r\n2.31.0\r\n",
        "numpy==1.26.4\u00a0\nrequests==2.31.0\n".encode(),
    ],
)
def test_hash_is_stable_across_cosmetic_differences(variant: bytes) -> None:
    assert _hash(variant) == _hash(BASE_REQ)


@pytest.mark.parametrize(
    "different",
    [
        b"numpy==1.26.5\nrequests==2.31.0\n",
        b"numpy==1.26.4\n",
        b"numpy==1.26.4\nrequests==2.31.0\nextra==1\n",
        b"numpy==1.26.4\nrequests==2.31.0 ; python_version<'3.13'\n",
    ],
)
def test_hash_changes_with_real_content(different: bytes) -> None:
    assert _hash(different) != _hash(BASE_REQ)


def test_hash_changes_with_every_other_input_and_all_are_distinct() -> None:
    hashes = {
        _hash(BASE_REQ),
        _hash(BASE_REQ, runtime_kind="subprocess"),
        _hash(BASE_REQ, runtime_kind="docker"),
        _hash(BASE_REQ, base="cpython-3.12.4"),
        _hash(BASE_REQ, python="3.12"),
        _hash(BASE_REQ, platform="win-amd64"),
        _hash(BASE_REQ, platform="linux-aarch64"),
    }
    assert len(hashes) == 7
    assert all(len(h) == 64 and h == h.lower() for h in hashes)


def test_package_name_case_is_deterministic_and_never_collides_wrongly() -> None:
    # Different spelling may (conservatively) be a different env; it must at least
    # be deterministic and never raise.
    assert _hash(b"Requests==2.0\n") == _hash(b"Requests==2.0\r\n")
    assert _hash(b"Requests==2.0\n") != _hash(b"Requests==2.1\n")


def test_empty_and_comment_only_requirements_hash_alike() -> None:
    assert _hash(b"") == _hash(b"# nothing\n\n   \n") == _hash(b"\xef\xbb\xbf")


@pytest.mark.parametrize("bad", ["", "é", "a\nb", "x" * 257])
def test_env_inputs_validate_fields(bad: str) -> None:
    with pytest.raises(ValueError):
        EnvInputs("venv", bad or "", "", b"", "linux")
    with pytest.raises(ValueError):
        EnvInputs("venv", "cpython-3.12", "", b"", bad)


# ------------------------------------------------------------ refused lines
@pytest.mark.parametrize(
    "line",
    [
        "-r other.txt",
        "-rother.txt",
        "-r\tother.txt",
        "--requirement=other.txt",
        "--requirement other.txt",
        "-c constraints.txt",
        "--constraint=c.txt",
        "-e .",
        "-e.",
        "--editable ./pkg",
        "./pkg",
        "../pkg",
        "/abs/pkg",
        "~/pkg",
        "C:\\pkgs\\a",
        "c:/pkgs/a",
        "\\\\server\\share\\a",
        "file:///tmp/a.whl",
        "FILE:///tmp/a.whl",
        "pkg @ file:///tmp/a.whl",
        "pkg@FILE:///tmp/a.whl",
        "pkg @   file:///tmp/a.whl",
    ],
)
def test_unhashable_lines_are_refused_anywhere_in_the_file(line: str) -> None:
    for text in (
        line + "\n",
        f"numpy==1\n{line}\nrequests==2\n",
        f"numpy==1\r\n{line}\r\n",
        "\ufeff" + line,
    ):
        with pytest.raises(EnvError) as info:
            normalize_requirements(text.encode())
        assert info.value.reason == "requirements_rejected"


def test_refused_line_hidden_behind_a_continuation_is_still_refused() -> None:
    with pytest.raises(EnvError):
        normalize_requirements(b"-r \\\nother.txt\n")
    with pytest.raises(EnvError):
        normalize_requirements(b"-\\\nr other.txt\n")


def test_an_include_inside_a_comment_is_not_a_line() -> None:
    assert normalize_requirements(b"numpy==1 # -r other.txt\n# -e .\n") == b"numpy==1\n"


@pytest.mark.parametrize("raw", [b"a\x00b\n", b"\xff\xfe=\n", b"a\x1bb\n"])
def test_binary_and_non_utf8_are_refused(raw: bytes) -> None:
    with pytest.raises(EnvError) as info:
        normalize_requirements(raw)
    assert info.value.reason == "requirements_rejected"


def test_utf16_file_is_refused_not_misread() -> None:
    with pytest.raises(EnvError):
        normalize_requirements("numpy==1\n".encode("utf-16"))


# ------------------------------------------------------------------ bounds
def test_byte_bound_max_ok_and_plus_one_refused() -> None:
    line = "a" * 99 + "\n"
    body = (line * 10)[: 1000 - 1] + "\n"
    assert len(body) == 1000
    normalize_requirements(body.encode(), max_bytes=1000)
    with pytest.raises(EnvError):
        normalize_requirements(body.encode() + b"\n", max_bytes=1000)


def test_byte_bound_counts_bytes_not_characters() -> None:
    text = ("é" * 5).encode()  # 10 bytes, 5 chars
    assert len(text) == 10
    with pytest.raises(EnvError):
        normalize_requirements(text, max_bytes=9)
    normalize_requirements(text, max_bytes=10)


def test_line_bound_max_ok_and_plus_one_refused_duplicates_free() -> None:
    lines = [f"pkg{i}==1" for i in range(50)]
    normalize_requirements("\n".join(lines).encode(), max_lines=50)
    with pytest.raises(EnvError):
        normalize_requirements("\n".join([*lines, "pkg50==1"]).encode(), max_lines=50)
    # duplicates / comments / blanks do not count towards the bound
    noisy = "\n".join([*lines, *lines, "# c", "", "   "])
    normalize_requirements(noisy.encode(), max_lines=50)


# -------------------------------------------------------- builder fixtures
def _builder(tmp_path: Path, **over: object) -> EnvBuilder:
    over.setdefault("free_disk_floor_bytes", 0)
    config = EnvsConfig(root=tmp_path / "envs", **over)  # type: ignore[arg-type]
    return EnvBuilder(config, uv_argv=("uv-not-used",), disk_free=lambda _p: 10**15)


class Step:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, target: Path) -> None:
        self.calls += 1
        (target / "lib").mkdir(parents=True)
        (target / "lib" / "mod.py").write_bytes(b"VALUE = 1\n")
        (target / "lib" / "other.py").write_bytes(b"VALUE = 2\n")


def _unlock(root: Path) -> None:
    for dirpath, dirnames, _files in os.walk(root):
        for name in dirnames:
            os.chmod(os.path.join(dirpath, name), 0o700)
    os.chmod(root, 0o700)


# ------------------------------------------------- damaged / partial caches
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    ["marker_missing", "marker_empty", "marker_truncated", "marker_dir", "wrong_kind"],
)
async def test_a_damaged_marker_is_never_a_hit_and_is_rebuilt(
    tmp_path: Path, damage: str
) -> None:
    builder, step = _builder(tmp_path), Step()
    await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    final = builder.env_dir("venv", DIGEST)
    _unlock(final)
    marker = final / MARKER_NAME
    marker.chmod(0o600)
    if damage == "marker_missing":
        marker.unlink()
    elif damage == "marker_empty":
        marker.write_bytes(b"")
    elif damage == "marker_truncated":
        marker.write_bytes(marker.read_bytes()[:10])
    elif damage == "marker_dir":
        marker.unlink()
        marker.mkdir()
    else:
        marker.write_text(json.dumps({"env_hash": DIGEST, "kind": "subprocess"}))
    assert not builder.is_ready("venv", DIGEST)
    again = await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    assert again.cache == "built" and step.calls == 2
    assert builder.is_ready("venv", DIGEST)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="chmod semantics")
async def test_an_env_with_a_deleted_file_is_not_used_silently(tmp_path: Path) -> None:
    """A valid marker but a file missing (disk damage, an operator's rm, a job that
    chmod'ed it back): the cache must not serve it as a plain hit."""
    builder, step = _builder(tmp_path), Step()
    await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    final = builder.env_dir("venv", DIGEST)
    _unlock(final)
    (final / "lib" / "mod.py").unlink()
    again = await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    assert again.cache != "hit", "a damaged env was served as a cache hit"
    assert (final / "lib" / "mod.py").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="chmod semantics")
async def test_an_env_with_a_modified_file_size_is_not_used_silently(
    tmp_path: Path,
) -> None:
    builder, step = _builder(tmp_path), Step()
    await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    final = builder.env_dir("venv", DIGEST)
    _unlock(final)
    f = final / "lib" / "mod.py"
    f.chmod(0o600)
    f.write_bytes(b"")  # truncated
    again = await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    assert again.cache != "hit"


@pytest.mark.asyncio
async def test_a_stray_building_dir_and_stale_final_without_marker_rebuild(
    tmp_path: Path,
) -> None:
    builder, step = _builder(tmp_path), Step()
    (builder.env_dir("venv", DIGEST + "x").parent).mkdir(parents=True)
    final = builder.env_dir("venv", DIGEST)
    final.mkdir(parents=True)
    (final / "half.py").write_text("partial")
    building = final.with_name(f"{DIGEST}.building")
    building.mkdir()
    (building / "junk").write_text("x")
    built = await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    assert built.cache == "built"
    assert not (final / "half.py").exists() and not building.exists()


# ------------------------------------------------------- disk / size limits
@pytest.mark.asyncio
async def test_enospc_during_the_build_cleans_up_and_is_typed(tmp_path: Path) -> None:
    builder = _builder(tmp_path)

    async def full_disk(target: Path) -> None:
        (target / "lib").mkdir(parents=True)
        (target / "lib" / "half.py").write_bytes(b"x" * 100)
        raise OSError(errno.ENOSPC, "No space left on device")

    with pytest.raises(BaseException) as info:
        await builder.ensure_dir_env("venv", DIGEST, full_disk, deadline=FAR)
    assert not builder.env_dir("venv", DIGEST).exists()
    assert not builder.env_dir("venv", DIGEST + ".building").exists()
    assert not list(builder.env_dir("venv", DIGEST).parent.glob("*.building"))
    # the failure is the typed disk_full, not a raw OSError escaping to the runtime
    assert isinstance(info.value, EnvError), type(info.value)
    assert info.value.failure_type is FailureType.DISK_FULL
    # and the next attempt builds cleanly
    step = Step()
    assert (await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)).cache == (
        "built"
    )


@pytest.mark.asyncio
async def test_floor_is_checked_per_build_and_failing_it_leaves_nothing(
    tmp_path: Path,
) -> None:
    free = [10**9]
    config = EnvsConfig(root=tmp_path / "envs", free_disk_floor_bytes=10**6)
    builder = EnvBuilder(config, uv_argv=("u",), disk_free=lambda _p: free[0])
    step = Step()
    await builder.ensure_dir_env("venv", "a" * 64, step, deadline=FAR)
    free[0] = 10**6 - 1
    with pytest.raises(EnvError) as info:
        await builder.ensure_dir_env("venv", "b" * 64, step, deadline=FAR)
    assert info.value.reason == "disk_full"
    assert not builder.env_dir("venv", "b" * 64).exists()
    # a cached env is still served when the disk is full (no build needed)
    assert (
        await builder.ensure_dir_env("venv", "a" * 64, step, deadline=FAR)
    ).cache == ("hit")


@pytest.mark.asyncio
async def test_size_cap_counts_all_files_boundary(tmp_path: Path) -> None:
    builder = _builder(tmp_path, max_env_bytes=20)  # two 10-byte files = 20 + marker?

    step = Step()
    ok = await builder.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    assert ok.size_bytes == 20
    builder2 = _builder(tmp_path / "b", max_env_bytes=19)
    with pytest.raises(EnvError) as info:
        await builder2.ensure_dir_env("venv", DIGEST, Step(), deadline=FAR)
    assert info.value.reason == "env_too_large"
    assert not builder2.env_dir("venv", DIGEST).exists()


# ---------------------------------------------------------- read-only env
@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permission bits and a non-root user",
)
async def test_a_job_cannot_write_delete_or_create_in_its_env(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    await builder.ensure_dir_env("venv", DIGEST, Step(), deadline=FAR)
    final = builder.env_dir("venv", DIGEST)
    with pytest.raises(PermissionError):
        (final / "lib" / "mod.py").write_text("hacked")
    with pytest.raises(PermissionError):
        (final / "lib" / "mod.py").unlink()
    with pytest.raises(PermissionError):
        (final / "lib" / "new.py").write_text("new")
    with pytest.raises(PermissionError):
        (final / "newdir").mkdir()
    with pytest.raises(PermissionError):
        shutil.rmtree(final / "lib")
    with pytest.raises(PermissionError):
        (final / MARKER_NAME).write_text("{}")
    assert (final / "lib" / "mod.py").read_bytes() == b"VALUE = 1\n"
    assert not any(os.stat(p).st_mode & stat.S_IWUSR for p in final.rglob("*"))


# ------------------------------------------------------------- concurrency
@pytest.mark.asyncio
async def test_many_hashes_many_waiters_each_hash_builds_exactly_once(
    tmp_path: Path,
) -> None:
    builder = _builder(tmp_path)
    steps = {i: Step() for i in range(6)}
    digests = {i: f"{i:02x}" * 32 for i in range(6)}
    calls = [
        builder.ensure_dir_env("venv", digests[i], steps[i], deadline=FAR)
        for i in itertools.islice(itertools.cycle(range(6)), 36)
    ]
    results = await asyncio.gather(*calls)
    assert all(s.calls == 1 for s in steps.values())
    assert sum(r.cache == "built" for r in results) == 6
    assert {r.cache for r in results} <= {"built", "waited", "hit"}


@pytest.mark.asyncio
async def test_the_event_loop_stays_responsive_while_waiting_for_a_lock(
    tmp_path: Path,
) -> None:
    builder = _builder(tmp_path)
    gate = asyncio.Event()

    async def slow(target: Path) -> None:
        await Step()(target)
        await gate.wait()

    first = asyncio.create_task(
        builder.ensure_dir_env("venv", DIGEST, slow, deadline=FAR)
    )
    await asyncio.sleep(0.05)
    waiter = asyncio.create_task(
        builder.ensure_dir_env("venv", DIGEST, Step(), deadline=FAR)
    )
    ticks = 0
    for _ in range(10):
        await asyncio.sleep(0.01)
        ticks += 1
    assert ticks == 10 and not waiter.done()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    gate.set()
    assert (await first).cache == "built"
    assert builder.is_ready("venv", DIGEST)


# ------------------------------------------- unpinned base (D87), edge forms
@pytest.mark.parametrize(
    "image",
    [
        "python:3.12",
        "python@sha256:" + "A" * 64,
        "python@sha256:" + "a" * 63,
        "python@sha256:" + "a" * 65,
        "python@sha256:" + "g" * 64,
        "python@sha256:" + "a" * 64 + "\n",
        "python@sha256:" + "a" * 64 + "@sha256:" + "a" * 64,
        "python@",
        "",
    ],
)
def test_pinned_check_rejects_malformed_digests(image: str) -> None:
    with pytest.raises(Exception) as info:
        dr._check_pinned(image)
    assert getattr(info.value, "reason", "unpinned_base") == "unpinned_base" or (
        "unpinned" in str(info.value) or "pinned" in str(info.value)
    )


# ------------------------------------------- base -> runtime selection matrix
D = RuntimeAvailability("docker", True, sandboxed=True)
V = RuntimeAvailability("venv", True)
S = RuntimeAvailability("subprocess", True)


@pytest.mark.parametrize("base", ["python-cpu", "cuda-pytorch"])
@pytest.mark.parametrize("has_docker", [True, False])
@pytest.mark.parametrize("has_venv", [True, False])
@pytest.mark.parametrize("has_sub", [True, False])
def test_selection_matrix(
    base: str, has_docker: bool, has_venv: bool, has_sub: bool
) -> None:
    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=True,
        docker=D if has_docker else None,
        subprocess=S if has_sub else None,
        venv=V if has_venv else None,
    )
    expected = None
    if has_docker:
        expected = "docker"
    elif has_venv and base == "python-cpu":
        expected = "venv"
    elif has_sub:
        expected = "subprocess"
    chosen = choose_runtime(offer, GpuNeed.NONE, base)
    assert (chosen.name if chosen else None) == expected


@pytest.mark.parametrize("base", ["python-cpu", "cuda-pytorch"])
def test_selection_with_execution_disabled_or_unsandboxed_off(base: str) -> None:
    off = execution_offer(
        enabled=False, allow_unsandboxed=True, docker=D, subprocess=S, venv=V
    )
    assert choose_runtime(off, GpuNeed.NONE, base) is None
    no_opt = execution_offer(
        enabled=True, allow_unsandboxed=False, docker=None, subprocess=S, venv=V
    )
    assert choose_runtime(no_opt, GpuNeed.NONE, base) is None


def test_unknown_base_is_not_served_by_venv() -> None:
    offer = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=None, subprocess=None, venv=V
    )
    assert choose_runtime(offer, GpuNeed.NONE, "rocm-whatever") is None
    assert choose_runtime(offer, GpuNeed.NONE, "") is None
