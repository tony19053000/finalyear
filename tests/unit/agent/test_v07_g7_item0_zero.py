"""V0.7 G7 item 0 (D165 (3)): ``SecretVault._zero`` opens its write-only
descriptor as ``wb`` and still overwrites the whole file in place.

``wb`` on a descriptor does not truncate (only a path open would), so the
zeroing semantics are unchanged: same size, every byte zero, through a hard
link too (it shares the inode). A truncating ``_zero`` would leave the old
blocks on the device and fails the size check here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.execution import secret_files
from meshforge.agent.execution.secret_files import SecretVault

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptors")


@POSIX
def test_zero_overwrites_every_chunk_in_place_and_keeps_the_size(
    tmp_path: Path,
) -> None:
    value = os.urandom(3 * 64 * 1024 + 17)  # several _WIPE_CHUNKs + a tail
    vault = SecretVault(tmp_path)
    folder = vault.materialise("att_zero", {"BIG": value}, read_only=False)
    link = tmp_path / "hard-link-the-job-made"
    os.link(folder / "BIG", link)
    vault.wipe(folder)
    assert not folder.exists()
    assert link.stat().st_size == len(value)
    assert link.read_bytes() == b"\0" * len(value)


@POSIX
def test_zero_opens_the_descriptor_write_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modes: list[str] = []
    real = os.fdopen

    def recording(fd: int, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        modes.append(mode)
        return real(fd, mode, *args, **kwargs)

    vault = SecretVault(tmp_path)
    folder = vault.materialise("att_mode", {"K": b"zero-me"}, read_only=False)
    monkeypatch.setattr(secret_files.os, "fdopen", recording)  # the wipe only
    vault.wipe(folder)
    assert modes == ["wb"]
