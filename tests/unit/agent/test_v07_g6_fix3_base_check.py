"""V0.7 G6 fix round 3, T-21 (D164): ONE check of the secrets base.

``/dev/shm`` is world-writable and the base name is predictable, so another
local user can plant it. ``SecretVault._open_base`` opens the base with
``O_NOFOLLOW | O_DIRECTORY``, checks the opened descriptor (directory, our
uid, mode 0700) and materialise / wipe / sweep work relative to it:

* a symlink, a plain file, a FIFO, a foreign owner, a wrong mode: refused
  typed by materialise; sweep and wipe touch nothing; one warning each that
  names the base folder only (never a symlink's target);
* the base swapped for a symlink AFTER the check: the walk stays in the real
  directory;
* inside a good base: a symlinked attempt folder, a FIFO and a plain file are
  unlinked, their targets untouched;
* the path-based fallback (no directory descriptors: Windows) refuses a
  symlinked base as well.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from meshforge.agent.execution import secret_files
from meshforge.agent.execution.secret_files import (
    SecretFilesError,
    SecretsBaseRefusedError,
    SecretVault,
    default_root,
)
from meshforge.common.errors import FailureType

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX"),
]

VALUE = b"t21-secret-value"


@pytest.fixture
def victim(tmp_path: Path) -> Path:
    """A directory that looks like a base full of attempt folders."""
    folder = tmp_path / "victim"
    (folder / "att_precious" / "deep").mkdir(parents=True)
    (folder / "att_precious" / "f").write_bytes(b"keep")
    (folder / "att_precious" / "deep" / "g").write_bytes(b"keep-too")
    (folder / "plain").write_bytes(b"keep-three")
    return folder


def _intact(victim: Path) -> None:
    assert (victim / "att_precious" / "f").read_bytes() == b"keep"
    assert (victim / "att_precious" / "deep" / "g").read_bytes() == b"keep-too"
    assert (victim / "plain").read_bytes() == b"keep-three"


@pytest.fixture
def vault(tmp_path: Path) -> SecretVault:
    (tmp_path / "root").mkdir()
    return SecretVault(tmp_path / "root", tag="f3f3f3f3f3f3")


def _refused(logs: list[Any], event: str) -> list[Any]:
    return [entry for entry in logs if entry["event"] == event]


def _plant_symlink(vault: SecretVault, victim: Path) -> None:
    os.symlink(victim, vault.base)


def _plant_file(vault: SecretVault, victim: Path) -> None:
    vault.base.write_bytes(b"not a directory")


def _plant_fifo(vault: SecretVault, victim: Path) -> None:
    os.mkfifo(vault.base)


def _plant_wide(vault: SecretVault, victim: Path) -> None:
    vault.base.mkdir()
    (vault.base / "att_old").mkdir()
    (vault.base / "att_old" / "K").write_bytes(b"planted")
    os.chmod(vault.base, 0o750)  # noqa: S103 - the refused case


def _plant_closed(vault: SecretVault, victim: Path) -> None:
    vault.base.mkdir()
    os.chmod(vault.base, 0o500)


def _plant_unreadable(vault: SecretVault, victim: Path) -> None:
    vault.base.mkdir()
    os.chmod(vault.base, 0o300)


PLANTS: dict[str, tuple[Callable[[SecretVault, Path], None], str]] = {
    "symlink": (_plant_symlink, "symlink"),
    "plain_file": (_plant_file, "not_a_directory"),
    "fifo": (_plant_fifo, "not_a_directory"),
    "mode_0750": (_plant_wide, "wrong_mode"),
    "mode_0500": (_plant_closed, "wrong_mode"),
    "mode_0300": (_plant_unreadable, "unreadable"),
}


PLANTS_LISTABLE = ("mode_0750", "mode_0500")


@pytest.fixture
def unlocked(vault: SecretVault) -> Iterator[None]:
    yield
    if vault.base.is_dir() and not vault.base.is_symlink():
        os.chmod(vault.base, 0o700)


@pytest.mark.security
@pytest.mark.usefixtures("unlocked")
@pytest.mark.parametrize("plant", sorted(PLANTS))
def test_a_bad_base_is_refused_by_materialise_sweep_and_wipe(
    vault: SecretVault, victim: Path, plant: str
) -> None:
    make, reason = PLANTS[plant]
    make(vault, victim)
    before = os.lstat(vault.base)
    inside = sorted(os.listdir(vault.base)) if plant in PLANTS_LISTABLE else None
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(SecretsBaseRefusedError) as exc:
            vault.materialise("att_1", {"A": VALUE}, read_only=False)
        assert vault.sweep() == 0
        vault.wipe(vault.folder("att_precious"))
        vault.wipe(vault.folder("att_old"))
    # typed, and it says which rule
    assert isinstance(exc.value, SecretFilesError)
    assert exc.value.failure_type is FailureType.CONFIG_ERROR
    assert exc.value.reason == reason
    assert exc.value.message.startswith(f"secrets_base_refused:{reason}:")
    # nothing changed: not the target, not the planted thing itself
    _intact(victim)
    after = os.lstat(vault.base)
    assert (after.st_mode, after.st_ino) == (before.st_mode, before.st_ino)
    if inside is not None:
        assert sorted(os.listdir(vault.base)) == inside
        if plant == "mode_0750":
            assert (vault.base / "att_old" / "K").read_bytes() == b"planted"
    # one warning per call, naming the base folder and never the target
    assert len(_refused(logs, "agent.execution.secrets_base_refused")) == 2
    assert len(_refused(logs, "agent.execution.secret_wipe_failed")) == 2
    for entry in logs:
        assert entry["log_level"] == "warning"
        assert entry["reason"] == reason
        assert entry["base"] == vault.base.name
        assert str(victim) not in repr(entry) and "victim" not in repr(entry)
    assert str(victim) not in exc.value.message


@pytest.mark.security
@pytest.mark.parametrize("call", ["materialise", "sweep", "wipe"])
def test_a_base_owned_by_another_user_is_refused(
    vault: SecretVault, monkeypatch: pytest.MonkeyPatch, call: str
) -> None:
    """We cannot chown here: the opened base's fstat reports another owner."""
    folder = vault.materialise("att_1", {"A": VALUE}, read_only=False)
    real = os.fstat
    base = os.stat(vault.base)

    def foreign(fd: int) -> os.stat_result:
        info = real(fd)
        if (info.st_ino, info.st_dev) != (base.st_ino, base.st_dev):
            return info
        fields = list(info)
        fields[stat.ST_UID] = os.getuid() + 1
        return os.stat_result(fields)

    monkeypatch.setattr(secret_files.os, "fstat", foreign)
    with structlog.testing.capture_logs() as logs:
        if call == "materialise":
            with pytest.raises(SecretsBaseRefusedError) as exc:
                vault.materialise("att_2", {"B": VALUE}, read_only=False)
            assert exc.value.reason == "foreign_owner"
        elif call == "sweep":
            assert vault.sweep() == 0
        else:
            vault.wipe(folder)
    monkeypatch.undo()
    assert [entry["reason"] for entry in logs] == ["foreign_owner"]
    assert (folder / "A").read_bytes() == VALUE  # untouched
    assert sorted(os.listdir(vault.base)) == ["att_1"]
    assert vault.sweep() == 1


