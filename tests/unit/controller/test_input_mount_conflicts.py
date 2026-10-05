"""V0.5 G5 D92: an input mount never overlaps a project path - fast on big trees."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.controller.api.job_routes import _mount_conflict

pytestmark = [pytest.mark.unit]


def _tree(*paths: str) -> Any:
    return SimpleNamespace(entries=[SimpleNamespace(path=p) for p in paths])


@pytest.mark.parametrize(
    ("mount", "conflict"),
    [
        ("data", "data/own.txt"),  # a project file under the mount
        ("data/own.txt", "data/own.txt"),  # the mount is a project file
        ("data/own.txt/x", "data/own.txt"),  # through a project file
        ("link/x", "link"),  # through a project symlink / empty dir entry
        ("data/in", None),  # a free folder next to project files
        ("dat", None),  # a name prefix is not a path prefix
        ("data2", None),
    ],
)
def test_conflicts(mount: str, conflict: str | None) -> None:
    project = _tree("data/own.txt", "link", "main.py", "data2x/a")
    found = _mount_conflict(project, [mount])
    assert (found[1] if found else None) == conflict


def test_a_100k_entry_tree_is_checked_quickly() -> None:
    project = _tree(*(f"d{i // 1000}/f{i}.bin" for i in range(100_000)))
    started = time.monotonic()
    assert _mount_conflict(project, [f"in{i}" for i in range(16)]) is None
    assert _mount_conflict(project, ["d99"]) == ("d99", "d99/f99000.bin")
    assert time.monotonic() - started < 2.0
