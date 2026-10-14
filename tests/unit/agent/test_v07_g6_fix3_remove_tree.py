"""V0.7 G6 fix round 3, T-20 (D164): ``remove_tree`` and cleanups.

* a tree holding directories the owner cannot read, search or write (a job
  can ``chmod 000`` inside its workdir) is removed - whatever the mode,
  nested, with files inside;
* it raises ``OSError`` only (never the ``TypeError`` of ``os.open(path)``
  without flags), ends on a tree it truly cannot remove, and treats an entry
  that vanished under it as done;
* a cleanup that runs while a ``CancelledError`` is on its way never replaces
  it: ``remove_tree_quietly`` and the env builder's ``_discard``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
import structlog

from meshforge.agent.envs import builder as builder_module
from meshforge.agent.envs import fsutil
from meshforge.agent.envs.fsutil import remove_tree, remove_tree_quietly

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes"),
    pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes"
    ),
]

MODES = [0o000, 0o100, 0o200, 0o300, 0o400, 0o500, 0o600, 0o700]


def _unlock(root: Path) -> None:
    """Test hygiene only: whatever is left becomes removable again."""
    if not root.exists():
        return
    os.chmod(root, stat.S_IRWXU)
    for dirpath, dirnames, _files in os.walk(root):
        for name in dirnames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                os.chmod(path, stat.S_IRWXU)


@pytest.mark.security
@pytest.mark.parametrize("inner", MODES)
@pytest.mark.parametrize("outer", [0o000, 0o300, 0o500, 0o700])
def test_a_tree_with_locked_directories_at_two_levels_is_removed(
    tmp_path: Path, outer: int, inner: int
) -> None:
    tree = tmp_path / "tree"
    deep = tree / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "f").write_text("x")
    (tree / "a" / "b" / "g").write_text("y")
    (tree / "a" / "h").write_text("z")
    os.chmod(tree / "a" / "h", 0o000)
    os.symlink(tmp_path, tree / "a" / "b" / "up")
    os.chmod(tree / "a" / "b", inner)
    os.chmod(tree / "a", outer)
    try:
        remove_tree(tree)
        assert not os.path.lexists(tree)
        assert tmp_path.is_dir()  # the symlink's target is not walked into
    finally:
        _unlock(tree)


def test_the_top_of_the_tree_may_be_locked_too(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f").write_text("x")
    os.chmod(tree / "sub", 0o000)
    os.chmod(tree, 0o000)
    try:
        remove_tree(tree)
        assert not os.path.lexists(tree)
    finally:
        _unlock(tree)


@pytest.mark.security
def test_a_tree_that_cannot_be_removed_raises_oserror_and_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another user's directory: chmod has no effect, every retry fails."""
    tree = tmp_path / "tree"
    (tree / "sub" / "deep").mkdir(parents=True)
    (tree / "sub" / "deep" / "f").write_text("x")
    os.chmod(tree / "sub", 0o000)
    real = os.chmod
    monkeypatch.setattr(fsutil.os, "chmod", lambda *a, **k: None)
    try:
        with pytest.raises(OSError, match="sub") as exc:
            remove_tree(tree)
        assert isinstance(exc.value, PermissionError)
        assert remove_tree_quietly(tree) is False
    finally:
        monkeypatch.setattr(fsutil.os, "chmod", real)
        _unlock(tree)
    remove_tree(tree)
    assert not tree.exists()


@pytest.mark.parametrize("step", ["open", "scandir", "rmdir", "unlink", "lstat"])
def test_an_entry_that_vanishes_under_the_walk_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """``_seal`` renames ``<hash>.building`` while the shielded remove walks it
    (the tester's ``during_remove`` case): whatever step meets the missing
    entry, the result is "gone", not an exception."""
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f").write_text("x")
    aside = tmp_path / "aside"
    real = getattr(os, step)
    done = {"n": 0}

    def vanish(*args: Any, **kwargs: Any) -> Any:
        if not done["n"] and tree.exists():
            done["n"] = 1
            os.rename(tree, aside)
            raise FileNotFoundError(2, "No such file or directory", str(tree))
        return real(*args, **kwargs)

    monkeypatch.setattr(shutil.os, step, vanish)
    remove_tree(tree)
    monkeypatch.undo()
    assert done["n"] == 1
    assert not tree.exists()


def test_a_failing_step_is_never_called_with_the_path_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect itself: the hook retried ``func(path)`` for every step."""
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f").write_text("x")
    real_open = os.open
    failed: list[Any] = []

    def flaky_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if os.path.basename(str(path)) == "sub" and not failed:
            failed.append(path)
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(shutil.os, "open", flaky_open)
    remove_tree(tree)
    monkeypatch.undo()
    assert failed and not tree.exists()


def test_quietly_never_raises_and_says_what_stayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "left-behind"
    tree.mkdir()

    def broken(path: Path) -> None:
        raise TypeError("open() missing required argument 'flags'")

    monkeypatch.setattr(fsutil, "remove_tree", broken)
    with structlog.testing.capture_logs() as logs:
        assert remove_tree_quietly(tree) is False
    assert logs == [
        {
            "event": "agent.fs.cleanup_failed",
            "log_level": "warning",
            "path": "left-behind",
            "cause": "TypeError",
        }
    ]
    monkeypatch.undo()
    assert remove_tree_quietly(tree) is True
    assert remove_tree_quietly(tree) is True  # missing is fine


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TypeError("flags"), PermissionError(13, "denied")])
async def test_a_shielded_discard_never_replaces_the_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """The pattern of every build cleanup: ``except BaseException: await
    _discard(path); raise`` - the caller must see ITS exception."""

    def refuse(path: Path) -> None:
        raise error

    monkeypatch.setattr(builder_module, "remove_tree", refuse)
    started = asyncio.Event()

    async def build() -> None:
        try:
            started.set()
            await asyncio.sleep(60)
        except BaseException:
            await builder_module._discard(tmp_path / "x.building")
            raise

    task = asyncio.create_task(build())
    await started.wait()
    task.cancel()
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(asyncio.CancelledError),
    ):
        await task
    assert [entry["event"] for entry in logs] == ["agent.envs.cleanup_failed"]
    assert logs[0]["path"] == "x.building"
    assert logs[0]["cause"] == type(error).__name__
