"""V1.0 G11 (D458): a venv's base interpreter is part of its identity.

Lab L4 (Windows, 0.17.1-1 -> 0.17.2-1): cached venvs kept running the old
version's ``python.exe`` because the env hash named only ``cpython-3.12.x``.

* the env hash includes the base interpreter identity (path + full version +
  platform): another base -> another key; the same base -> the same key;
* a venv whose ``pyvenv.cfg`` ``home`` is missing or now resolves elsewhere is
  ``damaged`` (rebuilt), never ``ready``;
* the agent GC evicts venvs whose base is gone or lies in an older bundle.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from meshforge.agent.envs.base_python import (
    BASE_HOME_KEY,
    interpreter_identity,
    stale_base,
)
from meshforge.agent.envs.builder import MARKER_NAME, EnvBuilder, EnvsConfig
from meshforge.agent.envs.fsutil import remove_tree
from meshforge.agent.envs.hash import EnvInputs, canonical_bytes, env_hash
from meshforge.agent.execution.venv_runtime import direct_python
from meshforge.agent.gc.collector import GarbageCollector, GcConfig
from meshforge.common.buildinfo import BUILDINFO_NAME
from meshforge.common.cas import CasStore
from meshforge.common.gc_index import GcIndex

pytestmark = pytest.mark.unit

_FULL = ".".join(str(p) for p in sys.version_info[:3])
_SYS_VERSION = f"{_FULL} (main, Oct  1 2026, 10:00:00) [MSC v.1940 64 bit (AMD64)]"
_OLD = r"C:\Program Files\MeshForge\0.17.1-1\python\python.exe"
_NEW = r"C:\Program Files\MeshForge\0.17.2-1\python\python.exe"


def _identity(executable: str, version: str = _SYS_VERSION) -> str:
    return interpreter_identity("cpython", executable, version, "win-amd64")


def _inputs(identity: str) -> EnvInputs:
    return EnvInputs(
        runtime_kind="venv",
        base=f"cpython-{_FULL}",
        python="",
        requirements=b"numpy==2.1.0",
        platform="win-amd64",
        interpreter=identity,
    )


# ------------------------------------------------------------- the env key


def test_another_base_path_is_another_key() -> None:
    assert env_hash(_inputs(_identity(_OLD))) != env_hash(_inputs(_identity(_NEW)))


def test_another_full_version_is_another_key() -> None:
    rebuilt = _SYS_VERSION.replace("Oct  1", "Nov  2")  # same x.y.z, new build
    assert env_hash(_inputs(_identity(_NEW))) != env_hash(
        _inputs(_identity(_NEW, rebuilt))
    )


def test_another_platform_or_implementation_is_another_identity() -> None:
    base = _identity(_NEW)
    assert base != interpreter_identity("cpython", _NEW, _SYS_VERSION, "win32")
    assert base != interpreter_identity("pypy", _NEW, _SYS_VERSION, "win-amd64")


def test_same_base_is_the_same_key() -> None:
    assert _identity(_NEW) == _identity(_NEW)
    assert env_hash(_inputs(_identity(_NEW))) == env_hash(_inputs(_identity(_NEW)))


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
def test_identity_uses_the_resolved_path(tmp_path: Path) -> None:
    real = tmp_path / "0.17.2-1" / "python3.12"
    real.parent.mkdir()
    real.write_bytes(b"")
    (tmp_path / "current").symlink_to("0.17.2-1")
    via_link = tmp_path / "current" / "python3.12"
    assert _identity(str(via_link)) == _identity(str(real))
    # the link re-pointed to another version: another identity
    other = tmp_path / "0.17.3-1" / "python3.12"
    other.parent.mkdir()
    other.write_bytes(b"")
    (tmp_path / "current").unlink()
    (tmp_path / "current").symlink_to("0.17.3-1")
    assert _identity(str(via_link)) == _identity(str(other)) != _identity(str(real))


def test_no_interpreter_keeps_the_old_hashed_form() -> None:
    """Docker / --target inputs (no interpreter) hash exactly as before."""
    plain = _inputs("")
    assert b"interpreter" not in canonical_bytes(plain)
    assert b'"interpreter":"' in canonical_bytes(_inputs(_identity(_NEW)))


def test_interpreter_must_be_a_digest() -> None:
    with pytest.raises(ValueError, match="interpreter"):
        _inputs(_NEW)


# ----------------------------------------------- the builder uses the identity


def _builder(tmp_path: Path, probes: dict[str, bytes]) -> EnvBuilder:
    builder = EnvBuilder(
        EnvsConfig(root=tmp_path / "envs", free_disk_floor_bytes=0),
        uv_argv=("uv",),
        bundled_python=None,
    )

    async def fake_query(argv: list[str], env: dict[str, str]) -> tuple[int, bytes]:
        if argv[1:3] == ["python", "find"]:
            return 0, b"/usr/bin/python3.12\n"
        return 0, probes["probe"]

    async def fake_install(argv: list[str], **_kw: object) -> None:
        if "venv" in argv:
            Path(argv[-1]).mkdir(parents=True)  # noqa: ASYNC240 - a test fake

    builder._query = fake_query  # type: ignore[method-assign]
    builder._install = fake_install  # type: ignore[method-assign]
    return builder


def _probe(executable: str, version: str = _SYS_VERSION) -> bytes:
    return json.dumps(["cpython", _FULL, "win-amd64", version, executable]).encode()


def test_ensure_venv_key_follows_the_base_interpreter(tmp_path: Path) -> None:
    probes = {"probe": _probe(_OLD)}

    async def digest_for(executable: str) -> str:
        probes["probe"] = _probe(executable)
        builder = _builder(tmp_path, probes)  # a fresh process: a fresh probe
        built = await builder.ensure_venv(python=None, requirements=b"", deadline=1e18)
        return built.env_hash

    try:
        old = asyncio.run(digest_for(_OLD))
        again = asyncio.run(digest_for(_OLD))
        new = asyncio.run(digest_for(_NEW))
    finally:
        remove_tree(tmp_path / "envs")
    assert old == again
    assert old != new


def test_a_three_field_probe_still_gets_an_identity(tmp_path: Path) -> None:
    probes = {"probe": json.dumps(["cpython", _FULL, "linux-x86_64"]).encode()}
    found = asyncio.run(_builder(tmp_path, probes).find_interpreter(None))
    assert len(found.identity) == 64
    assert found.identity == interpreter_identity(
        "cpython", "/usr/bin/python3.12", _FULL, "linux-x86_64"
    )


def test_the_interpreter_is_probed_once_per_process(tmp_path: Path) -> None:
    probes = {"probe": _probe(_NEW)}
    builder = _builder(tmp_path, probes)
    calls: list[list[str]] = []
    inner = builder._query

    async def counting(argv: list[str], env: dict[str, str]) -> tuple[int, bytes]:
        calls.append(argv)
        return await inner(argv, env)

    builder._query = counting  # type: ignore[method-assign]

    async def twice() -> tuple[str, str]:
        a = await builder.ensure_venv(python=None, requirements=b"", deadline=1e18)
        b = await builder.ensure_venv(python=None, requirements=b"", deadline=1e18)
        assert b.cache == "hit"
        return a.env_hash, b.env_hash

    try:
        first, second = asyncio.run(twice())
    finally:
        remove_tree(tmp_path / "envs")
    assert first == second
    assert len(calls) == 2  # one `uv python find` + one probe, for both calls


# ------------------------------------------------- stale pyvenv.cfg home


def _venv_builder(tmp_path: Path) -> EnvBuilder:
    return EnvBuilder(
        EnvsConfig(root=tmp_path / "envs", free_disk_floor_bytes=0),
        uv_argv=("uv",),
        bundled_python=None,
    )


def _venv_step(home: Path) -> Callable[[Path], Awaitable[None]]:
    async def build(target: Path) -> None:
        target.mkdir(parents=True)  # noqa: ASYNC240 - a test fake
        (target / "pyvenv.cfg").write_text(f"home = {home}\nversion_info = 3.12\n")

    return build


def test_missing_home_is_rebuilt_never_run(tmp_path: Path) -> None:
    home = tmp_path / "MeshForge" / "0.17.1-1" / "python"
    home.mkdir(parents=True)
    builder = _venv_builder(tmp_path)
    try:
        built = asyncio.run(
            builder.ensure_dir_env("venv", "h1", _venv_step(home), deadline=1e18)
        )
        assert built.cache == "built"
        assert builder.env_state("venv", "h1") == "ready"
        home.rmdir()  # the old version folder was removed
        assert builder.env_state("venv", "h1") == "damaged"
        new_home = tmp_path / "MeshForge" / "0.17.2-1" / "python"
        new_home.mkdir(parents=True)
        again = asyncio.run(
            builder.ensure_dir_env("venv", "h1", _venv_step(new_home), deadline=1e18)
        )
        assert again.cache == "built"  # rebuilt, not run on the missing base
        assert builder.env_state("venv", "h1") == "ready"
    finally:
        remove_tree(tmp_path / "envs")


def test_the_marker_records_the_resolved_home(tmp_path: Path) -> None:
    home = tmp_path / "py"
    home.mkdir()
    builder = _venv_builder(tmp_path)
    try:
        asyncio.run(
            builder.ensure_dir_env("venv", "h1", _venv_step(home), deadline=1e18)
        )
        marker = json.loads(
            (tmp_path / "envs" / "venv" / "h1" / MARKER_NAME).read_text()
        )
        assert marker[BASE_HOME_KEY] == os.path.normcase(os.path.realpath(home))
    finally:
        remove_tree(tmp_path / "envs")


@pytest.mark.skipif(os.name != "posix", reason="the current symlink is POSIX")
def test_a_home_that_now_resolves_elsewhere_is_damaged(tmp_path: Path) -> None:
    (tmp_path / "0.17.1-1" / "python" / "bin").mkdir(parents=True)
    (tmp_path / "0.17.2-1" / "python" / "bin").mkdir(parents=True)
    current = tmp_path / "current"
    current.symlink_to("0.17.1-1")
    builder = _venv_builder(tmp_path)
    try:
        asyncio.run(
            builder.ensure_dir_env(
                "venv", "h1", _venv_step(current / "python" / "bin"), deadline=1e18
            )
        )
        assert builder.env_state("venv", "h1") == "ready"
        current.unlink()
        current.symlink_to("0.17.2-1")  # the upgrade re-pointed `current`
        assert builder.env_state("venv", "h1") == "damaged"
    finally:
        remove_tree(tmp_path / "envs")


def test_direct_python_never_returns_a_missing_base(tmp_path: Path) -> None:
    venv = tmp_path / "v"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"")
    (venv / "pyvenv.cfg").write_text(f"home = {tmp_path / 'gone'}\n")
    assert direct_python(venv, platform="win32") is None


# ------------------------------------------------------------------- GC


def _bundle(root: Path) -> Path:
    """A fake bundle root with its interpreter folder; returns the ``home``."""
    home = root / "python"
    home.mkdir(parents=True)
    (root / BUILDINFO_NAME).write_text("{}", encoding="utf-8")
    return home


def _fake_venv(envs: Path, digest: str, home: Path, *, recorded: Path | None) -> None:
    folder = envs / "venv" / digest
    folder.mkdir(parents=True)
    (folder / "pyvenv.cfg").write_text(f"home = {home}\n")
    marker: dict[str, object] = {
        "env_hash": digest,
        "kind": "venv",
        "summary": {"bytes": 10},
    }
    if recorded is not None:
        marker[BASE_HOME_KEY] = os.path.normcase(os.path.realpath(recorded))
    (folder / MARKER_NAME).write_text(json.dumps(marker))


def test_stale_base_rules(tmp_path: Path) -> None:
    old_home = _bundle(tmp_path / "MeshForge" / "0.17.1-1")
    new_root = tmp_path / "MeshForge" / "0.17.2-1"
    new_home = _bundle(new_root)
    other = tmp_path / "usr" / "bin"
    other.mkdir(parents=True)
    envs = tmp_path / "envs"
    _fake_venv(envs, "a" * 64, old_home, recorded=old_home)
    _fake_venv(envs, "b" * 64, new_home, recorded=new_home)
    _fake_venv(envs, "c" * 64, other, recorded=None)  # a uv-found system Python
    _fake_venv(envs, "d" * 64, tmp_path / "gone", recorded=None)
    _fake_venv(envs, "e" * 64, old_home, recorded=None)  # a pre-D458 marker
    venv = envs / "venv"
    assert stale_base(venv / ("a" * 64), new_root) == "base_old"
    assert stale_base(venv / ("b" * 64), new_root) is None
    assert stale_base(venv / ("c" * 64), new_root) is None
    assert stale_base(venv / ("d" * 64), new_root) == "base_missing"
    assert stale_base(venv / ("e" * 64), new_root) == "base_old"
    # a dev agent (no bundle of its own): only a vanished base is stale
    assert stale_base(venv / ("a" * 64), None) is None
    assert stale_base(venv / ("d" * 64), None) == "base_missing"
    # no pyvenv.cfg: not judged
    (venv / ("f" * 64)).mkdir()
    assert stale_base(venv / ("f" * 64), new_root) is None


class _Rig:
    def __init__(self, tmp_path: Path, *, min_age_s: float = 0.0) -> None:
        self.now = 1_000_000.0
        store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 20)
        store.prepare()
        self.index = GcIndex(store.root, wall=lambda: self.now)
        self.index.open()
        self.envs = tmp_path / "envs"
        self.new_root = tmp_path / "MeshForge" / "0.17.2-1"
        self.gc = GarbageCollector(
            self.index,
            store,
            GcConfig(min_age_s=min_age_s),  # no quota: only the D458 rule evicts
            envs_root=self.envs,
            wall=lambda: self.now,
            current_bundle=self.new_root,
        )

    def close(self) -> None:
        self.index.close()


def test_gc_evicts_old_base_venvs_without_a_quota(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    old_home = _bundle(tmp_path / "MeshForge" / "0.17.1-1")
    new_home = _bundle(rig.new_root)
    _fake_venv(rig.envs, "a" * 64, old_home, recorded=old_home)
    _fake_venv(rig.envs, "b" * 64, new_home, recorded=new_home)
    _fake_venv(rig.envs, "d" * 64, tmp_path / "gone", recorded=None)
    try:
        report = rig.gc.run_pass("test")
        assert report.evicted.get("env") == 2
        assert not (rig.envs / "venv" / ("a" * 64)).exists()
        assert not (rig.envs / "venv" / ("d" * 64)).exists()
        assert (rig.envs / "venv" / ("b" * 64)).is_dir()
        assert {k for k, _s, _u in rig.index.items("env")} == {f"venv/{'b' * 64}"}
        # now nothing points into the old folder any more
        assert not any(
            "0.17.1-1" in (p / "pyvenv.cfg").read_text()
            for p in (rig.envs / "venv").iterdir()
        )
    finally:
        rig.close()


def test_gc_skips_a_cheap_pass(tmp_path: Path) -> None:
    rig = _Rig(tmp_path)
    old_home = _bundle(tmp_path / "MeshForge" / "0.17.1-1")
    _bundle(rig.new_root)
    _fake_venv(rig.envs, "a" * 64, old_home, recorded=old_home)
    try:
        rig.gc.reconcile()
        assert rig.gc.run_pass("cheap", reconcile=False).evicted == {}
        assert (rig.envs / "venv" / ("a" * 64)).is_dir()
    finally:
        rig.close()


def test_gc_keeps_a_leased_or_young_stale_venv(tmp_path: Path) -> None:
    rig = _Rig(tmp_path, min_age_s=300.0)
    old_home = _bundle(tmp_path / "MeshForge" / "0.17.1-1")
    _bundle(rig.new_root)
    _fake_venv(rig.envs, "a" * 64, old_home, recorded=old_home)
    _fake_venv(rig.envs, "c" * 64, old_home, recorded=old_home)
    try:
        # first seen now: younger than min_age_s, both kept
        assert rig.gc.run_pass("test").evicted == {}
        assert (rig.envs / "venv" / ("a" * 64)).is_dir()
        rig.index.lease("attempt-1", [("env", f"venv/{'c' * 64}")])  # running
        rig.now += 1000
        report = rig.gc.run_pass("test")
        assert report.evicted.get("env") == 1
        assert not (rig.envs / "venv" / ("a" * 64)).exists()
        assert (rig.envs / "venv" / ("c" * 64)).is_dir()  # leased: kept
        rig.index.release("attempt-1")
        rig.now += 1000
        assert rig.gc.run_pass("test").evicted.get("env") == 1
        assert not (rig.envs / "venv" / ("c" * 64)).exists()
    finally:
        rig.close()
