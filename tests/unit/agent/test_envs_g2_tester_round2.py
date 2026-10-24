"""V0.5 G2 tester round 2: marker summary (damage detection), eviction, PEP 503
normalisation (hash v2) edge cases."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from meshforge.agent.envs import EnvError, env_hash, normalize_requirements
from meshforge.agent.envs.builder import MARKER_NAME, EnvBuilder, EnvsConfig
from meshforge.agent.envs.fsutil import tree_summary

pytestmark = pytest.mark.unit

FAR = 1e18
DIGEST = "ef" * 32
POSIX_USER = sys.platform != "win32" and not (
    hasattr(os, "geteuid") and os.geteuid() == 0
)


def _builder(tmp_path: Path) -> EnvBuilder:
    config = EnvsConfig(root=tmp_path / "envs", free_disk_floor_bytes=0)
    return EnvBuilder(config, uv_argv=("u",), disk_free=lambda _p: 10**15)


class Step:
    def __init__(self, extra: tuple[str, ...] = ()) -> None:
        self.calls = 0
        self.extra = extra

    async def __call__(self, target: Path) -> None:
        self.calls += 1
        (target / "lib").mkdir(parents=True)
        (target / "lib" / "mod.py").write_bytes(b"VALUE = 1\n")
        (target / "lib" / "other.py").write_bytes(b"VALUE = 22\n")
        for rel in self.extra:
            p = target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"payload")


def _unlock(root: Path) -> None:
    for dirpath, dirnames, _files in os.walk(root):
        for name in dirnames:
            os.chmod(os.path.join(dirpath, name), 0o700)
    os.chmod(root, 0o700)


# --------------------------------------------------------- marker v2 checks
@pytest.mark.asyncio
async def test_v1_marker_without_summary_counts_as_damaged(tmp_path: Path) -> None:
    b, step = _builder(tmp_path), Step()
    await b.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    final = b.env_dir("venv", DIGEST)
    _unlock(final)
    marker = final / MARKER_NAME
    marker.chmod(0o600)
    doc = json.loads(marker.read_text())
    doc.pop("summary")
    doc["v"] = 1
    marker.write_text(json.dumps(doc))
    assert b.env_state("venv", DIGEST) == "damaged"
    again = await b.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    assert again.cache == "built" and step.calls == 2


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="chmod semantics")
@pytest.mark.parametrize("damage", ["delete", "add", "rename", "truncate", "symlink"])
async def test_each_kind_of_damage_is_detected_and_rebuilt(
    tmp_path: Path, damage: str
) -> None:
    b, step = _builder(tmp_path), Step()
    await b.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    final = b.env_dir("venv", DIGEST)
    _unlock(final)
    f = final / "lib" / "mod.py"
    if damage == "delete":
        f.unlink()
    elif damage == "add":
        (final / "lib" / "evil.py").write_text("x")
    elif damage == "rename":
        f.rename(final / "lib" / "mod2.py")
    elif damage == "truncate":
        f.chmod(0o600)
        f.write_bytes(b"")
    else:
        f.unlink()
        f.symlink_to("/etc/passwd")
    assert b.env_state("venv", DIGEST) == "damaged"
    r = await b.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    assert r.cache == "built"
    assert not (final / "lib" / "evil.py").exists()
    assert b.env_state("venv", DIGEST) == "ready"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="chmod semantics")
async def test_damaged_env_racing_many_jobs_rebuilds_exactly_once(
    tmp_path: Path,
) -> None:
    b, step = _builder(tmp_path), Step()
    await b.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    final = b.env_dir("venv", DIGEST)
    _unlock(final)
    (final / "lib" / "mod.py").unlink()
    seen_ready: list[bool] = []

    class Spy(Step):
        async def __call__(self, target: Path) -> None:
            await asyncio.sleep(0.2)  # a slow rebuild: the others must wait for it
            await super().__call__(target)

    spy = Spy()

    async def job() -> str:
        r = await b.ensure_dir_env("venv", DIGEST, spy, deadline=FAR)
        seen_ready.append(b.env_state("venv", DIGEST) == "ready")
        return r.cache

    caches = await asyncio.gather(*(job() for _ in range(12)))
    assert spy.calls == 1
    assert caches.count("built") == 1 and "hit" not in caches
    assert all(seen_ready)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="chmod semantics")
async def test_jobs_arriving_during_the_rebuild_never_see_a_hit(tmp_path: Path) -> None:
    b, step = _builder(tmp_path), Step()
    await b.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    final = b.env_dir("venv", DIGEST)
    _unlock(final)
    (final / "lib" / "other.py").unlink()
    gate = asyncio.Event()

    class Slow(Step):
        async def __call__(self, target: Path) -> None:
            await super().__call__(target)
            await gate.wait()

    slow = Slow()
    first = asyncio.create_task(b.ensure_dir_env("venv", DIGEST, slow, deadline=FAR))
    await asyncio.sleep(0.1)
    assert b.env_state("venv", DIGEST) == "missing"  # evicted, not served
    late = asyncio.create_task(b.ensure_dir_env("venv", DIGEST, Step(), deadline=FAR))
    await asyncio.sleep(0.1)
    assert not late.done()
    gate.set()
    assert (await first).cache == "built"
    assert (await late).cache == "waited"


# --------------------------------------------------------- eviction failure
@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="chmod semantics")
async def test_eviction_failure_is_a_typed_error_not_a_loop_or_raw_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meshforge.agent.envs import builder as mod

    b, step = _builder(tmp_path), Step()
    await b.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    final = b.env_dir("venv", DIGEST)
    _unlock(final)
    (final / "lib" / "mod.py").unlink()
    calls = 0

    def refuse(path: Path) -> None:
        nonlocal calls
        calls += 1
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(mod, "remove_tree", refuse)
    with pytest.raises(EnvError) as info:
        await asyncio.wait_for(
            b.ensure_dir_env("venv", DIGEST, step, deadline=FAR), timeout=10
        )
    assert info.value.reason in {"build_io_failed", "env_io_failed", "evict_failed"}
    assert calls <= 3  # no retry loop
    assert step.calls == 1  # never rebuilt over a tree it could not remove


# ----------------------------------- __pycache__ / marker-name exclusion width
@pytest.mark.skipif(sys.platform == "win32", reason="chmod semantics")
@pytest.mark.parametrize(
    "hidden",
    [
        "lib/__pycache__/mod.cpython-312.pyc",  # the documented exclusion
    ],
)
def test_bytecode_cache_changes_are_ignored(tmp_path: Path, hidden: str) -> None:
    root = tmp_path / "t"
    (root / "lib" / "__pycache__").mkdir(parents=True)
    (root / "lib" / "a.py").write_text("x")
    before = tree_summary(root)
    (root / hidden).write_bytes(b"\0" * 10)
    assert tree_summary(root) == before


@pytest.mark.skipif(sys.platform == "win32", reason="chmod semantics")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hidden",
    [
        "data/__pycache__/model.bin",  # not bytecode
        "lib/__pycache__/evil.py",  # source file in a bytecode dir
        "pkg/deep/" + MARKER_NAME,  # marker-named file deeper in the tree
    ],
)
async def test_damage_cannot_hide_under_the_exclusions(
    tmp_path: Path, hidden: str
) -> None:
    """The exclusions are meant for interpreter bytecode and the root marker only.
    Deleting any other payload must still count as damage."""
    b, step = _builder(tmp_path), Step(extra=(hidden,))
    await b.ensure_dir_env("venv", DIGEST, step, deadline=FAR)
    final = b.env_dir("venv", DIGEST)
    _unlock(final)
    (final / hidden).unlink()
    assert b.env_state("venv", DIGEST) == "damaged", hidden


def test_summary_cost_on_a_large_env_stays_small(tmp_path: Path) -> None:
    root = tmp_path / "big"
    for d in range(100):
        sub = root / f"p{d}"
        sub.mkdir(parents=True)
        for i in range(200):
            (sub / f"m{i}.py").write_bytes(b"x")
    started = time.monotonic()
    s = tree_summary(root)
    took = time.monotonic() - started
    print(f"\nsummary of 20000 files: {took * 1000:.0f} ms")  # noqa: T201
    assert s.files == 20000
    assert took < 3.0
    assert tree_summary(root) == s  # deterministic


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="file names Windows cannot store (newline, ?, *, trailing space)",
)
def test_summary_handles_unicode_and_odd_names(tmp_path: Path) -> None:
    root = tmp_path / "u"
    root.mkdir()
    for name in ("é.py", "日本.py", "a b.py", "a\nb.py"):
        (root / name).write_text("x")
    assert tree_summary(root).files == 4


# ------------------------------------------------------- PEP 503 hash v2
def _h(text: str) -> str:
    return env_hash_of(normalize_requirements(text.encode()))


def env_hash_of(reqs: bytes) -> str:
    from meshforge.agent.envs import EnvInputs

    return env_hash(EnvInputs("venv", "cpython-3.12.3", "", reqs, "linux-x86_64"))


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Requests==2.0", "requests == 2.0"),
        ("Foo_Bar.baz==1", "foo-bar-baz==1"),
        ("foo__bar==1", "foo-bar==1"),
        ("requests[Security,SOCKS]==2", "requests[socks, security]==2"),
        ("requests[socks,socks]==2", "requests[socks]==2"),
        ("foo>=1,<2", "foo >= 1 , < 2"),
        ("foo===1.0", "foo === 1.0"),
        ("foo==1;python_version<'3.12'", "foo==1 ;   python_version<'3.12'"),
        ("foo @ https://x.org/a.whl#sha256=ab", "Foo @https://x.org/a.whl#sha256=ab"),
        ("foo[]==1", "foo==1"),
    ],
)
def test_equivalent_spellings_share_a_hash(a: str, b: str) -> None:
    assert _h(a) == _h(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("foo==1", "foo===1"),
        ("foo==1", "foo>=1"),
        ("foo==1", "foo==1.0"),
        ("foo==1 ; os_name=='nt'", "foo==1 ; os_name=='posix'"),
        ("foo==1 ; os_name=='nt'", "foo==1"),
        ("foo[a]==1", "foo==1"),
        ("foo @ https://x.org/a.whl#egg=foo", "foo @ https://x.org/a.whl"),
        ("foo @ https://x.org/a.whl#sha256=aa", "foo @ https://x.org/a.whl#sha256=bb"),
        (
            "foo @ https://x.org/A.whl",
            "foo @ https://x.org/a.whl",
        ),  # path is case-sensitive
        ("foo @ git+https://h/r@v1", "foo @ git+https://h/r@v2"),
        ("foo-bar==1", "foobar==1"),
    ],
)
def test_distinct_environments_never_share_a_hash(a: str, b: str) -> None:
    assert _h(a) != _h(b)


def test_normalisation_is_idempotent_on_awkward_input() -> None:
    text = (
        "Requests[Security, SOCKS] == 2.0 ; python_version<'3.12'\n"
        "foo @ https://x.org/a.whl#egg=foo ; os_name=='nt'\n"
        "Foo_Bar>=1 , <2\n--index-url https://i.example/simple\n"
    )
    once = normalize_requirements(text.encode())
    assert normalize_requirements(once) == once


def test_hashed_requirement_lines_survive_normalisation() -> None:
    """pip-compile --generate-hashes output: `name==v \\ --hash=sha256:...`. The
    --hash option must stay separated from the version."""
    sha = "a" * 64
    out = normalize_requirements(
        f"mfdemo==1.0 \\\n    --hash=sha256:{sha} \\\n    --hash=sha256:{'b' * 64}\n".encode()
    ).decode()
    assert "1.0--hash" not in out and "1.0 --hash=sha256:" in out, out
    assert "==1.0" in out


@pytest.mark.parametrize(
    "line",
    [
        "foo @ git+file:///srv/repo@v1",
        "foo @ hg+file:///srv/repo",
        "foo @ svn+file:///srv/repo",
        "foo @ git+/srv/repo",
        "foo @ ftp://x/a.whl",
        "foo @ data:text/plain,x",
    ],
)
def test_vcs_urls_to_local_repositories_are_refused_too(line: str) -> None:
    with pytest.raises(EnvError):
        normalize_requirements(line.encode())


@pytest.mark.parametrize(
    "line",
    [
        "foo @ git+https://github.com/a/b@v1#egg=foo",
        "foo @ git+ssh://git@h/r.git@abc123",
        "foo @ https://x.org/a.whl#sha256=ab",
        "foo @ HTTP://x.org/a.whl",
    ],
)
def test_remote_direct_references_are_kept(line: str) -> None:
    out = normalize_requirements(line.encode()).decode()
    assert out.startswith("foo @ ") and line.split("@ ", 1)[1].split("#")[0] in out


def test_option_lines_and_bare_urls_are_kept_as_written() -> None:
    out = normalize_requirements(
        b"--index-url https://i.example/simple\nhttps://x.org/a-1.0-py3-none-any.whl\n"
    ).decode()
    assert "--index-url https://i.example/simple" in out
    assert "https://x.org/a-1.0-py3-none-any.whl" in out
