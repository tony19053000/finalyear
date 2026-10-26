"""V0.17 G4 (D387 on Windows): a developer's per-user identity moves to the service.

Windows candidates (``C:\\Users\\<name>\\AppData\\Local\\MeshForge\\identity``,
config from ``AppData\\Roaming``) and the **path backend** of ``safe_source``
(Windows has no ``openat``), driven on Linux by forcing
``safe_source.PATH_BACKEND``: the same rule matrix as G1 - one valid moves with
its node_id, two / damaged / linked block and touch nothing, a config naming a
profile path is refused - and the Windows wording (no ``sudo``). The real
Windows run is the ``package-windows`` CI job (seed -> install -> same node_id).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from meshforge.agent.identity import (
    JSON_FILE_NAME,
    KEY_FILE_NAME,
    NodeIdentity,
    load_or_create,
    read_identity,
)
from meshforge.agent.migration import (
    MigrationOutcome,
    Owner,
    Paths,
    execute,
    plan,
    safe_source,
)
from meshforge.agent.migration import model as mig_model
from meshforge.agent.migration.planning import (
    check_config_bytes,
    profile_name,
    windows_home_of,
)
from meshforge.common.errors import IdentityError

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="symlinks/hard links need privileges there; package-windows CI "
        "runs the real migration",
    ),
]

_NO_OWNER = Owner(None, None)
_TODAY = "20261025"


@pytest.fixture(autouse=True)
def _path_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(safe_source, "PATH_BACKEND", True)
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        target_identity=tmp_path / "ProgramData/MeshForge/identity",
        target_config=tmp_path / "ProgramData/MeshForge/config/config.toml",
        home_root=tmp_path / "Users",
        user_identity=mig_model.WINDOWS_USER_IDENTITY,
        user_config=mig_model.WINDOWS_USER_CONFIG,
    )


def _profile(tmp_path: Path, user: str) -> Path:
    return tmp_path / "Users" / user


def _user_identity(tmp_path: Path, user: str) -> NodeIdentity:
    return load_or_create(_profile(tmp_path, user) / mig_model.WINDOWS_USER_IDENTITY)


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


def _no_owner_name(path: Path) -> str | None:
    """``planning._user_of`` on Windows: no pwd, the profile folder names the user."""
    return None


def _run(paths: Paths, **kwargs: object) -> MigrationOutcome:
    kwargs.setdefault("user_of", _no_owner_name)
    chosen = plan(paths, **kwargs)  # type: ignore[arg-type]
    execute(chosen, owner=_NO_OWNER, today=_TODAY)
    return chosen.outcome


# --------------------------------------------------------------------- matrix
def test_one_identity_in_a_profile_moves_with_its_node_id(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    original = _user_identity(tmp_path, "asha")
    chosen = plan(paths, user_of=_no_owner_name)
    assert chosen.outcome is MigrationOutcome.MIGRATED
    assert chosen.user == "asha"  # from the profile folder (no pwd on Windows)
    report = execute(chosen, owner=_NO_OWNER, today=_TODAY)
    assert int(report.exit_code) == 0
    moved = load_or_create(paths.target_identity)
    assert (moved.node_id, moved.fingerprint) == (
        original.node_id,
        original.fingerprint,
    )
    # the source is renamed aside, never deleted (D387)
    local = _profile(tmp_path, "asha") / "AppData/Local/MeshForge"
    renamed = local / f"identity.migrated-{_TODAY}"
    assert read_identity(renamed).node_id == original.node_id
    assert not (local / "identity").exists()


def test_the_installing_users_profile_wins_over_the_search(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    asha = _user_identity(tmp_path, "asha")
    _user_identity(tmp_path, "ravi")
    homes = {"asha": _profile(tmp_path, "asha")}
    chosen = plan(paths, sudo_user="asha", home_of=homes.get)
    assert chosen.outcome is MigrationOutcome.MIGRATED
    assert chosen.identity is not None and chosen.identity.node_id == asha.node_id
    assert chosen.user == "asha"


def test_an_installer_user_without_an_identity_finds_the_other_profiles(
    tmp_path: Path,
) -> None:
    # Over-the-shoulder UAC: the elevated admin has no identity; the one found
    # by the search is attributed to its own profile, not to the admin.
    paths = _paths(tmp_path)
    _profile(tmp_path, "admin").mkdir(parents=True)
    _user_identity(tmp_path, "asha")
    homes = {"admin": _profile(tmp_path, "admin")}
    chosen = plan(paths, sudo_user="admin", home_of=homes.get, user_of=_no_owner_name)
    assert chosen.outcome is MigrationOutcome.MIGRATED
    assert chosen.user == "asha"


def test_two_profiles_with_identities_block_and_say_it_the_windows_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mig_model, "_WINDOWS", True)
    paths = _paths(tmp_path)
    _user_identity(tmp_path, "asha")
    _user_identity(tmp_path, "ravi")
    before = _snapshot(tmp_path / "Users")
    chosen = plan(paths)
    report = execute(chosen, owner=_NO_OWNER, today=_TODAY)
    assert chosen.outcome is MigrationOutcome.BLOCKED
    assert int(report.exit_code) == 4
    text = "\n".join(report.lines())
    assert "asha" in text and "ravi" in text
    assert "sudo" not in text
    assert "installer again" in text
    assert "sc.exe start MeshForgeAgent" in text
    assert _snapshot(tmp_path / "Users") == before
    with pytest.raises(IdentityError, match="migration"):
        load_or_create(paths.target_identity)


def test_a_damaged_identity_blocks_untouched(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    identity = _user_identity(tmp_path, "asha")
    (identity.path / JSON_FILE_NAME).write_text("{not json", encoding="utf-8")
    before = _snapshot(tmp_path / "Users")
    assert _run(paths) is MigrationOutcome.BLOCKED
    assert _snapshot(tmp_path / "Users") == before
    assert not (paths.target_identity / KEY_FILE_NAME).exists()


def test_a_linked_folder_on_the_way_is_refused(tmp_path: Path) -> None:
    # A junction / symlink anywhere on the path (here: AppData\Local) could
    # point the elevated installer at someone else's files.
    paths = _paths(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    load_or_create(elsewhere / "MeshForge" / "identity")
    appdata = _profile(tmp_path, "asha") / "AppData"
    appdata.mkdir(parents=True)
    (appdata / "Local").symlink_to(elsewhere, target_is_directory=True)
    chosen = plan(paths)
    execute(chosen, owner=_NO_OWNER, today=_TODAY)
    assert chosen.outcome is MigrationOutcome.BLOCKED
    assert "symbolic link or junction" in "\n".join(chosen.problems)
    assert not (paths.target_identity / KEY_FILE_NAME).exists()


def test_a_hard_linked_key_is_refused(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    identity = _user_identity(tmp_path, "asha")
    os.link(identity.path / KEY_FILE_NAME, tmp_path / "another-name")
    chosen = plan(paths)
    assert chosen.outcome is MigrationOutcome.BLOCKED
    assert "hard links" in "\n".join(chosen.problems)


def test_a_folder_swapped_after_the_check_is_refused(tmp_path: Path) -> None:
    identity = _user_identity(tmp_path, "asha")
    held = safe_source.open_dir(identity.path, os.getuid())
    moved = identity.path.with_name("identity.real")
    identity.path.rename(moved)
    load_or_create(identity.path)  # a different folder under the same name
    with pytest.raises(safe_source.UnsafeSourceError, match="replaced"):
        safe_source.read_file(held, KEY_FILE_NAME)


def test_rename_aside_refuses_an_entry_that_is_not_the_one_read(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "Local"
    (parent / "identity").mkdir(parents=True)
    with pytest.raises(safe_source.UnsafeSourceError, match="replaced"):
        safe_source.rename_aside(-1, parent, "identity", _TODAY, (0, 0))
    # put back under its own name
    assert (parent / "identity").is_dir()
    assert not list(parent.glob("identity.migrated-*"))


def test_a_lock_held_by_the_dev_agent_times_out_with_the_reason(
    tmp_path: Path,
) -> None:
    from meshforge.agent.identity import locked_identity

    identity = _user_identity(tmp_path, "asha")
    held = safe_source.open_dir(identity.path, os.getuid())
    with (
        locked_identity(identity.path),
        pytest.raises(safe_source.UnsafeSourceError, match="locked by another"),
        safe_source.locked(held, timeout_s=0.05),
    ):
        pass


# ---------------------------------------------------------------- config
def test_the_roaming_config_moves_with_the_identity(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _user_identity(tmp_path, "asha")
    source = _profile(tmp_path, "asha") / mig_model.WINDOWS_USER_CONFIG
    source.parent.mkdir(parents=True)
    text = '[agent]\ncontroller_address = "10.0.0.2:47100"\n'
    source.write_text(text, encoding="utf-8")
    assert _run(paths) is MigrationOutcome.MIGRATED
    assert paths.target_config.read_text(encoding="utf-8") == text
    assert source.with_name(f"config.toml.migrated-{_TODAY}").is_file()


@pytest.mark.parametrize(
    "value",
    [
        r"C:\Users\asha\envs",
        "c:/users/asha/cache",
        r"D:\Documents and Settings\asha\x",
    ],
)
def test_a_config_naming_a_profile_path_is_refused(value: str) -> None:
    raw = f"[agent.envs]\ndir = '{value}'\n".encode()
    problem = check_config_bytes(raw, Path("config.toml"))
    assert problem is not None and "home directory" in problem


def test_a_config_naming_programdata_is_fine() -> None:
    raw = b"[agent.envs]\ndir = 'C:\\\\ProgramData\\\\MeshForge\\\\cache\\\\envs'\n"
    assert check_config_bytes(raw, Path("config.toml")) is None


# ------------------------------------------------------------- profile lookup
def test_windows_home_of_prefers_userprofile_then_the_profile_folder(
    tmp_path: Path,
) -> None:
    users = tmp_path / "Users"
    (users / "asha").mkdir(parents=True)
    env = {"USERNAME": "Admin", "USERPROFILE": r"C:\Users\Admin"}
    assert windows_home_of("admin", env, users) == Path(r"C:\Users\Admin")
    assert windows_home_of("asha", env, users) == users / "asha"
    assert windows_home_of("nobody", env, users) is None
    for bad in ("", "..", r"..\x", "a/b"):
        assert windows_home_of(bad, env, users) is None


def test_profiles_root_comes_from_public_else_the_system_drive() -> None:
    root = mig_model.windows_profiles_root({"PUBLIC": "/x/Users/Public"})
    assert root == Path("/x/Users")
    assert mig_model.windows_profiles_root({"SystemDrive": "D:"}) == Path("D:\\/Users")


def test_profile_name_is_the_first_folder_below_the_root(tmp_path: Path) -> None:
    root = tmp_path / "Users"
    assert profile_name(root / "asha" / "AppData" / "x", root) == "asha"
    assert profile_name(tmp_path / "elsewhere", root) is None


def test_hints_name_the_windows_ways(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mig_model, "_WINDOWS", True)
    assert "installer again" in mig_model.rerun_hint()
    assert "sc.exe start MeshForgeAgent" in mig_model.start_hint()
    assert mig_model.example_identity_dir().startswith("C:\\Users\\")
    monkeypatch.setattr(mig_model, "_WINDOWS", False)
    assert mig_model.rerun_hint() == "`sudo meshforge-agent migrate-identity`"
