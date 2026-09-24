"""Shared fixtures for the ``lab/inventory.py`` unit tests (roadmap 0.2.2).

``lab/`` is not a package of the distribution; like ``lab/inventory.py`` at runtime,
the tests put it on ``sys.path`` so the helper package imports as ``_inventory``
(``[tool.mypy] mypy_path`` lists ``lab`` for the same reason).

Recorded tool outputs live in ``fixtures/{linux,windows,common}/``: the Linux ones
were captured on Laptop 1 (Ubuntu 24.04, i7-12650H, RTX 3050 with the driver
blocked by Secure Boot); the Windows ones reproduce the exact JSON shape that
``ConvertTo-Json`` emits for the CIM/NetAdapter classes queried.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
LAB_DIR: Final = REPO_ROOT / "lab"
FIXTURES: Final = Path(__file__).resolve().parent / "fixtures"

if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))


@pytest.fixture
def fixture_text() -> Callable[[str], str]:
    """Read ``fixtures/<relative path>`` as UTF-8 text."""

    def read(rel: str) -> str:
        return (FIXTURES / rel).read_text(encoding="utf-8")

    return read


@pytest.fixture(autouse=True)
def _clear_probe_caches() -> Iterator[None]:
    """Probe results and runner state (deadline, hung tools) that are cached per
    process must not leak between tests."""
    from _inventory import probes, runner

    probes._cim_bundle.cache_clear()
    probes.nvidia_status.cache_clear()
    runner.reset()
    yield
    probes._cim_bundle.cache_clear()
    probes.nvidia_status.cache_clear()
    runner.reset()


@pytest.fixture
def stub_controller() -> Iterator[object]:
    """A real HTTP stub of the controller's REST surface on 127.0.0.1:0 (V0.3 lab kit)."""
    from _fabric.testing import StubController

    stub = StubController().start()
    yield stub
    stub.stop()
