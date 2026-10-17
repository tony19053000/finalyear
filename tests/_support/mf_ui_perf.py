"""Load the ``scripts/ui_perf/`` package (V0.8 G7 perf script, split in V0.9 G0)
for a test, by its path: ``scripts/`` is not an installed package.

Each caller names its own copy (``sys.modules[name]``), as the tests did when
the script was one file, so module-level state never leaks between them.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

__all__ = ["UI_PERF_DIR", "load_ui_perf"]

UI_PERF_DIR = Path(__file__).resolve().parents[2] / "scripts" / "ui_perf"


def load_ui_perf(name: str = "ui_perf") -> ModuleType:
    """The package under ``name`` (already loaded -> that module)."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        UI_PERF_DIR / "__init__.py",
        submodule_search_locations=[str(UI_PERF_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module
