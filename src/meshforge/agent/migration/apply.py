"""Carry out a planned identity migration (``execute``) and the command (``migrate``)."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

from meshforge.agent.identity import (
    MIGRATION_PENDING_FILE_NAME,
    locked_identity,
    read_identity,
)
from meshforge.agent.migration.model import (
    IDENTITY_FILES,
    SERVICE_USER,
    MigrationOutcome,
    MigrationPlan,
    MigrationReport,
    Owner,
    Paths,
    system_paths,
)
from meshforge.agent.migration.planning import _home_of, plan
from meshforge.agent.migration.user_units import running_user_agents, stop_user_unit
from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.errors import IdentityError, MeshForgeError
from meshforge.common.logging import get_logger

log = get_logger(__name__)


def _service_owner(user: str | None) -> Owner:
    if user is None or sys.platform == "win32":
        return Owner(None, None)
    import pwd

    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        raise MeshForgeError(
            f"the service user {user!r} does not exist (the package creates it); "
            "reinstall the package or create the user"
        ) from None
    return Owner(entry.pw_uid, entry.pw_gid)


def _chown(path: Path, owner: Owner) -> None:
    if owner.uid is not None or owner.gid is not None:
        os.chown(
            path,
            -1 if owner.uid is None else owner.uid,
            -1 if owner.gid is None else owner.gid,
            follow_symlinks=False,
        )


def _write_marker(directory: Path, state: str, detail: str) -> None:
    payload = {
        "state": state,
        "detail": detail,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    atomic_write_bytes(
        directory / MIGRATION_PENDING_FILE_NAME,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(),
        mode=0o600,
    )


def _ensure_target_dir(directory: Path, owner: Owner) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        directory.chmod(0o700)
    _chown(directory, owner)


def _migrated_name(source: Path, today: str) -> Path:
    candidate = source.with_name(f"{source.name}.migrated-{today}")
    n = 2
    while candidate.exists():
        candidate = source.with_name(f"{source.name}.migrated-{today}-{n}")
        n += 1
    return candidate


def execute(
    chosen: MigrationPlan,
    *,
    owner: Owner,
    config_group: int | None = None,
    today: str | None = None,
    stop_unit: Callable[[str], tuple[bool, str] | None] | None = None,
    after_copy: Callable[[str], None] | None = None,
) -> MigrationReport:
    """Carry out ``chosen`` (module docstring). ``after_copy`` is a test hook
    called after each copied file (crash injection)."""
    report = MigrationReport(chosen, dry_run=False)
    paths = chosen.paths
    today = today or datetime.now(UTC).strftime("%Y%m%d")
    target = paths.target_identity

    if chosen.outcome is MigrationOutcome.BLOCKED:
        _ensure_target_dir(target, owner)
        _write_marker(target, "blocked", "; ".join(chosen.problems))
        _chown(target / MIGRATION_PENDING_FILE_NAME, owner)
        report.actions.append(
            f"wrote {target / MIGRATION_PENDING_FILE_NAME} (the service refuses to "
            "start a new identity until migration succeeds)"
        )
        return report

    if chosen.outcome is MigrationOutcome.NOTHING_TO_MIGRATE:
        _clear_stale_marker(target, report)
    elif chosen.outcome is MigrationOutcome.TARGET_PRESENT:
        if chosen.leftover_source is not None:
            renamed = _migrated_name(chosen.leftover_source, today)
            chosen.leftover_source.rename(renamed)
            report.actions.append(f"renamed {chosen.leftover_source} -> {renamed}")
    else:
        if stop_unit is not None and chosen.user is not None:
            stopped = stop_unit(chosen.user)
            if stopped is not None:
                ok, text = stopped
                (report.actions if ok else chosen.notes).append(text)
        _copy_identity(chosen, owner, report, today, after_copy)

    if chosen.config.action == "copy" and chosen.config.source is not None:
        _copy_config(chosen.config.source, paths.target_config, config_group, today)
        report.actions.append(
            f"renamed {chosen.config.source} -> "
            f"{chosen.config.source.name}.migrated-{today}"
        )
    return report


def _clear_stale_marker(target: Path, report: MigrationReport) -> None:
    marker = target / MIGRATION_PENDING_FILE_NAME
    if not marker.exists():
        return
    with locked_identity(target):
        for name in IDENTITY_FILES:  # a half copy from a crashed run, if any
            (target / name).unlink(missing_ok=True)
        marker.unlink()
    report.actions.append(
        f"removed {marker}: no per-user identity is left, so the service may "
        "create a new one"
    )


def _copy_identity(
    chosen: MigrationPlan,
    owner: Owner,
    report: MigrationReport,
    today: str,
    after_copy: Callable[[str], None] | None,
) -> None:
    source, expected = chosen.source, chosen.identity
    if source is None or expected is None:
        raise MeshForgeError("internal: a migration plan without its source")
    target = chosen.paths.target_identity
    _ensure_target_dir(target, owner)
    with ExitStack() as stack:
        stack.enter_context(locked_identity(source))
        stack.enter_context(locked_identity(target))
        # Re-read under the lock: the lock holder may have just finished a key
        # rotation (roll forward), which is the same node with a new key.
        current = read_identity(source)
        if current.node_id != expected.node_id:
            raise IdentityError(
                "the identity changed while it was being migrated",
                source,
                "re-run `sudo meshforge-agent migrate-identity`",
            )
        _write_marker(target, "copying", f"from {source}")
        for name in IDENTITY_FILES:  # a half copy from a crashed run
            (target / name).unlink(missing_ok=True)
        for name in IDENTITY_FILES:
            src = source / name
            if not src.is_file():
                continue
            atomic_write_bytes(target / name, src.read_bytes(), mode=0o600)
            if after_copy is not None:
                after_copy(name)
        copied = read_identity(target)
        if (copied.node_id, copied.fingerprint) != (
            current.node_id,
            current.fingerprint,
        ):
            raise IdentityError(
                "the copied identity does not match its source (node_id or key "
                "fingerprint differs); nothing was committed",
                target,
                "re-run `sudo meshforge-agent migrate-identity`",
            )
        for path in [target, *sorted(target.iterdir())]:
            if path.name != MIGRATION_PENDING_FILE_NAME:
                _chown(path, owner)
        (target / MIGRATION_PENDING_FILE_NAME).unlink()  # the commit point
        _fsync_dir(target)
    renamed = _migrated_name(source, today)
    source.rename(renamed)
    report.actions.append(
        f"copied node {current.node_id} into {target} (key fingerprint "
        f"{current.fingerprint[:16]}... verified) and renamed {source} -> {renamed}"
    )
    log.info(
        "agent.identity.migrated",
        node_id=current.node_id,
        key_fingerprint=current.fingerprint,
    )


def _copy_config(source: Path, target: Path, group: int | None, today: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, source.read_bytes(), mode=0o640)
    if group is not None:
        os.chown(target, 0, group)
    source.rename(source.with_name(f"{source.name}.migrated-{today}"))


def _fsync_dir(directory: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def migrate(
    *,
    explicit: Path | None,
    config_from: Path | None,
    dry_run: bool,
    paths: Paths | None = None,
    service_user: str | None = SERVICE_USER,
    sudo_user: str | None = None,
) -> MigrationReport:
    """The command: :func:`plan`, then :func:`execute` unless ``dry_run``."""
    paths = paths or system_paths()
    chosen = plan(
        paths, explicit=explicit, config_from=config_from, sudo_user=sudo_user
    )
    if chosen.outcome is MigrationOutcome.MIGRATED and chosen.user is not None:
        pids = running_user_agents(_uid_of(chosen.user))
        if pids:
            chosen.notes.append(
                f"{chosen.user} has a running `meshforge-agent run` (pid "
                f"{', '.join(map(str, pids))}) not managed by a user unit; stop it - "
                "two agents with one identity replace each other at the controller"
            )
    if dry_run:
        return MigrationReport(chosen, dry_run=True)
    owner = _service_owner(service_user)
    return execute(
        chosen,
        owner=owner,
        config_group=owner.gid,
        stop_unit=lambda user: stop_user_unit(user, _home_of(user)),
    )


def _uid_of(user: str) -> int | None:
    if sys.platform == "win32":  # pragma: no cover
        return None
    import pwd

    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None
