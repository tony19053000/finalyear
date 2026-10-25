"""V0.17 G3: the G1 migration follow-ups.

(a) the owner's ``policy.toml`` follows a migrated identity (read through
``safe_source``, validated by the agent's own parser; a refusal is a note, never a
block, and leaves the source untouched); (b) no path-based ``check_config``
exists any more; (c) a lock root creates in a user's folder gets the user's
group too; (d) ``rename_aside`` reports both errors when the swap-back fails.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from meshforge.agent.identity import NodeIdentity, load_or_create
from meshforge.agent.migration import (
    MigrationOutcome,
    Owner,
    Paths,
    execute,
    migrate,
    plan,
    planning,
    safe_source,
)
from meshforge.agent.policy import (
    POLICY_FILE_NAME,
    LivePolicy,
    format_overlay,
    read_overlay,
)
from meshforge.agent.settings import PolicySettings

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="Linux package (G4: Windows)"),
]

_NO_OWNER = Owner(None, None)
_TODAY = "20261024"
_POLICY = format_overlay({"accept_jobs": True, "max_cpu_percent": 40})


@pytest.fixture(autouse=True)
def _umask_022() -> Iterator[None]:
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
    return load_or_create(tmp_path / "home" / user / ".local/share/meshforge/identity")


def _user_policy(identity: NodeIdentity, data: bytes = _POLICY) -> Path:
    path = identity.path.parent / POLICY_FILE_NAME
    path.write_bytes(data)
    path.chmod(0o600)
    return path


# ------------------------------------------------------------------ (a) policy


def test_the_owner_policy_moves_with_the_identity(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    source = _user_policy(identity)
    chosen = plan(paths)
    assert chosen.outcome is MigrationOutcome.MIGRATED
    assert chosen.policy.action == "copy"
    report = execute(chosen, owner=_NO_OWNER, today=_TODAY)
    target = paths.target_policy
    assert target == paths.target_identity.parent / POLICY_FILE_NAME
    assert target.read_bytes() == _POLICY
    assert (target.stat().st_mode & 0o777) == 0o600
    assert not source.exists()
    assert (source.parent / f"{POLICY_FILE_NAME}.migrated-{_TODAY}").is_file()
    assert any("owner policy" in a for a in report.actions)
    assert any("owner policy: copied" in line for line in report.lines())
    # the service reads it exactly as the agent does
    assert read_overlay(target) == {"accept_jobs": True, "max_cpu_percent": 40}
    live = LivePolicy(PolicySettings(), target, gpu_count=0, max_concurrent_attempts=4)
    assert live.state.invalid_reason is None
    assert live.policy.accept_jobs is True
    assert report.as_dict()["policy"]["action"] == "copy"


@pytest.mark.parametrize(
    ("content", "words"),
    [
        (b"nonsense = true\n", "unknown keys"),
        (b"accept_jobs = \n", "not valid TOML"),
        (b"max_cpu_percent = 400\n", "not valid"),
    ],
)
def test_an_invalid_policy_is_a_note_never_a_block(
    tmp_path: Path, content: bytes, words: str
) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    source = _user_policy(identity, content)
    chosen = plan(paths)
    assert chosen.outcome is MigrationOutcome.MIGRATED
    assert chosen.policy.action == "refused"
    assert words in (chosen.policy.problem or "")
    report = execute(chosen, owner=_NO_OWNER, today=_TODAY)
    assert chosen.outcome is MigrationOutcome.MIGRATED  # the identity still moved
    assert load_or_create(paths.target_identity).node_id == identity.node_id
    assert not paths.target_policy.exists()
    assert source.read_bytes() == content  # untouched, not renamed
    assert any("accepts no jobs" in line for line in report.lines())


def test_a_symlinked_policy_is_never_followed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    secret = tmp_path / "elsewhere/secret.toml"
    secret.parent.mkdir()
    secret.write_bytes(b"accept_jobs = true\n")
    (identity.path.parent / POLICY_FILE_NAME).symlink_to(secret)
    chosen = plan(paths)
    assert chosen.policy.action == "refused"
    assert "symbolic link" in (chosen.policy.problem or "")
    execute(chosen, owner=_NO_OWNER, today=_TODAY)
    assert not paths.target_policy.exists()
    assert (identity.path.parent / POLICY_FILE_NAME).is_symlink()


def test_a_policy_swapped_for_a_symlink_after_the_plan_is_refused(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    source = _user_policy(identity)
    chosen = plan(paths)
    assert chosen.policy.action == "copy"
    secret = tmp_path / "secret.toml"
    secret.write_bytes(b"accept_jobs = true\nallow_network = true\n")
    source.unlink()
    source.symlink_to(secret)
    execute(chosen, owner=_NO_OWNER, today=_TODAY)
    assert chosen.policy.action == "refused"
    assert not paths.target_policy.exists()


def test_a_policy_the_service_already_has_is_kept(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    source = _user_policy(identity)
    paths.target_policy.parent.mkdir(parents=True)
    paths.target_policy.write_bytes(b"accept_jobs = false\n")
    chosen = plan(paths)
    assert chosen.policy.action == "kept"
    execute(chosen, owner=_NO_OWNER, today=_TODAY)
    assert paths.target_policy.read_bytes() == b"accept_jobs = false\n"
    assert source.read_bytes() == _POLICY


def test_no_identity_no_policy_move(tmp_path: Path) -> None:
    """A policy without an identity is not this laptop's node: nothing moves."""
    paths = _paths(tmp_path)
    data_dir = tmp_path / "home/asha/.local/share/meshforge"
    data_dir.mkdir(parents=True)
    (data_dir / POLICY_FILE_NAME).write_bytes(_POLICY)
    chosen = plan(paths)
    assert chosen.outcome is MigrationOutcome.NOTHING_TO_MIGRATE
    assert chosen.policy.action == "none"
    execute(chosen, owner=_NO_OWNER, today=_TODAY)
    assert not paths.target_policy.exists()


