"""V0.7 G6 tester r3 (T-20): `remove_tree` (used by every T-14 cleanup and by the
workdir cleanup) raises TypeError on a tree that holds a directory the owner
cannot read (mode 000/300): the `rmtree` error hook retries `os.open(path)`
without flags. A job can create such a directory itself.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from meshforge.agent.envs.fsutil import remove_tree

pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes"),
]


@pytest.mark.xfail(
    strict=True,
    reason="T-20 (minor, pre-G6): remove_tree raises TypeError (not OSError) on a tree "
    "with an unreadable directory; _remove_quietly / evict handlers catch OSError "
    "only, and a shielded cleanup's TypeError replaces the CancelledError",
)
@pytest.mark.parametrize("mode", [0o000, 0o300, 0o100])
def test_remove_tree_removes_a_tree_with_an_unreadable_directory(
    tmp_path: Path, mode: int
) -> None:
    tree = tmp_path / "tree"
    (tree / "sub" / "deep").mkdir(parents=True)
    (tree / "sub" / "f").write_text("x")
    os.chmod(tree / "sub", mode)
    try:
        remove_tree(tree)
        assert not tree.exists()
    finally:
        if (tree / "sub").exists():
            os.chmod(tree / "sub", stat.S_IRWXU)
