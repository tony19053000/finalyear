"""V0.5 G3 (D94) tester: log GC against hostile folder contents and races."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from pathlib import Path

import pytest

from meshforge.common.ids import new_attempt_id
from meshforge.controller.jobs.logs import LogHub

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

NOW = dt.datetime.now(dt.UTC)
OLD = (NOW - dt.timedelta(days=30)).timestamp()
CUTOFF = NOW - dt.timedelta(days=7)
posix_only = pytest.mark.skipif(os.name != "posix", reason="symlinks")


def _hub(tmp_path: Path) -> LogHub:
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=1000)
    hub.prepare()
    return hub


def _touch(path: Path, mtime: float = OLD) -> Path:
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


@posix_only
async def test_symlinks_to_outside_files_and_dirs_leave_targets_intact(
    tmp_path: Path,
) -> None:
    hub = _hub(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = _touch(outside / "secret.txt")
    a, b, c = (str(new_attempt_id()) for _ in range(3))
    (hub.root / f"{a}.stdout").symlink_to(secret)
    (hub.root / f"{b}.stderr").symlink_to(outside, target_is_directory=True)
    (hub.root / f"{c}.gaps").symlink_to(tmp_path / "does-not-exist")

    async def yes(_a: str) -> bool:
        return True

    for link in hub.root.iterdir():
        os.utime(link, (OLD, OLD), follow_symlinks=False)
    await hub.collect_garbage(yes, older_than=CUTOFF, limit=100)
    assert secret.exists() and secret.read_bytes() == b"x"
    assert outside.is_dir() and list(outside.iterdir()) == [secret]
    assert list(hub.root.iterdir()) == []  # the links went, never their targets


async def test_orphan_files_with_no_db_row_are_collected(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    attempt = str(new_attempt_id())
    files = [
        _touch(hub.root / f"{attempt}.stdout"),
        _touch(hub.root / f"{attempt}.gaps"),
    ]

    async def orphan(_a: str) -> bool:
        return True

    assert await hub.collect_garbage(orphan, older_than=CUTOFF, limit=10) == 2
    assert not any(f.exists() for f in files)


async def test_an_attempt_reopened_while_the_check_runs_is_kept(
    tmp_path: Path,
) -> None:
    hub = _hub(tmp_path)
    attempt = str(new_attempt_id())
    f = _touch(hub.root / f"{attempt}.stdout")

    async def reopen_then_yes(a: str) -> bool:
        await asyncio.sleep(0)
        hub.open(a, job_id="job_1", node_id="node_1", token=2)
        return True

    deleted = await hub.collect_garbage(reopen_then_yes, older_than=CUTOFF, limit=10)
    assert deleted == 0 and f.exists()


async def test_a_raising_check_does_not_delete_and_files_survive(
    tmp_path: Path,
) -> None:
    hub = _hub(tmp_path)
    attempt = str(new_attempt_id())
    f = _touch(hub.root / f"{attempt}.stdout")

    async def boom(_a: str) -> bool:
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        await hub.collect_garbage(boom, older_than=CUTOFF, limit=10)
    assert f.exists()


async def test_mtime_exactly_at_the_cutoff_is_kept(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    attempt = str(new_attempt_id())
    f = _touch(hub.root / f"{attempt}.stdout", CUTOFF.timestamp())

    async def yes(_a: str) -> bool:
        return True

    assert await hub.collect_garbage(yes, older_than=CUTOFF, limit=10) == 0
    assert f.exists()


async def test_unicode_and_junk_names_are_ignored(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    junk = [
        _touch(hub.root / "é日本.stdout"),
        _touch(hub.root / "notanid.stdout"),
        _touch(hub.root / ".hidden"),
        _touch(hub.root / "..stdout"),
    ]

    async def yes(_a: str) -> bool:
        return True

    assert await hub.collect_garbage(yes, older_than=CUTOFF, limit=10) == 0
    assert all(j.exists() for j in junk)
