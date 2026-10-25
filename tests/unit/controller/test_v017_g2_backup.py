"""V0.17 G2 (D393): pre-upgrade backup decisions, names, rotation and the marker.

Pure (files in tmp_path only); the dumps themselves are component-tested.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from meshforge.controller.db_managed.backup import (
    ROLLBACK_FAILED_NAME,
    SchemaMarker,
    clear_marker,
    clear_rollback_failed,
    list_backups,
    manual_backup_path,
    needs_upgrade_backup,
    pre_upgrade_path,
    read_marker,
    read_rollback_failed,
    rotate,
    write_marker,
    write_rollback_failed,
)

pytestmark = pytest.mark.unit


def _touch(path: Path, age_s: int) -> Path:
    path.write_bytes(b"x" * 10)
    stamp = 1_700_000_000 - age_s
    os.utime(path, (stamp, stamp))
    return path


def test_names() -> None:
    assert pre_upgrade_path(Path("/b"), "0.17.1").name == "pre-0.17.1.dump"
    assert (
        pre_upgrade_path(Path("/b"), "1.0.0rc1+local").name == "pre-1.0.0rc1+local.dump"
    )
    # never a path: separators and dots-only collapse
    assert pre_upgrade_path(Path("/b"), "../../etc/x").parent == Path("/b")
    assert pre_upgrade_path(Path("/b"), "..").name == "pre-unknown.dump"
    when = dt.datetime(2026, 10, 25, 1, 2, 3, tzinfo=dt.UTC)
    assert manual_backup_path(Path("/b"), when).name == "manual-20261025T010203Z.dump"


def test_rotation_keeps_the_newest_pre_upgrade_only(tmp_path: Path) -> None:
    olds = [_touch(tmp_path / f"pre-0.17.{i}.dump", age_s=100 - i) for i in range(5)]
    manual = _touch(tmp_path / "manual-20200101T000000Z.dump", age_s=1000)
    partial = _touch(tmp_path / ".pre-0.18.0.dump.partial", age_s=1000)
    other = _touch(tmp_path / "notes.txt", age_s=1000)
    removed = rotate(tmp_path, keep=3)
    assert sorted(removed) == sorted(olds[:2])
    left = [b.path for b in list_backups(tmp_path)]
    assert left == [manual, *olds[2:]]  # oldest first; manual untouched
    assert partial.exists() and other.exists()
    assert rotate(tmp_path, keep=3) == []
    with pytest.raises(ValueError):
        rotate(tmp_path, keep=0)


def test_list_backups_kinds_and_missing_dir(tmp_path: Path) -> None:
    assert list_backups(tmp_path / "missing") == []
    _touch(tmp_path / "pre-1.dump", 5)
    _touch(tmp_path / "manual-x.dump", 4)
    _touch(tmp_path / "imported.dump", 3)
    (tmp_path / "dir.dump").mkdir()
    kinds = [(b.path.name, b.kind) for b in list_backups(tmp_path)]
    assert kinds == [
        ("pre-1.dump", "pre-upgrade"),
        ("manual-x.dump", "manual"),
        ("imported.dump", "other"),
    ]


@pytest.mark.parametrize(
    ("marker", "db_revision", "expected"),
    [
        (None, None, False),  # fresh database: nothing to protect
        (SchemaMarker("0.17.0", "0034", ""), None, False),
        (None, "0034", True),  # no marker (first G2 start, Q21 import)
        (SchemaMarker("0.17.0", "0034", ""), "0034", True),  # version changed
        (SchemaMarker("0.17.1", "0033", ""), "0033", True),  # behind head
        (SchemaMarker("0.17.1", "0034", ""), "0034", False),  # plain restart
    ],
)
def test_needs_upgrade_backup(
    marker: SchemaMarker | None, db_revision: str | None, expected: bool
) -> None:
    assert (
        needs_upgrade_backup(
            marker, version="0.17.1", db_revision=db_revision, head_revision="0034"
        )
        is expected
    )


def test_marker_round_trip_and_damage(tmp_path: Path) -> None:
    assert read_marker(tmp_path) is None
    write_marker(tmp_path, version="0.17.1", revision="0034")
    marker = read_marker(tmp_path)
    assert marker is not None and (marker.version, marker.revision) == (
        "0.17.1",
        "0034",
    )
    assert (tmp_path / "schema-version.json").stat().st_mode & 0o777 == 0o600
    (tmp_path / "schema-version.json").write_text("{not json")
    assert read_marker(tmp_path) is None  # damaged = "back up to be safe"
    (tmp_path / "schema-version.json").write_text('{"version": 3}')
    assert read_marker(tmp_path) is None
    clear_marker(tmp_path)
    clear_marker(tmp_path)  # idempotent
    assert read_marker(tmp_path) is None


def test_rollback_failed_record_round_trip_and_damage(tmp_path: Path) -> None:
    assert read_rollback_failed(tmp_path) is None
    write_rollback_failed(
        tmp_path,
        backup=tmp_path / "backups" / "pre-0.18.0.dump",
        from_version="0.17.0",
        to_version="0.18.0",
        error="ManagedDbError: " + "x" * 2000,
    )
    path = tmp_path / ROLLBACK_FAILED_NAME
    assert path.stat().st_mode & 0o777 == 0o600
    record = read_rollback_failed(tmp_path)
    assert record is not None
    assert record["backup"] == str(tmp_path / "backups" / "pre-0.18.0.dump")
    assert (record["from_version"], record["to_version"]) == ("0.17.0", "0.18.0")
    assert len(str(record["error"])) == 500 and record["at"]
    # A damaged record still says "the rollback failed" (details lost only).
    path.write_text("{not json")
    assert read_rollback_failed(tmp_path) == {"error": "damaged record"}
    path.write_text("[1, 2]")
    assert read_rollback_failed(tmp_path) == {"error": "damaged record"}
    clear_rollback_failed(tmp_path)
    clear_rollback_failed(tmp_path)  # idempotent
    assert read_rollback_failed(tmp_path) is None
