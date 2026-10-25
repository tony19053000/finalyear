"""V0.17 G4 tester round 2: B1 no-follow data removal, N4 console readers, `--` handling.

Windows junctions cannot be made here; POSIX symlinks (directory and file,
dangling, looping) stand in for them: ``remove_tree_no_follow`` must treat any
link as an entry to unlink, never as a directory to enter. (N1/N3 rollback and
migration tests are in ``test_v017_g4_tester_winsetup.py``.)
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent import wininstall as wi
from meshforge.agent.system_cli import AGENT_PROGRAM
from meshforge.common.system_cli import decide_windows, first_command

pytestmark = pytest.mark.unit
posix_only = pytest.mark.skipif(
    os.name != "posix", reason="symlinks stand in for junctions"
)


def _victim(tmp_path: Path) -> Path:
    v = tmp_path / "victim"
    (v / "sub").mkdir(parents=True)
    (v / "precious.txt").write_text("keep")
    (v / "sub" / "deep.txt").write_text("keep")
    return v


def _snapshot(v: Path) -> dict[str, str]:
    return {
        p.relative_to(v).as_posix(): (p.read_text() if p.is_file() else "<dir>")
        for p in sorted(v.rglob("*"))
    }


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "ProgramData" / "MeshForge"
    for d in ("config", "identity", "cache", "logs", "console", "envs"):
        (root / d).mkdir(parents=True)
    (root / "identity" / "node.key").write_text("k")
    (root / "logs" / "agent.log").write_text("l")
    return root


class Fixer:
    def __init__(self) -> None:
        self.paths: list[Path] = []
        self.was_link: list[bool] = []

    def __call__(self, path: Path) -> None:
        self.paths.append(path)
        self.was_link.append(path.is_symlink())


@posix_only
def test_clean_tree_removed_without_any_acl_call(tmp_path: Path) -> None:
    root = _root(tmp_path)
    expected = 1 + sum(1 for _ in root.rglob("*"))
    fixer = Fixer()
    assert wi.remove_tree_no_follow(root, fixer) == expected
    assert not root.exists() and fixer.paths == []


@posix_only
def test_dir_link_file_link_dangling_and_loop_inside_root_are_unlinked_not_followed(
    tmp_path: Path,
) -> None:
    root, victim = _root(tmp_path), _victim(tmp_path)
    before = _snapshot(victim)
    (root / "envs" / "trap").symlink_to(victim, target_is_directory=True)
    (root / "envs" / "filelink").symlink_to(victim / "precious.txt")
    (root / "cache" / "dangling").symlink_to(tmp_path / "nowhere")
    (root / "cache" / "loop_a").symlink_to(root / "cache" / "loop_b")
    (root / "cache" / "loop_b").symlink_to(root / "cache" / "loop_a")
    (root / "cache" / "up").symlink_to(
        tmp_path, target_is_directory=True
    )  # parent of root
    (root / "logs" / "to_root").symlink_to(
        root, target_is_directory=True
    )  # cycle into itself
    fixer = Fixer()
    wi.remove_tree_no_follow(root, fixer)
    assert not root.exists()
    assert _snapshot(victim) == before
    assert (tmp_path / "ProgramData").is_dir()
    assert fixer.paths == []


@posix_only
def test_deeply_nested_link_is_still_not_followed(tmp_path: Path) -> None:
    root, victim = _root(tmp_path), _victim(tmp_path)
    before = _snapshot(victim)
    deep = root / "envs" / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "trap").symlink_to(victim, target_is_directory=True)
    wi.remove_tree_no_follow(root, Fixer())
    assert not root.exists() and _snapshot(victim) == before


@posix_only
def test_root_itself_a_link_only_the_link_is_removed(tmp_path: Path) -> None:
    victim = _victim(tmp_path)
    before = _snapshot(victim)
    root = tmp_path / "ProgramData" / "MeshForge"
    root.parent.mkdir()
    root.symlink_to(victim, target_is_directory=True)
    fixer = Fixer()
    assert wi.remove_tree_no_follow(root, fixer) == 1
    assert not root.is_symlink() and not root.exists()
    assert _snapshot(victim) == before and fixer.paths == []


@posix_only
def test_missing_root_raises_rather_than_claiming_success(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        wi.remove_tree_no_follow(tmp_path / "absent", Fixer())


def _refuse_until_fixed(
    monkeypatch: pytest.MonkeyPatch, refused: set[Path], fixer: Fixer
) -> None:
    real_unlink, real_rmdir = os.unlink, os.rmdir

    def guard(real: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(path, *a, **k):  # type: ignore[no-untyped-def]
            p = Path(path)
            if p in refused and p not in fixer.paths:
                raise PermissionError(13, "Access is denied", str(p))
            return real(path, *a, **k)

        return wrapper

    monkeypatch.setattr(os, "unlink", guard(real_unlink))
    monkeypatch.setattr(os, "rmdir", guard(real_rmdir))


@posix_only
def test_nested_refused_deletes_fix_only_those_entries_then_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, victim = _root(tmp_path), _victim(tmp_path)
    before = _snapshot(victim)
    (root / "envs" / "trap").symlink_to(victim, target_is_directory=True)
    locked_file = root / "envs" / "job1" / "site" / "locked.bin"
    locked_file.parent.mkdir(parents=True)
    locked_file.write_text("x")
    locked_dir = root / "cache" / "lockeddir"
    locked_dir.mkdir()
    fixer = Fixer()
    _refuse_until_fixed(monkeypatch, {locked_file, locked_dir}, fixer)
    wi.remove_tree_no_follow(root, fixer)
    assert not root.exists()
    assert sorted(fixer.paths) == sorted([locked_file, locked_dir])
    assert not any(fixer.was_link)
    assert _snapshot(victim) == before
    for p in fixer.paths:
        for argv in wi.data_fix_argvs(p):
            assert argv[0] == "icacls" and argv[1] == str(p)
            assert "/T" not in argv and "/t" not in argv
            assert "/L" in argv  # the entry, never a link target
            assert any(a.startswith("*S-1-5-32-544") for a in argv)


@posix_only
def test_refusal_that_fixing_cannot_cure_raises_oserror_and_spares_the_victim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, victim = _root(tmp_path), _victim(tmp_path)
    before = _snapshot(victim)
    (root / "envs" / "trap").symlink_to(victim, target_is_directory=True)
    stuck = root / "logs" / "agent.log"
    real_unlink = os.unlink

    def always(path, *a, **k):  # type: ignore[no-untyped-def]
        if Path(path) == stuck:
            raise PermissionError(13, "Access is denied", str(path))
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(os, "unlink", always)
    fixer = Fixer()
    with pytest.raises(OSError):
        wi.remove_tree_no_follow(root, fixer)
    assert fixer.paths == [stuck]  # one fix attempt, then the error; no loop
    assert _snapshot(victim) == before
    assert stuck.exists()


@posix_only
def test_unlistable_directory_gets_one_fix_then_is_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    locked = root / "identity"
    fixer = Fixer()
    real_listdir = os.listdir

    def listdir(path="."):  # type: ignore[no-untyped-def]
        if Path(path) == locked and locked not in fixer.paths:
            raise PermissionError(13, "Access is denied", str(path))
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", listdir)
    wi.remove_tree_no_follow(root, fixer)
    assert not root.exists() and fixer.paths == [locked]


def test_data_fix_argvs_shape_for_windows_paths() -> None:
    from pathlib import PureWindowsPath

    p = PureWindowsPath(r"C:\ProgramData\MeshForge\envs\job 1\a b.txt")
    argvs = wi.data_fix_argvs(p)
    assert len(argvs) == 2
    for argv in argvs:
        assert argv[:2] == ["icacls", str(p)]
        assert not {"/T", "/t", "/reset", "/RESET", "/inheritance:r"} & set(argv)
        assert "/L" in argv


# ---------------------------------------------------------------------- N4
SESSION, ADMIN, SVC = "S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-500", "S-1-5-18"


def test_console_readers_session_user_only_even_when_admin_elevated() -> None:
    assert wi.console_reader_sids(SESSION, ADMIN, excluded=[SVC]) == [SESSION]


def test_console_readers_fall_back_to_process_user_in_session_zero() -> None:
    assert wi.console_reader_sids(None, ADMIN, excluded=[SVC]) == [ADMIN]


def test_console_readers_never_include_excluded_service_identities() -> None:
    assert wi.console_reader_sids(None, SVC, excluded=[SVC]) == []
    assert wi.console_reader_sids(SVC, ADMIN, excluded=[SVC]) == []
    assert wi.console_reader_sids(SESSION, SESSION, excluded=[]) == [SESSION]


# ------------------------------------------------------------ `--` handling
V = frozenset({"--config"})


@pytest.mark.parametrize(
    ("args", "command"),
    [
        (["--", "run"], "run"),
        (["--"], None),
        ([], None),
        (["--", "--"], "--"),
        (["--", "--help"], "--help"),
        (["-v", "--", "run"], "run"),
        (["--config", "--", "run"], "run"),  # `--` is the *value* of --config
        (["--config", "x", "--", "run"], "run"),
        (["--config=x", "--", "run"], "run"),
        (["--", "run", "--", "x"], "run"),
        (["run", "--", "x"], "run"),
    ],
)
def test_first_command_double_dash_matrix(args: list[str], command: str | None) -> None:
    assert first_command(args, V) == command


def _d(args: list[str], elevated: bool) -> str:
    return decide_windows(
        args,
        program=AGENT_PROGRAM,
        environ={},
        installed=True,
        bundled=True,
        elevated=elevated,
    ).action


@pytest.mark.parametrize("elevated", [False, True])
@pytest.mark.parametrize(
    "args",
    [
        ["--", "run"],
        ["-v", "--", "run"],
        ["--config", "x", "--", "run"],
        ["--config", "--", "run"],
        ["--config=x", "--", "run"],
        ["--", "run", "--", "x"],
    ],
)
def test_run_is_refused_whatever_precedes_the_double_dash(
    args: list[str], elevated: bool
) -> None:
    assert _d(args, elevated) == "refuse"


def test_double_dash_other_commands_still_need_elevation() -> None:
    assert _d(["--", "identity", "show"], False) == "refuse"
    assert _d(["--", "identity", "show"], True) == "inplace"
    assert _d(["--", "--"], False) == "refuse"
    assert _d(["--"], False) == "refuse"


def test_double_dash_is_not_a_way_to_skip_the_elevation_rule_for_migrate_identity() -> (
    None
):
    # stricter than the bare command (which is `anyone`), never looser
    assert _d(["--", "migrate-identity"], False) == "refuse"
    assert _d(["migrate-identity"], False) == "none"


def test_controller_style_reading_after_double_dash_is_the_command() -> None:
    assert first_command(["--", "db"], frozenset({"--config"})) == "db"
