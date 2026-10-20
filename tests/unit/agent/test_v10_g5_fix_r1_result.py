"""V0.10 G5 fix round 1 (N6): the combine's control result file is read for
a built-in only, never for a combine script, and never through a link."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from meshforge.agent.execution.combine_folder import CombineFolder
from meshforge.agent.execution.combine_steps import CombineState, _read_result
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = [pytest.mark.unit]


def _state(tmp_path: Path, *, script: tuple[str, ...] | None) -> CombineState:
    folder = CombineFolder(tmp_path, "att_n6", chunks=1, docker=False)
    folder.root.mkdir(parents=True)
    folder.result.write_text(
        json.dumps({"ok": False, "code": "header_mismatch", "message": "forged"}),
        encoding="utf-8",
    )
    return CombineState(spec=agent_pb2.CombineSpec(), folder=folder, script=script)


def test_builtin_result_file_is_read(tmp_path: Path) -> None:
    assert _read_result(_state(tmp_path, script=None)) == "header_mismatch: forged"


def test_script_combine_result_file_is_never_read(tmp_path: Path) -> None:
    """A script that writes the control result file cannot word its own
    combine_error: for a script only its exit code counts."""
    assert _read_result(_state(tmp_path, script=("python", "c.py"))) is None


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX")
def test_result_file_symlink_is_not_followed(tmp_path: Path) -> None:
    state = _state(tmp_path, script=None)
    assert state.folder is not None
    target = tmp_path / "elsewhere.json"
    target.write_text(state.folder.result.read_text(encoding="utf-8"))
    state.folder.result.unlink()
    state.folder.result.symlink_to(target)
    assert _read_result(state) is None
