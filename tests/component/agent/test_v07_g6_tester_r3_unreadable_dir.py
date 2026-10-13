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
