"""Carry out a planned identity migration (``execute``) and the command (``migrate``)."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from meshforge.agent.identity import (
    DEFAULT_LOCK_TIMEOUT_S,
    MIGRATION_PENDING_FILE_NAME,
    NodeIdentity,
    locked_identity,
    read_identity,
)
from meshforge.agent.migration import safe_source as _safe
from meshforge.agent.migration.model import (
    IDENTITY_FILES,
    SERVICE_USER,
    ConfigPlan,
    MigrationOutcome,
    MigrationPlan,
    MigrationReport,
    Owner,
    Paths,
    rerun_hint,
    system_paths,
)
from meshforge.agent.migration.planning import (
    _home_of,
    check_config_bytes,
    identity_from,
    plan,
    policy_problem,
)
from meshforge.agent.migration.safe_source import (
    OpenDir,
    ReadFile,
    UnsafeSourceError,
    held_key,
    locked,
    opened_dir,
    read_file,
    read_files,
    rename_aside,
)
from meshforge.agent.migration.user_units import running_user_agents, stop_user_unit
from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.errors import IdentityError, MeshForgeError
from meshforge.common.filelock import locked as file_locked
from meshforge.common.logging import get_logger

log = get_logger(__name__)

MIGRATE_LOCK_FILE_NAME: Final = "migrate-identity.lock"
"""Serialises whole ``migrate-identity`` runs (plan + execute); in the folder
that holds the target identity folder. Never deleted (``common.filelock``)."""
MIGRATE_LOCK_TIMEOUT_S: Final = 300.0
"""A run may stop a user unit and copy a few small files: minutes at most."""


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


def _block(
    chosen: MigrationPlan, owner: Owner, report: MigrationReport
) -> MigrationReport:
    target = chosen.paths.target_identity
    chosen.outcome = MigrationOutcome.BLOCKED
    _ensure_target_dir(target, owner)
    _write_marker(target, "blocked", "; ".join(chosen.problems))
    _chown(target / MIGRATION_PENDING_FILE_NAME, owner)
    report.actions.append(
        f"wrote {target / MIGRATION_PENDING_FILE_NAME} (the service refuses to "
        "start a new identity until migration succeeds)"
    )
    return report


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
    called after each copied file (crash injection).

    Every source is re-read here through checked fds (``safe_source``), all of
    it *before* the first write; a source that changed since :func:`plan` (or is
    no longer safe to read) turns the run into ``blocked`` with nothing copied."""
    report = MigrationReport(chosen, dry_run=False)
    today = today or datetime.now(UTC).strftime("%Y%m%d")
    target = chosen.paths.target_identity

    if chosen.outcome is MigrationOutcome.BLOCKED:
        return _block(chosen, owner, report)

    with ExitStack() as stack:
        config, blocked = _reread_config(chosen, stack)
        if blocked:
            return _block(chosen, owner, report)
        if chosen.outcome is MigrationOutcome.NOTHING_TO_MIGRATE:
            _clear_stale_marker(target, report)
        elif chosen.outcome is MigrationOutcome.TARGET_PRESENT:
            _rename_leftover(chosen, report, today)
        else:
            if stop_unit is not None and chosen.user is not None:
                stopped = stop_unit(chosen.user)
                if stopped is not None:
                    ok, text = stopped
                    (report.actions if ok else chosen.notes).append(text)
            try:
                _copy_identity(chosen, owner, report, today, after_copy)
            except (UnsafeSourceError, IdentityError) as exc:
                chosen.problems.append(f"the identity was not migrated: {exc}")
                chosen.notes.append(
                    f"fix: resolve the problem above, then re-run {rerun_hint()}"
                )
                return _block(chosen, owner, report)
            _copy_policy(chosen, owner, today, report)
        if config is not None:
            _copy_config(chosen, config, config_group, today, report)
    return report


def _open_planned_config(
    plan_config: ConfigPlan, stack: ExitStack
) -> tuple[OpenDir, ReadFile]:
    source, uid = plan_config.source, plan_config.uid
    if source is None or uid is None:
        raise UnsafeSourceError(Path(), "internal: a config plan without its source")
    folder = stack.enter_context(opened_dir(source.parent, uid))
    found = read_file(folder, source.name)
    if found is None:
        raise UnsafeSourceError(source, f"{source} disappeared")
    return folder, found


