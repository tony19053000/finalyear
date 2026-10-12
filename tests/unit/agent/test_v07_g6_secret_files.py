"""V0.7 G6 deliverable 4 (D144): the agent checks, materialises and wipes secrets.

* :func:`check_secrets`: exactly the manifest's names, each once, canonical,
  value bounds; an insecure-dev agent refuses them (``secrets_need_mtls``)
  unless allowed; an ``as_env`` value must be text;
* :class:`SecretVault`: files ``0400`` (Docker) / ``0600`` (subprocess) in a
  ``0500`` folder of a ``0700`` per-user base; a symlinked base is refused;
  :meth:`wipe` zeroes the bytes (seen through a descriptor opened before) and
  removes everything; :meth:`sweep` removes a crashed agent's folders;
* the executor's plan: ``MESHFORGE_SECRETS_DIR`` and only the ``as_env``
  variables.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from meshforge.agent.execution.secret_files import (
    SecretFilesError,
    SecretVault,
    check_secrets,
    secret_env,
)
from meshforge.common.errors import FailureType
from meshforge.common.manifest import SecretSpec
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
A = SecretSpec(name="API_KEY")
D = SecretSpec(name="DB_PASS", as_env=True)


def _js(name: str, value: bytes) -> agent_pb2.JobSecret:
    return agent_pb2.JobSecret(name=name, value=value)


def test_exactly_the_manifest_names() -> None:
    got = check_secrets(
        [_js("API_KEY", b"value-1"), _js("DB_PASS", b"pass-2")], [A, D], allowed=True
    )
    assert got == {"API_KEY": b"value-1", "DB_PASS": b"pass-2"}
    assert check_secrets([], [], allowed=False) == {}


@pytest.mark.parametrize(
    "received",
    [
        [_js("API_KEY", b"value-1")],  # one missing
        [_js("API_KEY", b"value-1"), _js("DB_PASS", b"x-123"), _js("EXTRA", b"y-123")],
        [
            _js("API_KEY", b"value-1"),
            _js("API_KEY", b"value-2"),
            _js("DB_PASS", b"p-12"),
        ],
        [_js("api_key", b"value-1"), _js("DB_PASS", b"pass-2")],  # not canonical
        [_js("API_KEY", b"abc"), _js("DB_PASS", b"pass-2")],  # too short
        [_js("1BAD", b"value-1"), _js("DB_PASS", b"pass-2")],
    ],
)
def test_mismatches_are_refused(received: list[agent_pb2.JobSecret]) -> None:
    with pytest.raises(SecretFilesError) as exc:
        check_secrets(received, [A, D], allowed=True)
    assert exc.value.failure_type is FailureType.INVALID_INPUT
    assert "secrets_mismatch" in exc.value.message
    assert "value-1" not in exc.value.message


def test_insecure_dev_refuses_unless_allowed() -> None:
    with pytest.raises(SecretFilesError) as exc:
        check_secrets([_js("API_KEY", b"value-1")], [A], allowed=False)
    assert exc.value.failure_type is FailureType.CONFIG_ERROR
    assert exc.value.message.startswith("secrets_need_mtls")


@pytest.mark.parametrize("value", [b"\xff\xfe-binary", b"with\x00nul"])
def test_as_env_needs_text(value: bytes) -> None:
    with pytest.raises(SecretFilesError) as exc:
        check_secrets([_js("DB_PASS", value)], [D], allowed=True)
    assert "secret_not_text:DB_PASS" in exc.value.message
    # the same bytes are fine as a file
    assert check_secrets([_js("API_KEY", value)], [A], allowed=True)


def test_secret_env_only_as_env() -> None:
    assert secret_env({"API_KEY": b"k-123", "DB_PASS": b"p-123"}, [A, D]) == {
        "DB_PASS": "p-123"
    }


@POSIX
@pytest.mark.parametrize(("read_only", "mode"), [(True, 0o400), (False, 0o600)])
def test_materialise_modes(tmp_path: Path, read_only: bool, mode: int) -> None:
    vault = SecretVault(tmp_path)
    folder = vault.materialise(
        "att_x", {"API_KEY": b"value-1", "B": b"value-2"}, read_only=read_only
    )
    assert folder.parent == vault.base
    assert (vault.base.stat().st_mode & 0o777) == 0o700
    assert (folder.stat().st_mode & 0o777) == 0o500
    for name, value in (("API_KEY", b"value-1"), ("B", b"value-2")):
        path = folder / name
        assert (path.stat().st_mode & 0o777) == mode
        assert path.read_bytes() == value
    vault.wipe(folder)
    assert not folder.exists()


@POSIX
def test_wipe_zeroes_before_unlinking(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path)
    folder = vault.materialise("att_y", {"K": b"zero-me-please"}, read_only=False)
    fd = os.open(folder / "K", os.O_RDONLY)
    try:
        vault.wipe(folder)
        assert os.pread(fd, 64, 0) == b"\0" * len(b"zero-me-please")
    finally:
        os.close(fd)
    assert not folder.exists() and list(vault.base.iterdir()) == []


@POSIX
def test_a_planted_symlink_base_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "attacker"
    elsewhere.mkdir()
    root = tmp_path / "shm"
    root.mkdir()
    vault = SecretVault(root)
    vault.base.symlink_to(elsewhere)
    with pytest.raises(SecretFilesError):
        vault.materialise("att_z", {"K": b"value-1"}, read_only=False)
    assert list(elsewhere.iterdir()) == []


@POSIX
def test_sweep_removes_a_crashed_agents_folders(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path)
    vault.materialise("att_1", {"K": b"value-1"}, read_only=True)
    vault.materialise("att_2", {"K": b"value-2"}, read_only=False)
    assert vault.sweep() == 2
    assert list(vault.base.iterdir()) == []
    assert vault.sweep() == 0


@POSIX
def test_a_reused_attempt_folder_is_replaced(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path)
    vault.materialise("att_1", {"OLD": b"value-1"}, read_only=True)
    folder = vault.materialise("att_1", {"NEW": b"value-2"}, read_only=True)
    assert sorted(p.name for p in folder.iterdir()) == ["NEW"]
