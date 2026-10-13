"""V0.7 G6 tester r1: the agent's secret vault and checks (criterion 4).

Hostile folders (planted symlinks, links to files outside), partial failure,
hard links, binary values with trailing newlines, sweep isolation between two
agents of one user, and the check_secrets matrix incl. the insecure channel.
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
    check_secrets,
    default_root,
    secret_env,
)
from meshforge.common.errors import FailureType
from meshforge.common.manifest import SecretSpec
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes"),
]


def _js(name: str, value: bytes) -> agent_pb2.JobSecret:
    return agent_pb2.JobSecret(name=name, value=value)


def test_bytes_round_trip_exactly_including_trailing_newline_and_nul(
    tmp_path: Path,
) -> None:
    vault = SecretVault(tmp_path / "root", tag="t")
    value = b"line1\nline2\r\n\x00\xff\n"
    folder = vault.materialise("att_1", {"K1": value}, read_only=False)
    assert (folder / "K1").read_bytes() == value
    assert stat.S_IMODE((folder / "K1").stat().st_mode) == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == 0o500
    assert stat.S_IMODE(vault.base.stat().st_mode) == 0o700


def test_a_64kib_value_is_written_whole_and_wiped_whole(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "root")
    value = os.urandom(64 * 1024)
    folder = vault.materialise("att_1", {"BIG": value}, read_only=True)
    assert (folder / "BIG").read_bytes() == value
    assert stat.S_IMODE((folder / "BIG").stat().st_mode) == 0o400
    held = os.open(folder / "BIG", os.O_RDONLY)
    try:
        vault.wipe(folder)
        assert not folder.exists()
        os.lseek(held, 0, os.SEEK_SET)
        assert os.read(held, 70_000) == b"\0" * len(value)
    finally:
        os.close(held)


def test_a_hard_link_made_by_the_job_is_zeroed_too(tmp_path: Path) -> None:
    """A same-user job can `ln` the secret file into its workdir (hard links to
    one's own files are allowed); wiping must zero the shared inode."""
    vault = SecretVault(tmp_path / "root")
    folder = vault.materialise("att_1", {"K1": b"hard-link-secret"}, read_only=False)
    link = tmp_path / "stolen"
    os.link(folder / "K1", link)
    vault.wipe(folder)
    assert link.read_bytes() == b"\0" * len(b"hard-link-secret")


def test_wipe_never_follows_a_symlink_planted_in_the_folder(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "root")
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    folder = vault.materialise("att_1", {"K1": b"value-123"}, read_only=False)
    os.chmod(folder, 0o700)
    (folder / "evil").symlink_to(victim)
    (folder / "evil_dir").symlink_to(tmp_path)
    vault.wipe(folder)
    assert victim.read_text() == "precious"
    assert not folder.exists()
    assert tmp_path.exists()


def test_wipe_of_a_symlinked_attempt_folder_removes_only_the_link(
    tmp_path: Path,
) -> None:
    vault = SecretVault(tmp_path / "root")
    target = tmp_path / "other"
    target.mkdir()
    (target / "keep").write_text("keep")
    vault.materialise("att_0", {"K1": b"value-123"}, read_only=False)
    link = vault.base / "att_1"
    link.symlink_to(target)
    vault.wipe(link)
    assert (target / "keep").read_text() == "keep"
    assert not link.is_symlink()


def test_materialise_over_a_planted_symlink_attempt_folder_does_not_write_through(
    tmp_path: Path,
) -> None:
    vault = SecretVault(tmp_path / "root")
    vault.materialise("att_0", {"K1": b"value-123"}, read_only=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault.base / "att_1").symlink_to(outside)
    folder = vault.materialise("att_1", {"K1": b"value-456"}, read_only=False)
    assert not folder.is_symlink()
    assert list(outside.iterdir()) == []


def test_a_failed_materialise_leaves_nothing_behind(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "root")
    with pytest.raises((OSError, SecretFilesError)):
        # the second name is a path that cannot be created: a partial folder
        # (and the first, already written secret) must be wiped.
        vault.materialise(
            "att_1", {"AAA": b"value-123", "sub/BBB": b"value-456"}, read_only=False
        )
    assert list(vault.base.iterdir()) == []


def test_sweep_leaves_another_agents_base_alone(tmp_path: Path) -> None:
    root = tmp_path / "root"
    one = SecretVault(root, tag=agent_tag(tmp_path / "a"))
    two = SecretVault(root, tag=agent_tag(tmp_path / "b"))
    one.materialise("att_1", {"K1": b"value-123"}, read_only=False)
    keep = two.materialise("att_2", {"K1": b"value-456"}, read_only=False)
    assert one.sweep() == 1
    assert keep.exists() and list(one.base.iterdir()) == []


def test_two_attempts_have_separate_folders_and_the_other_wipe_is_independent(
    tmp_path: Path,
) -> None:
    vault = SecretVault(tmp_path / "root")
    a = vault.materialise("att_a", {"K1": b"aaaa-value"}, read_only=False)
    b = vault.materialise("att_b", {"K1": b"bbbb-value"}, read_only=False)
    vault.wipe(a)
    assert (b / "K1").read_bytes() == b"bbbb-value"


def test_base_owned_by_someone_else_or_a_file_is_refused(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "root")
    vault.root.mkdir(parents=True)
    vault.base.write_text("a file, not a folder")
    with pytest.raises(SecretFilesError):
        vault.materialise("att_1", {"K1": b"value-123"}, read_only=False)


def test_a_group_writable_base_is_refused_not_trusted(tmp_path: Path) -> None:
    """Fix round 3 (D164, T-21) reverses this r1 test: it asserted that a 0777
    base is tightened to 0700 and then used; the ruling is that a base with a
    mode other than 0700 is REFUSED (others may have planted entries in it),
    left exactly as it is, and nothing is written."""
    vault = SecretVault(tmp_path / "root")
    vault.root.mkdir(parents=True)
    vault.base.mkdir(mode=0o777)
    os.chmod(vault.base, 0o777)  # noqa: S103
    with pytest.raises(SecretFilesError) as exc:
        vault.materialise("att_1", {"K1": b"value-123"}, read_only=False)
    assert exc.value.failure_type is FailureType.CONFIG_ERROR
    assert stat.S_IMODE(vault.base.stat().st_mode) == 0o777
    assert list(vault.base.iterdir()) == []


def test_default_root_is_dev_shm_on_linux() -> None:
    assert default_root(Path("/nonexistent")) == Path("/dev/shm")


# ------------------------------------------------------------ check_secrets


def test_insecure_agent_refuses_even_when_only_the_manifest_asks() -> None:
    with pytest.raises(SecretFilesError) as exc:
        check_secrets([], [SecretSpec(name="K1")], allowed=False)
    assert exc.value.message.startswith("secrets_need_mtls")


def test_a_controller_that_sends_secrets_nobody_asked_for_is_refused() -> None:
    with pytest.raises(SecretFilesError) as exc:
        check_secrets([_js("K1", b"value-123")], [], allowed=True)
    assert exc.value.failure_type is FailureType.INVALID_INPUT


def test_no_secrets_at_all_never_needs_the_flag() -> None:
    assert check_secrets([], [], allowed=False) == {}
    assert check_secrets([], [], allowed=True) == {}


@pytest.mark.parametrize("name", ["K\x001", "../K1", "K1 ", " K1", "k1", "K-1", ""])
def test_hostile_names_from_the_wire_never_reach_the_filesystem(name: str) -> None:
    with pytest.raises(SecretFilesError) as exc:
        check_secrets([_js(name, b"value-123")], [SecretSpec(name="K1")], allowed=True)
    assert "value-123" not in exc.value.message


def test_as_env_text_with_newline_is_allowed_but_nul_and_bad_utf8_are_not() -> None:
    spec = [SecretSpec(name="K1", as_env=True)]
    got = check_secrets([_js("K1", b"line1\nline2")], spec, allowed=True)
    assert secret_env(got, spec) == {"K1": "line1\nline2"}
    for bad in (b"ab\x00cd", b"\xff\xfe\xfd\xfc", "é".encode("latin-1") * 4):
        with pytest.raises(SecretFilesError):
            check_secrets([_js("K1", bad)], spec, allowed=True)


def test_secret_env_never_exports_a_file_only_secret() -> None:
    specs = [SecretSpec(name="A1"), SecretSpec(name="B1", as_env=True)]
    values = {"A1": b"file-only-val", "B1": b"env-value-1"}
    assert secret_env(values, specs) == {"B1": "env-value-1"}
