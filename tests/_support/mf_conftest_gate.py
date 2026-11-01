"""Gate a test directory on optional packages without breaking collection.

A module-level ``pytest.importorskip`` in a ``conftest.py`` raises ``Skipped`` while
pytest is *loading the conftest*, which aborts the whole run - including unrelated
paths given on the same command line (L3, Windows: ``pytest tests/unit/common/x.py
tests/component/agent/y.py`` with only ``--extra agent`` ran nothing). Instead a
conftest calls :func:`missing_requirement` once, imports its heavy modules only when
nothing is missing, and implements ``pytest_collection_modifyitems`` with
:func:`skip_items_under`: every test of that directory is then reported as skipped
with the same reason the ``importorskip`` used to give, and other paths run.

Test modules of the gated directory that import a missing extra themselves are
already turned into a skip by the root conftest's ``_ExtrasAwareModule``.
"""

from __future__ import annotations

import importlib
from collections.abc import Sequence
from pathlib import Path

import pytest


def missing_requirement(requirements: Sequence[tuple[str, str]]) -> str | None:
    """The skip reason for the first ``(module, reason)`` that cannot be imported.

    Same test as ``pytest.importorskip``: the module is imported and only a
    ``ModuleNotFoundError`` counts as missing (any other import error propagates).
    """
    for module, reason in requirements:
        try:
            importlib.import_module(module)
        except ModuleNotFoundError:
            return f"{reason} (`{module}` is not installed)"
    return None


def skip_items_under(
    directory: Path, items: Sequence[pytest.Item], reason: str | None
) -> None:
    """Mark every collected item inside ``directory`` skipped with ``reason``.

    ``pytest_collection_modifyitems`` of a nested conftest sees the whole session's
    items, so the directory filter is what keeps other paths untouched.
    """
    if reason is None:
        return
    marker = pytest.mark.skip(reason=reason)
    for item in items:
        if item.path.is_relative_to(directory):
            item.add_marker(marker)
