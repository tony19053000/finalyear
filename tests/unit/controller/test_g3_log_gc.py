"""V0.5 G3 (D94): :meth:`LogHub.collect_garbage` on a real folder.

The database check is the caller's (``expired``); here it is a recorded fake so
the hub's own rules are pinned: the mtime pre-filter, the "live in this hub is
never collected" guard, names the hub does not write are never touched, stale
temp files go, ``limit`` bounds the checks, and a failing unlink is counted,
never raised.
"""

from __future__ import annotations

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


def _hub(tmp_path: Path) -> LogHub:
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=1000)
    hub.prepare()
    return hub


def _files(hub: LogHub, attempt_id: str, *, mtime: float | None = OLD) -> list[Path]:
    paths = [hub.path(attempt_id, s) for s in ("stdout", "stderr")]
    for path in paths:
        path.write_bytes(b"x")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
    return paths


class Expired:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def __call__(self, attempt_id: str) -> bool:
        self.asked.append(attempt_id)
        return self.answer


async def test_old_and_expired_files_are_deleted(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    attempt = str(new_attempt_id())
    paths = _files(hub, attempt)
    expired = Expired()
    assert await hub.collect_garbage(expired, older_than=CUTOFF, limit=10) == 2
    assert expired.asked == [attempt]
    assert not any(p.exists() for p in paths)
    assert hub.stats.gc_files_deleted == 2


async def test_not_expired_is_kept(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    paths = _files(hub, str(new_attempt_id()))
    assert await hub.collect_garbage(Expired(False), older_than=CUTOFF, limit=10) == 0
    assert all(p.exists() for p in paths)


async def test_a_recent_file_keeps_the_whole_attempt_without_asking(
    tmp_path: Path,
) -> None:
    hub = _hub(tmp_path)
    attempt = str(new_attempt_id())
    old, recent = _files(hub, attempt)
    os.utime(recent, None)  # touched now
    expired = Expired()
    assert await hub.collect_garbage(expired, older_than=CUTOFF, limit=10) == 0
    assert expired.asked == [] and old.exists() and recent.exists()


async def test_an_attempt_live_in_the_hub_is_never_collected(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    attempt = str(new_attempt_id())
    paths = _files(hub, attempt)
    hub.open(attempt, job_id="job_x", node_id="node_x", token=1)
    expired = Expired()  # even if the caller says so
    assert await hub.collect_garbage(expired, older_than=CUTOFF, limit=10) == 0
    assert expired.asked == [] and all(p.exists() for p in paths)


async def test_foreign_names_are_never_touched_and_stale_temps_go(
    tmp_path: Path,
) -> None:
    hub = _hub(tmp_path)
    foreign = [
        hub.root / "notes.txt",
        hub.root / "att_1.stdout",  # not a canonical attempt id
        hub.root / f"{new_attempt_id()}.other",
    ]
    stale = hub.root / f".{new_attempt_id()}.stdout.abcd1234.tmp"
    fresh = hub.root / f".{new_attempt_id()}.stdout.beef0000.tmp"
    for path in [*foreign, stale, fresh]:
        path.write_bytes(b"x")
    for path in [*foreign, stale]:
        os.utime(path, (OLD, OLD))
    expired = Expired()
    assert await hub.collect_garbage(expired, older_than=CUTOFF, limit=10) == 1
    assert expired.asked == []
    assert all(p.exists() for p in foreign) and fresh.exists()
    assert not stale.exists()


async def test_a_symlink_is_removed_not_followed(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("symlinks need privileges on Windows")
    hub = _hub(tmp_path)
    target = tmp_path / "precious"
    target.write_bytes(b"keep me")
    link = hub.path(str(new_attempt_id()), "stdout")
    link.symlink_to(target)
    os.utime(link, (OLD, OLD), follow_symlinks=False)
    assert await hub.collect_garbage(Expired(), older_than=CUTOFF, limit=10) == 1
    assert not link.is_symlink() and target.read_bytes() == b"keep me"


async def test_limit_bounds_the_checks_oldest_first(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    attempts = [str(new_attempt_id()) for _ in range(5)]
    for age, attempt in enumerate(attempts):
        _files(hub, attempt, mtime=OLD - age * 60)  # later ids are older
    expired = Expired()
    assert await hub.collect_garbage(expired, older_than=CUTOFF, limit=2) == 4
    assert expired.asked == [attempts[4], attempts[3]]
    assert await hub.collect_garbage(expired, older_than=CUTOFF, limit=2) == 4
    assert await hub.collect_garbage(expired, older_than=CUTOFF, limit=2) == 2
    assert sorted(expired.asked) == sorted(attempts)


async def test_an_unlink_failure_is_counted_not_raised(tmp_path: Path) -> None:
    if os.name != "posix" or os.geteuid() == 0:
        pytest.skip("needs POSIX permissions and a non-root user")
    hub = _hub(tmp_path)
    paths = _files(hub, str(new_attempt_id()))
    hub.root.chmod(0o500)  # no unlink in the folder
    try:
        assert await hub.collect_garbage(Expired(), older_than=CUTOFF, limit=10) == 0
    finally:
        hub.root.chmod(0o700)
    assert hub.stats.gc_failures == 2 and all(p.exists() for p in paths)


async def test_a_missing_folder_is_not_an_error(tmp_path: Path) -> None:
    hub = LogHub(tmp_path / "never-made", blobs=None, max_bytes=1000)
    assert await hub.collect_garbage(Expired(), older_than=CUTOFF, limit=10) == 0
