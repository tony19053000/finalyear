"""Unit tests for ``tests/_support/mf_conftest_gate.py`` (L3 C4.15-g: a conftest's
module-level ``importorskip`` aborted collection of unrelated paths)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mf_conftest_gate import missing_requirement, skip_items_under

pytestmark = pytest.mark.unit


def test_nothing_missing_is_none() -> None:
    assert missing_requirement([("json", "never"), ("pytest", "never")]) is None


def test_the_first_missing_module_gives_its_reason() -> None:
    reason = missing_requirement(
        [
            ("json", "never"),
            ("mf_no_such_module_xyz", "needs the controller extra"),
            ("mf_no_such_module_abc", "second"),
        ]
    )
    assert (
        reason
        == "needs the controller extra (`mf_no_such_module_xyz` is not installed)"
    )


def test_an_import_that_breaks_otherwise_still_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "mf_broken_gate_module.py").write_text("raise RuntimeError('boom')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(RuntimeError, match="boom"):
        missing_requirement([("mf_broken_gate_module", "x")])


def _item(path: Path) -> Any:
    item = SimpleNamespace(path=path, markers=[])
    item.add_marker = item.markers.append
    return item


def test_only_items_inside_the_directory_are_skipped(tmp_path: Path) -> None:
    gated = tmp_path / "component" / "agent"
    inside = _item(gated / "test_a.py")
    nested = _item(gated / "sub" / "test_b.py")
    sibling = _item(tmp_path / "component" / "agent_other" / "test_c.py")
    unit = _item(tmp_path / "unit" / "test_d.py")
    items = cast(list[pytest.Item], [inside, nested, sibling, unit])
    skip_items_under(gated, items, "needs the controller extra")
    for item in (inside, nested):
        assert [m.kwargs["reason"] for m in item.markers] == [
            "needs the controller extra"
        ]
        assert item.markers[0].name == "skip"
    assert sibling.markers == [] and unit.markers == []


def test_no_reason_marks_nothing(tmp_path: Path) -> None:
    item = _item(tmp_path / "test_a.py")
    skip_items_under(tmp_path, cast(list[pytest.Item], [item]), None)
    assert item.markers == []
