"""Tester pass, round G11: D457 writer races, D458 GC/env safety under contention."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from meshforge.agent import public_node
from meshforge.agent.envs.base_python import BASE_HOME_KEY
from meshforge.agent.envs.builder import MARKER_NAME
from meshforge.agent.gc.collector import GarbageCollector, GcConfig
from meshforge.agent.identity import NodeIdentity, load_or_create
from meshforge.common.buildinfo import BUILDINFO_NAME
from meshforge.common.cas import CasStore
from meshforge.common.filelock import locked
from meshforge.common.gc_index import GcIndex
from meshforge.common.local_node import PUBLIC_NODE_FILE_NAME, read_public_node_id

pytestmark = pytest.mark.unit

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX modes")


@pytest.fixture
def identity(tmp_path: Path) -> NodeIdentity:
    return load_or_create(tmp_path / "private" / "identity")


@posix_only
def test_concurrent_publishers_always_leave_a_readable_record(
    tmp_path: Path, identity: NodeIdentity
) -> None:
    """Start + rotation hook racing: a reader never sees a half record."""
    public = tmp_path / "public"
    public.mkdir()
    public.chmod(0o755)
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for _ in range(60):
                public_node.publish(identity, public)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        while not stop.is_set():
            target = public / PUBLIC_NODE_FILE_NAME
            if not target.exists():
                continue
            try:
                assert read_public_node_id(public) == identity.node_id
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return

    threads = [threading.Thread(target=writer) for _ in range(4)]
    watcher = threading.Thread(target=reader)
    watcher.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    stop.set()
    watcher.join(10)
    assert not errors, errors
    assert [p.name for p in public.iterdir()] == [PUBLIC_NODE_FILE_NAME]


@posix_only
def test_publish_replaces_a_planted_symlink_instead_of_following_it(
    tmp_path: Path, identity: NodeIdentity
) -> None:
    public = tmp_path / "public"
    public.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (public / PUBLIC_NODE_FILE_NAME).symlink_to(victim)
    public_node.publish(identity, public)
    assert victim.read_text() == "keep me"
    assert not (public / PUBLIC_NODE_FILE_NAME).is_symlink()


def test_public_file_has_no_secret_bytes_from_the_private_dir(
    tmp_path: Path, identity: NodeIdentity
) -> None:
    public = tmp_path / "public"
    public.mkdir()
    path = public_node.publish(identity, public)
    blob = path.read_bytes()
    private = (tmp_path / "private" / "identity").iterdir()
    for secret in private:
        if secret.name == "node.json":
            continue
        data = secret.read_bytes()
        for line in data.splitlines():
            if len(line) > 20:
                assert line not in blob, secret.name
    assert b"PRIVATE" not in blob and b"BEGIN" not in blob
    assert set(json.loads(blob)) == {
        "format",
        "node_id",
        "key_fingerprint_sha256",
        "agent_version",
        "written_at",
    }


# ---------------------------------------------------------------- D458 GC


def _bundle(root: Path) -> Path:
    home = root / "python"
    home.mkdir(parents=True)
    (root / BUILDINFO_NAME).write_text("{}", encoding="utf-8")
    return home


def _venv(envs: Path, digest: str, home: Path) -> Path:
    folder = envs / "venv" / digest
    folder.mkdir(parents=True)
    (folder / "pyvenv.cfg").write_text(f"home = {home}\n")
    (folder / MARKER_NAME).write_text(
        json.dumps(
            {
                "env_hash": digest,
                "kind": "venv",
                "summary": {"bytes": 10},
                BASE_HOME_KEY: os.path.normcase(os.path.realpath(home)),
            }
        )
    )
    return folder


def _rig(tmp_path: Path) -> tuple[GarbageCollector, GcIndex, Path, Path]:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 20)
    store.prepare()
    now = [1_000_000.0]
    index = GcIndex(store.root, wall=lambda: now[0])
    index.open()
    envs = tmp_path / "envs"
    new_root = tmp_path / "MF" / "2"
    gc = GarbageCollector(
        index,
        store,
        GcConfig(min_age_s=0.0),
        envs_root=envs,
        wall=lambda: now[0],
        current_bundle=new_root,
    )
    return gc, index, envs, new_root


def test_gc_skips_a_stale_venv_whose_build_lock_is_held(tmp_path: Path) -> None:
    """A venv being reused/built by another process is never removed under it."""
    gc, index, envs, new_root = _rig(tmp_path)
    _bundle(new_root)
    old = _bundle(tmp_path / "MF" / "1")
    digest = "a" * 64
    folder = _venv(envs, digest, old)
    lock = envs / "locks" / f"{digest}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        with locked(lock):
            report = gc.run_pass("test")
            assert folder.is_dir()
            assert report.evicted.get("env", 0) == 0
        assert gc.run_pass("test").evicted.get("env") == 1
        assert not folder.exists()
    finally:
        index.close()


def test_gc_pass_is_idempotent_and_never_touches_the_current_bundles_venv(
    tmp_path: Path,
) -> None:
    gc, index, envs, new_root = _rig(tmp_path)
    cur = _venv(envs, "b" * 64, _bundle(new_root))
    try:
        for _ in range(3):
            assert gc.run_pass("test").evicted == {}
            assert cur.is_dir()
    finally:
        index.close()


def test_gc_ignores_non_hash_folders_and_garbage_markers(tmp_path: Path) -> None:
    gc, index, envs, new_root = _rig(tmp_path)
    _bundle(new_root)
    (envs / "venv" / "not-a-hash").mkdir(parents=True)
    (envs / "venv" / "not-a-hash" / "pyvenv.cfg").write_text("home = /nowhere\n")
    bad = _venv(envs, "c" * 64, _bundle(new_root.parent / "3"))
    (bad / MARKER_NAME).write_bytes(b"\x00\xff garbage")
    try:
        gc.run_pass("test")
        assert (envs / "venv" / "not-a-hash").is_dir()
        assert bad.is_dir()  # no readable marker: not a tracked env item, never touched
    finally:
        index.close()