@pytest.mark.security
@pytest.mark.parametrize("call", ["sweep", "wipe", "materialise"])
def test_a_base_swapped_for_a_symlink_after_the_check_redirects_nothing(
    vault: SecretVault,
    victim: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    call: str,
) -> None:
    """TOCTOU: the name is replaced between the check and the walk; the walk
    stays in the directory that was checked (now ``moved``)."""
    vault.materialise("att_precious", {"f": VALUE}, read_only=False)
    moved = tmp_path / "moved"
    real = SecretVault._open_base

    def check_then_swap(self: SecretVault) -> Any:
        opened = real(self)
        os.rename(self.base, moved)
        os.symlink(victim, self.base)
        return opened

    monkeypatch.setattr(SecretVault, "_open_base", check_then_swap)
    if call == "sweep":
        assert vault.sweep() == 1
        assert os.listdir(moved) == []
    elif call == "wipe":
        vault.wipe(vault.folder("att_precious"))
        assert os.listdir(moved) == []
    else:
        vault.materialise("plain", {"NEW": VALUE}, read_only=False)
        assert (moved / "plain" / "NEW").read_bytes() == VALUE
        assert sorted(os.listdir(moved)) == ["att_precious", "plain"]
    _intact(victim)
    assert sorted(os.listdir(victim)) == ["att_precious", "plain"]
    # leave nothing read-only behind for pytest's own temp cleanup
    monkeypatch.undo()
    os.unlink(vault.base)
    os.rename(moved, vault.base)
    vault.sweep()


@pytest.mark.security
def test_odd_entries_inside_a_good_base_are_unlinked_not_followed(
    vault: SecretVault, victim: Path
) -> None:
    vault.materialise("att_1", {"A": VALUE}, read_only=True)
    os.symlink(victim / "att_precious", vault.base / "att_link")  # attempt folder
    os.symlink(victim / "plain", vault.base / "file_link")
    os.mkfifo(vault.base / "fifo")
    (vault.base / "plain-file").write_bytes(b"stray")
    assert vault.sweep() == 5
    assert os.listdir(vault.base) == []
    _intact(victim)


