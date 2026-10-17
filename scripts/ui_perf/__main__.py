"""The CLI entry: ``uv run python scripts/ui_perf ...`` (a directory run) or
``uv run python -m scripts.ui_perf ...`` (from the repository root).

A directory run executes this file with no parent package, so the package is
loaded from its own folder first (relative imports need a package)."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _package() -> ModuleType:
    if __package__:
        return importlib.import_module(__package__)
    folder = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "ui_perf", folder / "__init__.py", submodule_search_locations=[str(folder)]
    )
    if spec is None or spec.loader is None:  # pragma: no cover - our own folder
        raise ImportError(f"cannot load the ui_perf package from {folder}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    sys.exit(_package().main())
