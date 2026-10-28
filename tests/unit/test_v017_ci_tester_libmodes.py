"""Tester: drop_library_exec_bits edge cases (symlinks, subfolders, idempotence)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from pg_bundle import drop_library_exec_bits

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name == "nt", reason="POSIX modes"),
]


def test_only_top_level_regular_files_lose_exec_bits(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    (lib / "sub").mkdir(parents=True)
    so = lib / "libpq.so.5.16"
    so.write_bytes(b"x")
    so.chmod(0o755)
    (lib / "libpq.so.5").symlink_to(so.name)
    nested = lib / "sub" / "conv.so"
    nested.write_bytes(b"x")
    nested.chmod(0o755)
    ro = lib / "ro.so"
    ro.write_bytes(b"x")
    ro.chmod(0o555)
    ok = lib / "ok.so"
    ok.write_bytes(b"x")
    ok.chmod(0o644)
    assert drop_library_exec_bits(lib) == 2
    assert stat.S_IMODE(so.stat().st_mode) == 0o644
    assert stat.S_IMODE(ro.stat().st_mode) == 0o444
    assert stat.S_IMODE(ok.stat().st_mode) == 0o644
    assert stat.S_IMODE(nested.stat().st_mode) == 0o755  # documented: left alone
    assert (lib / "libpq.so.5").is_symlink()
    assert drop_library_exec_bits(lib) == 0  # idempotent
    assert drop_library_exec_bits(tmp_path / "missing") == 0