def _reread_config(
    chosen: MigrationPlan, stack: ExitStack
) -> tuple[tuple[OpenDir, ReadFile] | None, bool]:
    """The config planned for copying, re-read and re-checked now (held open
    until it is renamed). Returns it (or ``None``) and whether the run is now
    blocked: a refused config blocks a migration, otherwise it is a note."""
    plan_config = chosen.config
    if plan_config.action != "copy" or plan_config.source is None:
        return None, False
    source = plan_config.source
    try:
        opened = _open_planned_config(plan_config, stack)
    except UnsafeSourceError as exc:
        problem: str | None = str(exc)
    else:
        problem = check_config_bytes(opened[1].data, source)
        if problem is None:
            return opened, False
    problem = problem or "config refused"
    plan_config.action, plan_config.problem = "refused", problem
    if chosen.outcome is not MigrationOutcome.MIGRATED:
        chosen.notes.append(
            f"config not migrated: {problem}; the service starts from defaults"
        )
        return None, False
    chosen.problems.append(problem)
    chosen.notes.append(
        "fix: correct the file named above (or move it aside to start from "
        f"defaults) and re-run {rerun_hint()}"
    )
    return None, True


def _rename_leftover(
    chosen: MigrationPlan, report: MigrationReport, today: str
) -> None:
    leftover, uid = chosen.leftover_source, chosen.source_uid
    if leftover is None or uid is None:
        return
    try:
        with opened_dir(leftover, uid) as held:
            renamed = rename_aside(
                held.parent_fd,
                leftover.parent,
                leftover.name,
                today,
                held_key(held),
            )
    except (UnsafeSourceError, OSError) as exc:
        chosen.notes.append(f"{leftover} was not renamed: {exc}")
        return
    report.actions.append(f"renamed {leftover} -> {renamed}")


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
    source, expected, uid = chosen.source, chosen.identity, chosen.source_uid
    if source is None or expected is None or uid is None:
        raise MeshForgeError("internal: a migration plan without its source")
    target = chosen.paths.target_identity
    _ensure_target_dir(target, owner)
    with ExitStack() as stack:
        held = stack.enter_context(opened_dir(source, uid))
        source_lock = ExitStack()
        stack.enter_context(source_lock)
        source_lock.enter_context(locked(held, timeout_s=DEFAULT_LOCK_TIMEOUT_S))
        stack.enter_context(locked_identity(target))
        # Re-read under the lock, through the checked fds: the lock holder may
        # have just finished a key rotation (the same node with a new key).
        files = read_files(held, IDENTITY_FILES)
        current = identity_from(files, source)
        if current.node_id != expected.node_id:
            raise IdentityError(
                "the identity changed while it was being migrated",
                source,
                f"re-run {rerun_hint()}",
            )
        _write_marker(target, "copying", f"from {source}")
        try:
            for name in IDENTITY_FILES:  # a half copy from a crashed run
                (target / name).unlink(missing_ok=True)
            for name, found in files.items():
                atomic_write_bytes(target / name, found.data, mode=0o600)
                if after_copy is not None:
                    after_copy(name)
            _verify_copy(target, current)
        except BaseException:
            # No root-owned half copy stays behind; the marker does (it keeps
            # the service from minting until a run succeeds).
            for name in IDENTITY_FILES:
                (target / name).unlink(missing_ok=True)
            raise
        for path in [target, *sorted(target.iterdir())]:
            if path.name != MIGRATION_PENDING_FILE_NAME:
                _chown(path, owner)
        (target / MIGRATION_PENDING_FILE_NAME).unlink()  # the commit point
        _fsync_dir(target)
        source_inode = held_key(held)
        if _safe.PATH_BACKEND:
            # Windows cannot rename a folder while a file in it (its lock) is
            # open; the copy is committed, so the source lock is done with.
            source_lock.close()
        try:
            renamed: Path | str = rename_aside(
                held.parent_fd, source.parent, source.name, today, source_inode
            )
        except (UnsafeSourceError, OSError) as exc:
            renamed = "(not renamed)"
            chosen.notes.append(
                f"{source} was copied but not renamed: {exc}; rename or remove it "
                "by hand (the service now uses its own copy)"
            )
    report.actions.append(
        f"copied node {current.node_id} into {target} (key fingerprint "
        f"{current.fingerprint[:16]}... verified) and renamed {source} -> {renamed}"
    )
    log.info(
        "agent.identity.migrated",
        node_id=current.node_id,
        key_fingerprint=current.fingerprint,
    )