def test_dry_run_writes_no_policy(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    identity = _identity(tmp_path)
    source = _user_policy(identity)
    report = migrate(
        explicit=None, config_from=None, dry_run=True, paths=paths, service_user=None
    )
    assert report.plan.policy.action == "copy"
    assert any("would copy" in line for line in report.lines())
    assert not paths.target_policy.exists()
    assert source.read_bytes() == _POLICY


# ------------------------------------------------------------ (b) check_config


def test_there_is_no_path_based_config_check() -> None:
    """Root reads a user's file only through ``safe_source``."""
    assert not hasattr(planning, "check_config")
    import meshforge.agent.migration as migration

    assert "check_config" not in migration.__all__


# ------------------------------------------------------------------ (c) lock


def test_a_lock_root_creates_gets_the_users_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "identity"
    folder.mkdir(mode=0o700)
    calls: list[tuple[int, int]] = []
    real_fchown = os.fchown

    def fake_fchown(fd: int, uid: int, gid: int) -> None:
        calls.append((uid, gid))
        real_fchown(fd, -1, -1)

    monkeypatch.setattr(safe_source, "_primary_gid", lambda uid: 4242)
    monkeypatch.setattr(os, "fchown", fake_fchown)
    with (
        safe_source.opened_dir(folder, os.getuid()) as held,
        safe_source.locked(held, timeout_s=1.0),
    ):
        pass
    assert calls == [(os.getuid(), 4242)]


def test_an_existing_lock_is_not_rechowned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "identity"
    folder.mkdir(mode=0o700)
    (folder / safe_source.LOCK_FILE_NAME).write_bytes(b"")
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(safe_source, "_primary_gid", lambda uid: 4242)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: calls.append((uid, gid)))
    with (
        safe_source.opened_dir(folder, os.getuid()) as held,
        safe_source.locked(held, timeout_s=1.0),
    ):
        pass
    assert calls == []


# ------------------------------------------------------------- (d) rename


def test_a_failed_swap_back_reports_both_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "data"
    (parent / "identity").mkdir(parents=True)
    fd = os.open(parent, os.O_RDONLY)
    real_rename = os.rename
    calls = 0

    def flaky_rename(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:  # the swap-back
            raise PermissionError(13, "Permission denied")
        real_rename(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "rename", flaky_rename)
    try:
        with pytest.raises(safe_source.UnsafeSourceError) as caught:
            safe_source.rename_aside(fd, parent, "identity", _TODAY, (0, 0))
    finally:
        os.close(fd)
    text = str(caught.value)
    assert "was replaced" in text
    assert "putting it back failed too" in text
    assert "Permission denied" in text
    assert f"identity.migrated-{_TODAY}" in text  # where the entry is now


def test_a_successful_swap_back_still_says_left_as_is(tmp_path: Path) -> None:
    parent = tmp_path / "data"
    (parent / "identity").mkdir(parents=True)
    fd = os.open(parent, os.O_RDONLY)
    try:
        with pytest.raises(safe_source.UnsafeSourceError, match="left as is"):
            safe_source.rename_aside(fd, parent, "identity", _TODAY, (0, 0))
    finally:
        os.close(fd)
    assert (parent / "identity").is_dir()
