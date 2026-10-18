"""Closed-world guard for the import-linter contracts (task 0.1.4).

import-linter contracts are allow-by-omission: a module that is not named in
any contract is unconstrained. This helper closes that gap by requiring the
``meshforge`` tree to be *fully declared* in ``pyproject.toml``:

1. every top-level entry under ``meshforge/`` (subpackage or stray module) is
   the ``source_modules`` of exactly one ``forbidden`` contract, so every
   entry states its outbound import policy;
2. every module named in any contract actually exists (no stale names);
3. for every entry, the set of siblings its contract does NOT forbid equals
   the architecture's allowed-dependency map (``ALLOWED_DEPS``). Adding a new
   subpackage therefore fails until each contract makes an explicit decision
   about it (forbid it, or add it to ``ALLOWED_DEPS``).

Rule 3 is what stops "laundering" (controller -> new_pkg -> agent) in general:
new_pkg must be forbidden to controller unless deliberately allowed, and
import-linter's forbidden contracts already follow indirect chains.

Pure Python (tomllib + filesystem), so it runs on temporary copies of the tree.
"""

from __future__ import annotations

import pkgutil
import tomllib
from pathlib import Path
from typing import Any

ROOT_PACKAGE = "meshforge"

# Architecture spec (ARCHITECTURE module boundaries): which sibling top-level
# entries each entry may import. Keep in sync with [tool.importlinter].
ALLOWED_DEPS: dict[str, frozenset[str]] = {
    "common": frozenset(),
    "controller": frozenset({"common", "bench"}),  # D232
    "agent": frozenset({"common"}),
    "gpu_service": frozenset({"common"}),
    "sdk": frozenset({"common"}),
    "cli": frozenset({"common", "sdk", "bench"}),
    "bench": frozenset({"common"}),
}

HINT = (
    "Every entry under src/meshforge/ must be declared in pyproject.toml "
    "[tool.importlinter] (a 'forbidden' contract with it as source_modules, "
    "and listed in the other contracts' forbidden_modules unless allowed) and "
    "in ALLOWED_DEPS in tests/unit/_import_guard.py."
)


def top_level_entries(package_dir: Path) -> set[str]:
    """Subpackages and top-level modules of ``package_dir`` (excl. ``_``-prefixed).

    Also counts directories without ``__init__.py`` that contain Python files
    (implicit namespace packages), which ``pkgutil.iter_modules`` skips.
    """
    names = {info.name for info in pkgutil.iter_modules([str(package_dir)])}
    names.update(
        child.name
        for child in package_dir.iterdir()
        if child.is_dir() and any(child.rglob("*.py"))
    )
    return {n for n in names if not n.startswith("_")}


def _short(module: str) -> str:
    """``meshforge.controller.x`` -> ``controller``; non-meshforge -> as-is."""
    parts = module.split(".")
    if parts[0] == ROOT_PACKAGE and len(parts) > 1:
        return parts[1]
    return module


def _contracts(pyproject: Path) -> list[dict[str, Any]]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    contracts: list[dict[str, Any]] = data["tool"]["importlinter"]["contracts"]
    return contracts


def boundary_problems(
    package_dir: Path,
    pyproject: Path,
    allowed_deps: dict[str, frozenset[str]] | None = None,
) -> list[str]:
    """Return human-readable problems; empty list means the tree is fully declared."""
    allowed = ALLOWED_DEPS if allowed_deps is None else allowed_deps
    entries = top_level_entries(package_dir)
    contracts = _contracts(pyproject)
    problems: list[str] = []

    policy: dict[str, set[str]] = {}
    named: set[str] = set()
    for contract in contracts:
        for key in ("source_modules", "forbidden_modules", "modules"):
            named.update(_short(m) for m in contract.get(key, []))
        if contract.get("type") != "forbidden":
            continue
        forbidden = {_short(m) for m in contract.get("forbidden_modules", [])}
        for src in contract.get("source_modules", []):
            name = _short(src)
            if name in policy:
                problems.append(
                    f"meshforge.{name} is source_modules of more than one "
                    "forbidden contract; keep one policy per entry."
                )
            policy[name] = forbidden

    problems.extend(
        f"meshforge.{name} exists but no 'forbidden' contract has it as "
        f"source_modules, so its imports are unconstrained. {HINT}"
        for name in sorted(entries - policy.keys())
    )
    problems.extend(
        f"meshforge.{name} is named in [tool.importlinter] but does not exist."
        for name in sorted(named - entries)
    )
    problems.extend(
        f"meshforge.{name} has no ALLOWED_DEPS entry. {HINT}"
        for name in sorted(entries - allowed.keys())
    )

    for name in sorted(entries & policy.keys() & allowed.keys()):
        not_forbidden = entries - policy[name] - {name}
        if not_forbidden != allowed[name]:
            problems.append(
                f"contract for meshforge.{name} permits {sorted(not_forbidden)} "
                f"but ALLOWED_DEPS says {sorted(allowed[name])}. {HINT}"
            )
    return problems
