# ruff: noqa: T201
"""V0.7 G6 tester r3: the start sweep removes leftovers under its OWN base only -
never another agent's base, never a symlink's target outside the base.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from meshforge.agent.execution.secret_files import (
    SecretFilesError,
    SecretVault,
    agent_tag,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX"),
]


def test_sweep_removes_everything_under_its_base_including_odd_entries(
    tmp_path: Path,
) -> None:
    vault = SecretVault(tmp_path / "root", tag="aaaaaaaaaaaa")
    mine = vault.materialise("att_1", {"A": b"alpha-secret"}, read_only=True)
    vault.materialise(
        "att_2", {"B": b"beta-secret", "C": b"gamma-secret"}, read_only=False
    )
    (vault.base / "stray-dir" / "deep").mkdir(parents=True)
    (vault.base / "stray-dir" / "deep" / "f").write_bytes(b"y")
    os.chmod(vault.base / "stray-dir", 0o500)  # not writable
    assert mine.exists()
    assert vault.sweep() == 3
    assert list(vault.base.iterdir()) == []


def test_sweep_never_touches_another_agents_base(tmp_path: Path) -> None:
    root = tmp_path / "root"
    mine = SecretVault(root, tag=agent_tag(tmp_path / "data-a"))
    other = SecretVault(root, tag=agent_tag(tmp_path / "data-b"))
    assert mine.base != other.base
    theirs = other.materialise(
        "att_theirs", {"K": b"other-agents-secret"}, read_only=False
    )
    mine.materialise("att_mine", {"K": b"my-secret-value"}, read_only=False)
    assert mine.sweep() == 1
    assert (theirs / "K").read_bytes() == b"other-agents-secret"
    # a prefix-sharing tag is another base as well
    third = SecretVault(root, tag=mine.tag + "x")
    t3 = third.materialise("att_x", {"K": b"third-secret"}, read_only=False)
    assert mine.sweep() == 0
    assert (t3 / "K").read_bytes() == b"third-secret"


def test_a_symlink_in_the_base_is_unlinked_and_its_target_left_alone(
    tmp_path: Path,
) -> None:
    vault = SecretVault(tmp_path / "root", tag="bbbbbbbbbbbb")
    vault.materialise("att_1", {"A": b"alpha-secret"}, read_only=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "precious.txt"
    precious.write_bytes(b"do-not-zero-me")
    os.symlink(outside, vault.base / "att_link_dir")
    os.symlink(precious, vault.base / "att_link_file")
    assert vault.sweep() == 3
    assert precious.read_bytes() == b"do-not-zero-me"
    assert outside.is_dir()
    assert list(vault.base.iterdir()) == []


def test_a_symlink_inside_an_attempt_folder_is_not_followed(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "root", tag="cccccccccccc")
    folder = vault.materialise("att_1", {"A": b"alpha-secret"}, read_only=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "precious.txt"
    precious.write_bytes(b"do-not-zero-me")
    os.chmod(folder, 0o700)
    os.symlink(precious, folder / "evil-file")
    os.symlink(outside, folder / "evil-dir")
    vault.sweep()
    assert precious.read_bytes() == b"do-not-zero-me"
    assert not folder.exists()


def test_a_base_that_is_a_symlink_is_refused_and_never_swept_into(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    victim = tmp_path / "victim"
    (victim / "att_precious").mkdir(parents=True)
    (victim / "att_precious" / "f").write_bytes(b"keep")
    vault = SecretVault(root, tag="dddddddddddd")
    os.symlink(victim, vault.base)
    with pytest.raises(SecretFilesError):
        vault.materialise("att_1", {"A": b"alpha-secret"}, read_only=False)
    assert (victim / "att_precious" / "f").read_bytes() == b"keep"
    # the start sweep goes through the same symlinked base: report what it does
    removed = vault.sweep()
    print("sweep through a symlinked base removed", removed)
    assert (victim / "att_precious" / "f").exists(), (
        "the sweep followed a symlinked base and wiped files outside it"
    )


def test_wipe_is_idempotent_and_survives_a_vanished_folder(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "root", tag="eeeeeeeeeeee")
    folder = vault.materialise("att_1", {"A": b"alpha-secret"}, read_only=True)
    vault.wipe(folder)
    vault.wipe(folder)
    vault.wipe(vault.folder("never-existed"))
    assert not folder.exists()
    assert stat.S_IMODE(os.stat(vault.base).st_mode) == 0o700