def _verify_copy(target: Path, current: NodeIdentity) -> None:
    copied = read_identity(target)
    if (copied.node_id, copied.fingerprint) != (current.node_id, current.fingerprint):
        raise IdentityError(
            "the copied identity does not match its source (node_id or key "
            "fingerprint differs); nothing was committed",
            target,
            f"re-run {rerun_hint()}",
        )


def _copy_config(
    chosen: MigrationPlan,
    config: tuple[OpenDir, ReadFile],
    group: int | None,
    today: str,
    report: MigrationReport,
) -> None:
    folder, found = config
    source, target = chosen.config.source, chosen.paths.target_config
    if source is None:  # pragma: no cover - _reread_config returned it
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, found.data, mode=0o640)
    if group is not None:
        os.chown(target, 0, group)
    try:
        renamed = rename_aside(
            folder.fd, folder.path, source.name, today, (found.dev, found.ino)
        )
    except (UnsafeSourceError, OSError) as exc:
        chosen.notes.append(f"{source} was copied but not renamed: {exc}")
        return
    report.actions.append(f"renamed {source} -> {renamed.name}")


def _copy_policy(
    chosen: MigrationPlan, owner: Owner, today: str, report: MigrationReport
) -> None:
    """After the identity committed: the owner's ``policy.toml`` follows it
    (G1 follow-up a), re-read through ``safe_source`` and re-validated now.
    Never blocks: a refusal is a note and the service keeps its safe default
    (accepts no jobs)."""
    planned = chosen.policy
    source, uid = planned.source, planned.uid
    if planned.action != "copy" or source is None or uid is None:
        return
    target = chosen.paths.target_policy

    def refuse(problem: str) -> None:
        planned.action, planned.problem = "refused", problem

    try:
        with opened_dir(source.parent, uid) as folder:
            found = read_file(folder, source.name)
            if found is None:
                refuse(f"{source} disappeared")
                return
            problem = policy_problem(found.data, source)
            if problem is not None:
                refuse(problem)
                return
            if os.path.lexists(target):
                planned.action = "kept"
                return
            atomic_write_bytes(target, found.data, mode=0o600)
            _chown(target, owner)
            try:
                renamed = rename_aside(
                    folder.fd, folder.path, source.name, today, (found.dev, found.ino)
                )
            except (UnsafeSourceError, OSError) as exc:
                chosen.notes.append(f"{source} was copied but not renamed: {exc}")
                return
    except (UnsafeSourceError, OSError) as exc:
        refuse(str(exc))
        return
    report.actions.append(f"copied the owner policy; renamed {source} -> {renamed}")


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
    """The command: :func:`plan`, then :func:`execute` unless ``dry_run``.

    A real run holds :data:`MIGRATE_LOCK_FILE_NAME` (beside the target
    identity folder) from the plan to the end of the execute, so concurrent
    runs (two package hooks, an admin re-run) happen one after the other: the
    later one plans against what the earlier one left (``target_present``)
    instead of acting on a plan the other run has made stale - which could
    block a committed identity or clear another run's half copy (CI fix
    round 4). A dry run only reads and takes no lock."""
    paths = paths or system_paths()
    if dry_run:
        return _plan_and_execute(
            paths,
            explicit=explicit,
            config_from=config_from,
            dry_run=True,
            service_user=service_user,
            sudo_user=sudo_user,
        )
    lock_dir = paths.target_identity.parent
    lock_dir.mkdir(parents=True, exist_ok=True)
    try:
        with file_locked(
            lock_dir / MIGRATE_LOCK_FILE_NAME, timeout_s=MIGRATE_LOCK_TIMEOUT_S
        ):
            return _plan_and_execute(
                paths,
                explicit=explicit,
                config_from=config_from,
                dry_run=False,
                service_user=service_user,
                sudo_user=sudo_user,
            )
    except TimeoutError:
        raise MeshForgeError(
            f"another migrate-identity run holds {lock_dir / MIGRATE_LOCK_FILE_NAME}"
            f" (waited {MIGRATE_LOCK_TIMEOUT_S:g} s); let it finish, then re-run "
            f"{rerun_hint()}"
        ) from None


def _plan_and_execute(
    paths: Paths,
    *,
    explicit: Path | None,
    config_from: Path | None,
    dry_run: bool,
    service_user: str | None,
    sudo_user: str | None,
) -> MigrationReport:
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
