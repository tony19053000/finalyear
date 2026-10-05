"""V0.5 G5 fix round 2 - the GC index is never quarantined under a live owner
(G5-R2-1) and a foreign/mis-versioned SQLite file is treated as corrupt
(G5-R2-2). Real SQLite files, real owner locks, the real ``start_gc``."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest
from mf_agent_harness import make_agent_settings

from meshforge.agent.cas import AgentCas
from meshforge.agent.runtime import start_gc
from meshforge.common.gc_index import DB_NAME, GcIndex, GcIndexBusyError

pytestmark = [pytest.mark.component, pytest.mark.timeout(120)]

BLOB = f"{1:064x}"


def _corrupt_under(live: GcIndex) -> None:
    assert live._conn is not None
    live._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with live.path.open("r+b") as fh:
        fh.write(os.urandom(8192))


def _quarantined(root: Path) -> list[Path]:
    return [p for p in root.iterdir() if ".corrupt-" in p.name]


def test_busy_while_a_live_owner_then_quarantined_once_it_is_gone(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gc"
    live = GcIndex(root)
    live.open()
    live.upsert("blob", BLOB, 1)
    live.lease("attempt", [("blob", BLOB)])
    _corrupt_under(live)
    second = GcIndex(root)
    with pytest.raises(GcIndexBusyError):
        second.open()
    assert _quarantined(root) == []  # the live owner's file stays in place
    # the failed open registered nothing: only the live owner's lock remains
    assert [p.stem for p in (root / "gc-owners").iterdir()] == [live.owner]
    with pytest.raises(GcIndexBusyError):
        second.open()  # still busy: retrying never guesses
    live.close()
    second.open()  # the other process is gone: now it is moved aside
    assert len(_quarantined(root)) == 1
    assert second.usage("blob") == 0  # fresh index
    second.close()


def test_a_dead_owners_lock_file_does_not_block_the_quarantine(tmp_path: Path) -> None:
    root = tmp_path / "gc"
    first = GcIndex(root)
    first.open()
    first.close()
    (root / "gc-owners" / "deadbeefdeadbeef.lock").touch()  # crashed process
    root.joinpath(DB_NAME).write_bytes(os.urandom(8192))
    idx = GcIndex(root)
    idx.open()
    assert len(_quarantined(root)) == 1
    idx.close()


@pytest.mark.asyncio
async def test_start_gc_runs_without_gc_while_the_index_is_busy(tmp_path: Path) -> None:
    settings = make_agent_settings(
        tmp_path / "data",
        "127.0.0.1:1",
        gc={"cas_bytes": 1, "min_age_s": 0, "interval_s": 3600},
    )
    live = GcIndex(settings.gc_dir)
    live.open()
    live.upsert("blob", BLOB, 1)
    live.lease("attempt", [("blob", BLOB)])
    _corrupt_under(live)
    cas = AgentCas.at(settings.data_dir)
    assert await start_gc(settings, cas, node_id="node_x") is None
    assert _quarantined(settings.gc_dir) == []
    live.close()
    service = await start_gc(settings, cas, node_id="node_x")
    assert service is not None
    await service.close()


def _foreign(root: Path, sql: str, version: int = 0) -> None:
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / DB_NAME)
    db.executescript(sql)
    db.execute(f"PRAGMA user_version = {version}")
    db.commit()
    db.close()


@pytest.mark.parametrize(
    ("sql", "version"),
    [
        ("CREATE TABLE item(id INTEGER PRIMARY KEY, name TEXT)", 0),
        ("CREATE TABLE notes(body TEXT)", 0),
        ("CREATE VIEW pin AS SELECT 1 AS kind, 2 AS key", 0),
        ("", 99),
        ("CREATE TABLE owner(owner TEXT PRIMARY KEY, started REAL NOT NULL)", 1),
    ],
    ids=[
        "clashing-table",
        "unknown-table",
        "view-named-pin",
        "newer-version",
        "v1-partial",
    ],
)
def test_foreign_or_misversioned_files_are_quarantined_and_rebuilt(
    tmp_path: Path, sql: str, version: int
) -> None:
    root = tmp_path / "gc"
    _foreign(root, sql, version)
    idx = GcIndex(root)
    idx.open()
    assert len(_quarantined(root)) == 1
    idx.upsert("blob", BLOB, 3)
    assert idx.usage("blob") == 3
    idx.close()
    again = GcIndex(root)
    again.open()  # the rebuilt file is accepted as is
    assert again.usage("blob") == 3
    assert len(_quarantined(root)) == 1
    again.close()


def test_a_foreign_file_under_a_live_owner_is_busy_not_quarantined(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gc"
    live = GcIndex(root)
    live.open()
    live.close()
    holder = GcIndex(root)
    holder.open()  # a live owner
    assert holder._conn is not None
    holder._conn.execute("PRAGMA user_version = 77")
    other = GcIndex(root)
    with pytest.raises(GcIndexBusyError):
        other.open()
    assert _quarantined(root) == []
    holder.close()


def test_an_index_from_before_blob_lru_is_migrated_not_quarantined(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gc"
    idx = GcIndex(root)
    idx.open()
    idx.upsert("blob", BLOB, 5)
    idx.close()
    db = sqlite3.connect(root / DB_NAME)
    db.executescript("DROP TABLE blob_lru; PRAGMA user_version = 0;")
    db.close()
    again = GcIndex(root)
    again.open()
    assert _quarantined(root) == []
    victims = again.victims("blob", cutoff=time.time() + 10, limit=5)
    assert [v.key for v in victims] == [BLOB]  # mirrored into blob_lru
    again.close()