def test_a_symlinked_attempt_folder_is_replaced_not_written_through(
    vault: SecretVault, victim: Path
) -> None:
    """The attempt's own name planted as a symlink (only this user could)."""
    vault.materialise("att_0", {"A": VALUE}, read_only=False)
    os.symlink(victim / "att_precious", vault.base / "att_1")
    folder = vault.materialise("att_1", {"A": VALUE}, read_only=False)
    assert not folder.is_symlink()
    assert (folder / "A").read_bytes() == VALUE
    assert sorted(os.listdir(victim / "att_precious")) == ["deep", "f"]
    # wiping a folder that was swapped for a symlink unlinks the link only
    vault.wipe(vault.folder("att_0"))
    os.symlink(victim / "att_precious", vault.base / "att_0")
    vault.wipe(vault.folder("att_0"))
    assert not (vault.base / "att_0").is_symlink()
    _intact(victim)


def test_a_fifo_or_symlink_inside_an_attempt_folder_cannot_hold_the_wipe(
    vault: SecretVault, victim: Path
) -> None:
    folder = vault.materialise("att_1", {"A": VALUE}, read_only=False)
    os.chmod(folder, 0o700)
    os.mkfifo(folder / "pipe")  # opening it for writing would block for ever
    os.symlink(victim / "plain", folder / "link")
    os.symlink(victim, folder / "dirlink")
    vault.wipe(folder)
    assert not folder.exists()
    _intact(victim)


def test_wipe_refuses_a_path_outside_the_base(vault: SecretVault, victim: Path) -> None:
    vault.materialise("att_1", {"A": VALUE}, read_only=False)
    with structlog.testing.capture_logs() as logs:
        vault.wipe(victim / "att_precious")
    assert [entry["reason"] for entry in logs] == ["outside_the_base"]
    assert str(victim) not in repr(logs)
    _intact(victim)


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../x", "a\0b"])
def test_an_attempt_id_or_secret_name_that_is_not_one_component_is_refused(
    vault: SecretVault, name: str
) -> None:
    with pytest.raises(SecretFilesError):
        vault.materialise(name, {"A": VALUE}, read_only=False)
    with pytest.raises(SecretFilesError):
        vault.materialise("att_1", {name: VALUE}, read_only=False)
    assert not vault.base.exists() or os.listdir(vault.base) == []


def test_a_new_base_is_0700_under_any_umask(vault: SecretVault) -> None:
    old = os.umask(0o277)
    try:
        folder = vault.materialise("att_1", {"A": VALUE}, read_only=False)
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(vault.base).st_mode) == 0o700
    assert (folder / "A").read_bytes() == VALUE
    assert vault.sweep() == 1


@pytest.mark.security
def test_the_data_dir_fallback_root_goes_through_the_same_check(
    tmp_path: Path, victim: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off Linux (or without a usable /dev/shm) the root is <data>/secrets."""
    monkeypatch.setattr(secret_files.sys, "platform", "darwin")
    data = tmp_path / "data"
    root = default_root(data)
    assert root == data / "secrets"
    vault = SecretVault(root, tag="abcabcabcabc")
    folder = vault.materialise("att_1", {"A": VALUE}, read_only=False)
    assert folder.parent.parent == root
    assert vault.sweep() == 1
    vault.base.rmdir()
    os.symlink(victim, vault.base)
    with pytest.raises(SecretsBaseRefusedError):
        vault.materialise("att_1", {"A": VALUE}, read_only=False)
    assert vault.sweep() == 0
    vault.wipe(vault.folder("att_precious"))
    _intact(victim)


@pytest.mark.security
def test_without_directory_descriptors_a_symlinked_base_is_still_refused(
    vault: SecretVault, victim: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path-based fallback (Windows): same refusals, same results."""
    monkeypatch.setattr(secret_files, "_FD_WALK", False)
    folder = vault.materialise("att_1", {"A": VALUE}, read_only=True)
    assert (folder / "A").read_bytes() == VALUE
    assert stat.S_IMODE(os.stat(folder / "A").st_mode) == 0o400
    os.symlink(victim / "plain", vault.base / "link")
    assert vault.sweep() == 2
    assert os.listdir(vault.base) == []
    vault.base.rmdir()
    os.symlink(victim, vault.base)
    with pytest.raises(SecretsBaseRefusedError) as exc:
        vault.materialise("att_1", {"A": VALUE}, read_only=False)
    assert exc.value.reason == "symlink"
    assert vault.sweep() == 0
    vault.wipe(vault.folder("att_precious"))
    _intact(victim)
    vault.base.unlink()
    vault.base.write_bytes(b"x")
    with pytest.raises(SecretsBaseRefusedError) as exc:
        vault.materialise("att_1", {"A": VALUE}, read_only=False)
    assert exc.value.reason == "not_a_directory"
