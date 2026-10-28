"""CI fix round 4: concurrent ``migrate-identity`` runs are serialised.

package-linux (CI run 40652417064) ran four ``migrate-identity`` at once and one
left ``migration.pending`` beside a committed identity: a run that planned
``migrated`` while another was copying found the source already renamed, and
"blocked" the service. ``migrate`` now holds ``migrate-identity.lock`` from the
plan to the end of the execute, so a later run plans against what the earlier
one left (``target_present``).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.identity import (
    MIGRATION_PENDING_FILE_NAME,
    load_or_create,
    read_identity,
)
from meshforge.agent.migration import MigrationOutcome, MigrationReport, Paths, apply
from meshforge.common.errors import MeshForgeError
from meshforge.common.filelock import locked

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="Linux package (G4: Windows)"),
]


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        target_identity=tmp_path / "var/lib/meshforge/identity",
        target_config=tmp_path / "etc/meshforge/config.toml",
        home_root=tmp_path / "home",
    )


def _source(tmp_path: Path) -> tuple[Path, str]:
    directory = tmp_path / "home" / "asha" / ".local/share/meshforge/identity"
    return directory, load_or_create(directory).node_id


def _migrate(paths: Paths, *, dry_run: bool = False) -> MigrationReport:
    return apply.migrate(
        explicit=None,
        config_from=None,
        dry_run=dry_run,
        paths=paths,
        service_user=None,
    )


def test_overlapping_runs_converge_on_one_committed_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run B starts while run A is inside its execute (the CI interleaving).
    Without the lock B planned ``migrated`` from the not-yet-renamed source and
    then blocked the target; with it, B plans after A: ``target_present``."""
    paths = _paths(tmp_path)
    source, node_id = _source(tmp_path)
    a_executing = threading.Event()
    real_execute = apply.execute

    def slow_execute(*args: Any, **kwargs: Any) -> MigrationReport:
        if not a_executing.is_set():
            a_executing.set()
            threading.Event().wait(0.5)  # A is mid-run; B gets going now
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(apply, "execute", slow_execute)
    results: dict[str, MigrationReport] = {}
    errors: list[Exception] = []

    def run(name: str) -> None:
        try:
            results[name] = _migrate(paths)
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    a = threading.Thread(target=run, args=("a",))
    a.start()
    assert a_executing.wait(10)
    b = threading.Thread(target=run, args=("b",))
    b.start()
    a.join(30)
    b.join(30)
    assert not errors, errors
    assert results["a"].plan.outcome is MigrationOutcome.MIGRATED
    assert results["b"].plan.outcome is MigrationOutcome.TARGET_PRESENT
    assert not (paths.target_identity / MIGRATION_PENDING_FILE_NAME).exists()
    assert read_identity(paths.target_identity).node_id == node_id
    assert load_or_create(paths.target_identity).node_id == node_id  # startable
    assert not source.exists()
    assert len(list(source.parent.glob("identity.migrated-*"))) == 1


def test_a_held_lock_refuses_after_the_timeout_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    source, node_id = _source(tmp_path)
    monkeypatch.setattr(apply, "MIGRATE_LOCK_TIMEOUT_S", 0.2)
    lock = paths.target_identity.parent / apply.MIGRATE_LOCK_FILE_NAME
    lock.parent.mkdir(parents=True)
    with locked(lock, timeout_s=1.0):
        with pytest.raises(MeshForgeError, match="another migrate-identity run"):
            _migrate(paths)
        # a dry run only reads: it takes no lock and still answers
        assert _migrate(paths, dry_run=True).plan.outcome is MigrationOutcome.MIGRATED
    assert not paths.target_identity.exists()
    assert read_identity(source).node_id == node_id
    # released: the run goes through, and the lock file stays (never deleted)
    assert _migrate(paths).plan.outcome is MigrationOutcome.MIGRATED
    assert lock.is_file()
