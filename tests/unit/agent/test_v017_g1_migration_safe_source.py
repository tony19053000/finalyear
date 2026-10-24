"""V0.17 G1 fix round 1: root reads a user's identity / config without trusting it.

``migrate-identity`` runs as root (``postinst``) over folders a user controls. A
symlink (file, folder or ancestor), a file of another owner, a folder others can
write, or a swap between the plan and the copy must make the run ``blocked``
with the reason - never make root read, copy or rename something else - and a
failed verify leaves no root-owned half copy in the target.
No root needed: a foreign owner is presented through ``safe_source._fstat``.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from meshforge.agent.identity import (
    CA_CERT_FILE_NAME,
    JSON_FILE_NAME,
    KEY_FILE_NAME,
    MIGRATION_PENDING_FILE_NAME,
    NODE_CERT_FILE_NAME,
    NodeIdentity,
    load_or_create,
)
from meshforge.agent.migration import (
    MigrationOutcome,
    Owner,
    Paths,
    execute,
    plan,
    safe_source,
)
from meshforge.common.errors import IdentityError

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="Linux package (G4: Windows)"),
]

_NO_OWNER = Owner(None, None)
_TODAY = "20261024"
_SECRET = b"root-only secret\n"


@pytest.fixture(autouse=True)
def _umask_022() -> Iterator[None]:
    """Folders made here are 0755 whatever the runner's umask (each test sets
    the mode it is about)."""
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        target_identity=tmp_path / "var/lib/meshforge/identity",
        target_config=tmp_path / "etc/meshforge/config.toml",
        home_root=tmp_path / "home",
    )


def _identity(tmp_path: Path, user: str = "asha") -> NodeIdentity:
    directory = tmp_path / "home" / user / ".local/share/meshforge/identity"
    identity = load_or_create(directory)
    (directory / NODE_CERT_FILE_NAME).write_bytes(b"cert")
    (directory / NODE_CERT_FILE_NAME).chmod(0o600)
    (directory / CA_CERT_FILE_NAME).write_bytes(b"ca")
    return identity


def _secret(tmp_path: Path) -> Path:
    path = tmp_path / "elsewhere/secret"
    path.parent.mkdir(parents=True)
    path.write_bytes(_SECRET)
    path.chmod(0o600)
    return path


def _target_files(paths: Paths) -> set[str]:
    if not paths.target_identity.exists():
        return set()
    return {p.name for p in paths.target_identity.iterdir()} - {
        MIGRATION_PENDING_FILE_NAME,
        "identity.lock",
    }


def _assert_blocked_clean(paths: Paths, words: str, problems: list[str]) -> None:
    assert words in "\n".join(problems), problems
    assert _target_files(paths) == set()  # no copy, not even partial
    with pytest.raises(IdentityError, match="migration"):
        load_or_create(paths.target_identity)
    for path in paths.target_identity.parent.rglob("*"):
        if path.is_file():
            assert _SECRET not in path.read_bytes()


def _run(paths: Paths) -> tuple[MigrationOutcome, list[str]]:
    chosen = plan(paths)
    execute(chosen, owner=_NO_OWNER, today=_TODAY)
    return chosen.outcome, chosen.problems


# --------------------------------------------------------------- symlinks


@pytest.mark.parametrize("name", [KEY_FILE_NAME, JSON_FILE_NAME, NODE_CERT_FILE_NAME])
def test_a_symlinked_identity_file_blocks(tmp_path: Path, name: str) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    secret = _secret(tmp_path)
    (identity.path / name).unlink()
    (identity.path / name).symlink_to(secret)
    outcome, problems = _run(paths)
    assert outcome is MigrationOutcome.BLOCKED
    _assert_blocked_clean(paths, "symbolic link", problems)
    assert (identity.path / name).is_symlink()  # the source is left as is
    assert secret.read_bytes() == _SECRET


def test_a_symlinked_identity_folder_blocks(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    real = load_or_create(tmp_path / "elsewhere/identity")
    link = tmp_path / "home/asha/.local/share/meshforge/identity"
    link.parent.mkdir(parents=True)
    link.symlink_to(real.path, target_is_directory=True)
    outcome, problems = _run(paths)
    assert outcome is MigrationOutcome.BLOCKED
    _assert_blocked_clean(paths, "symbolic link", problems)
    assert link.is_symlink() and real.path.is_dir()


def test_a_symlinked_ancestor_folder_blocks(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    load_or_create(tmp_path / "elsewhere/.local/share/meshforge/identity")
    home = tmp_path / "home/asha"
    home.mkdir(parents=True)
    (home / ".local").symlink_to(tmp_path / "elsewhere/.local")
    outcome, problems = _run(paths)
    assert outcome is MigrationOutcome.BLOCKED
    _assert_blocked_clean(paths, "symbolic link", problems)


def test_a_symlinked_config_blocks_the_migration(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    secret = _secret(tmp_path)
    config = tmp_path / "home/asha/.config/meshforge/config.toml"
    config.parent.mkdir(parents=True)
    config.symlink_to(secret)
    outcome, problems = _run(paths)
    assert outcome is MigrationOutcome.BLOCKED
    _assert_blocked_clean(paths, "symbolic link", problems)
    assert not paths.target_config.exists()
    assert identity.path.is_dir()


def test_a_symlinked_lock_file_is_not_followed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    secret = _secret(tmp_path)
    lock = identity.path / "identity.lock"
    lock.unlink()
    lock.symlink_to(secret)
    outcome, problems = _run(paths)
    assert outcome is MigrationOutcome.BLOCKED
    _assert_blocked_clean(paths, "symbolic link", problems)
    assert secret.read_bytes() == _SECRET
    assert identity.path.is_dir()  # not renamed


# --------------------------------------------------------- owners and modes


def _foreign(monkeypatch: pytest.MonkeyPatch, victim: Path) -> None:
    """Make ``victim`` look owned by another uid when ``fstat``-ed."""
    inode = victim.lstat().st_ino
    real = os.fstat

    def fake(fd: int) -> os.stat_result:
        st = real(fd)
        if st.st_ino != inode:
            return st
        fields = list(st)
        fields[4] = st.st_uid + 4242  # st_uid
        return os.stat_result(fields)

    monkeypatch.setattr(safe_source, "_fstat", fake)


@pytest.mark.parametrize("name", [KEY_FILE_NAME, CA_CERT_FILE_NAME])
def test_a_foreign_owned_file_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    _foreign(monkeypatch, identity.path / name)
    outcome, problems = _run(paths)
    assert outcome is MigrationOutcome.BLOCKED
    _assert_blocked_clean(paths, "belongs to uid", problems)


def test_a_foreign_owned_identity_folder_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    _foreign(monkeypatch, identity.path)
    outcome, problems = _run(paths)
    assert outcome is MigrationOutcome.BLOCKED
    _assert_blocked_clean(paths, "belongs to uid", problems)


@pytest.mark.parametrize("where", ["identity", "meshforge"])
def test_a_group_writable_folder_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    folder = identity.path if where == "identity" else identity.path.parent
    folder.chmod(0o770)
    monkeypatch.setattr(safe_source, "_is_private_group", lambda gid, uid: False)
    outcome, problems = _run(paths)
    assert outcome is MigrationOutcome.BLOCKED
    _assert_blocked_clean(paths, "writable by group", problems)


def test_a_group_writable_folder_of_a_private_group_is_fine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ubuntu's umask 002 + user private groups: every folder is 0775."""
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    identity.path.parent.chmod(0o775)
    monkeypatch.setattr(safe_source, "_is_private_group", lambda gid, uid: True)
    outcome, _ = _run(paths)
    assert outcome is MigrationOutcome.MIGRATED


