"""V0.5 G5 D92: the agent re-checks where a named input lands (network input)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from meshforge.agent.execution.executor import _free_mount
from meshforge.agent.execution.fetch import FetchError
from meshforge.agent.execution.plan import PlanError, _inputs
from meshforge.common.errors import FailureType
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = [pytest.mark.unit]

TREE = "sha256:" + "a" * 64


def test_a_free_mount_creates_its_parents(tmp_path: Path) -> None:
    dest = _free_mount(tmp_path, "data/sets/ds")
    assert dest == tmp_path / "data/sets/ds"
    assert dest.parent.is_dir() and not dest.exists()


@pytest.mark.skipif(
    os.name != "posix", reason="symlink creation needs privileges on Windows"
)
def test_a_mount_through_a_project_link_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "data").symlink_to(outside)
    with pytest.raises(FetchError) as info:
        _free_mount(tmp_path, "data/ds")
    assert info.value.failure_type is FailureType.INVALID_INPUT
    assert list(outside.iterdir()) == []  # nothing created through the link


def test_a_mount_over_project_files_is_refused(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data/ds").write_text("project file")
    with pytest.raises(FetchError, match="overlaps"):
        _free_mount(tmp_path, "data/ds")
    (tmp_path / "file").write_text("x")
    with pytest.raises(FetchError, match="through a project file"):
        _free_mount(tmp_path, "file/ds")


def _spec(*inputs: agent_pb2.InputTree, tree: str = TREE) -> agent_pb2.ExecutionSpec:
    return agent_pb2.ExecutionSpec(tree_id=tree, inputs=list(inputs))


def test_spec_inputs_are_validated_and_bounded() -> None:
    from meshforge.common.hashing import Digest

    tree = Digest.parse(TREE)
    ok = _inputs(_spec(agent_pb2.InputTree(tree_id=TREE, mount="d", ref="x@1")), tree)
    assert ok[0].mount == "d"
    many = [
        agent_pb2.InputTree(tree_id=TREE, mount=f"m{i}", ref="x@1") for i in range(17)
    ]
    assert len(_inputs(_spec(*many[:16]), tree)) == 16
    with pytest.raises(PlanError):
        _inputs(_spec(*many), tree)
    for bad in (
        agent_pb2.InputTree(tree_id="sha256:zz", mount="d", ref="x@1"),
        agent_pb2.InputTree(tree_id=TREE, mount="../d", ref="x@1"),
        agent_pb2.InputTree(tree_id=TREE, mount=".meshforge/d", ref="x@1"),
        agent_pb2.InputTree(tree_id=TREE, mount="d", ref="x\x1b@1"),
    ):
        with pytest.raises(PlanError):
            _inputs(_spec(bad), tree)
    with pytest.raises(PlanError, match="overlapping"):
        _inputs(
            _spec(
                agent_pb2.InputTree(tree_id=TREE, mount="d", ref="x@1"),
                agent_pb2.InputTree(tree_id=TREE, mount="d/e", ref="y@1"),
            ),
            tree,
        )
    with pytest.raises(PlanError, match="tree job"):
        _inputs(_spec(agent_pb2.InputTree(tree_id=TREE, mount="d", ref="x@1")), None)