def test_a_world_writable_folder_blocks(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    identity.path.parent.chmod(0o777)
    outcome, problems = _run(paths)
    assert outcome is MigrationOutcome.BLOCKED
    _assert_blocked_clean(paths, "writable by every user", problems)


# --------------------------------------------------- plan -> execute races


def test_a_symlink_planted_after_the_plan_blocks_at_execute(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    secret = _secret(tmp_path)
    chosen = plan(paths)
    planned: MigrationOutcome = chosen.outcome
    assert planned is MigrationOutcome.MIGRATED
    (identity.path / NODE_CERT_FILE_NAME).unlink()
    (identity.path / NODE_CERT_FILE_NAME).symlink_to(secret)
    report = execute(chosen, owner=_NO_OWNER, today=_TODAY)
    assert chosen.outcome is MigrationOutcome.BLOCKED
    assert int(report.exit_code) == 4
    _assert_blocked_clean(paths, "symbolic link", chosen.problems)
    assert identity.path.is_dir()


def test_a_failed_verify_leaves_no_partial_copy(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    other = load_or_create(tmp_path / "elsewhere/other")
    chosen = plan(paths)

    def corrupt(name: str) -> None:  # the copy is damaged before the verify
        if name == JSON_FILE_NAME:
            (paths.target_identity / JSON_FILE_NAME).write_bytes(
                (other.path / JSON_FILE_NAME).read_bytes()
            )

    execute(chosen, owner=_NO_OWNER, today=_TODAY, after_copy=corrupt)
    assert chosen.outcome is MigrationOutcome.BLOCKED
    _assert_blocked_clean(paths, "does not match", chosen.problems)
    marker = json.loads(
        (paths.target_identity / MIGRATION_PENDING_FILE_NAME).read_bytes()
    )
    assert marker["state"] == "blocked"
    assert identity.path.is_dir()  # the source is not renamed

    # The next run converges.
    outcome, _ = _run(paths)
    assert outcome is MigrationOutcome.MIGRATED
    assert load_or_create(paths.target_identity).node_id == identity.node_id


def test_a_folder_swapped_before_the_rename_is_put_back(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    decoy = identity.path.with_name("decoy")
    decoy.mkdir()
    chosen = plan(paths)

    def swap(name: str) -> None:
        if name == CA_CERT_FILE_NAME:  # the last file copied
            identity.path.rename(identity.path.with_name("identity.real"))
            decoy.rename(identity.path)

    report = execute(chosen, owner=_NO_OWNER, today=_TODAY, after_copy=swap)
    assert chosen.outcome is MigrationOutcome.MIGRATED  # the copy committed
    assert load_or_create(paths.target_identity).node_id == identity.node_id
    assert "was replaced" in "\n".join(chosen.notes)
    assert "(not renamed)" in "\n".join(report.actions)
    assert identity.path.is_dir() and not any(identity.path.iterdir())  # decoy
    assert (identity.path.with_name("identity.real") / KEY_FILE_NAME).is_file()
    assert not list(identity.path.parent.glob("*.migrated-*"))


def test_reading_goes_through_the_checked_fd(tmp_path: Path) -> None:
    """``read_file`` refuses a fifo/device rather than blocking or reading it."""
    folder = tmp_path / "f"
    folder.mkdir(mode=0o700)
    os.mkfifo(folder / "fifo")
    with safe_source.opened_dir(folder, os.getuid()) as held:
        with pytest.raises(safe_source.UnsafeSourceError, match="not a regular"):
            safe_source.read_file(held, "fifo")
        assert safe_source.read_file(held, "absent") is None
