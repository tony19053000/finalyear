"""Roadmap 0.1.8 / V0.1 criterion 1: a plain `uv sync` (no extras) must be able to
*collect* every test module, so `pytest -m unit` never aborts on an unrelated
component module that imports an optional-extra package at top level.

Structural guard (AST scan, no imports executed): in every ``tests/**/*.py`` file,
a module-level import of a package that only an optional extra provides
(``[project.optional-dependencies]`` in pyproject.toml: sqlalchemy, alembic,
asyncpg, fastapi, psutil, pynvml, torch, ...) must come *after* a module-level
``pytest.importorskip(...)`` of a package from the same extra. Extras install as a
unit, so skipping on ``sqlalchemy`` also guards ``alembic`` (both ``controller``).

Allowed without a guard: imports inside functions/fixtures (not run at collection),
``if TYPE_CHECKING:`` blocks, and the body of ``try: ... except ImportError``
(or ``ModuleNotFoundError``; a bare/``Exception`` handler is not a guard). Loop,
``with``, ``match``, class, and ``except``/``else``/``finally`` bodies are scanned.
Dev-group packages (docker, testcontainers, ...) are not optional: plain `uv sync`
installs them. Known limit: an indirect import (e.g. ``meshforge.controller.store
.engine`` pulling in sqlalchemy) is not traced; the real fresh-clone test
(tests/component/test_fresh_clone_unit_criterion.py) is the backstop for that.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
TESTS_DIR: Final = REPO_ROOT / "tests"

#: Distribution name -> top-level import names, where they differ or are several.
#: Every distribution in an optional extra must resolve here or to its own name
#: (checked by ``test_every_extra_distribution_has_an_import_name``).
DIST_TO_IMPORTS: Final[dict[str, tuple[str, ...]]] = {
    "nvidia-ml-py": ("pynvml",),
    "uvicorn": ("uvicorn",),
}


def _dist_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement.strip())
    assert match, f"unparseable requirement {requirement!r}"
    return match.group(0).lower().replace("_", "-")


def optional_import_extras(pyproject: Path) -> dict[str, frozenset[str]]:
    """Top-level import name -> the extras that provide it (and nothing in base)."""
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    base = {_dist_name(r) for r in project.get("dependencies", [])}
    result: dict[str, set[str]] = {}
    for extra, reqs in project.get("optional-dependencies", {}).items():
        for req in reqs:
            dist = _dist_name(req)
            if dist in base:
                continue
            for name in DIST_TO_IMPORTS.get(dist, (dist.replace("-", "_"),)):
                result.setdefault(name, set()).add(extra)
    return {name: frozenset(extras) for name, extras in result.items()}


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    """Only an explicit ``except ImportError``/``ModuleNotFoundError`` is a guard.

    A bare ``except:`` or ``except Exception`` would also swallow the error, but it
    hides the intent (and every other bug) -- this policy asks for the explicit form.
    """
    if handler.type is None:
        return False
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    guards = {"ImportError", "ModuleNotFoundError"}
    return any(isinstance(t, ast.Name) and t.id in guards for t in types)


def _importorskip_target(node: ast.stmt) -> str | None:
    value = node.value if isinstance(node, ast.Expr | ast.Assign) else None
    if not isinstance(value, ast.Call):
        return None
    func = value.func
    is_skip = (isinstance(func, ast.Attribute) and func.attr == "importorskip") or (
        isinstance(func, ast.Name) and func.id == "importorskip"
    )
    if is_skip and value.args and isinstance(value.args[0], ast.Constant):
        arg = value.args[0].value
        return arg.split(".")[0] if isinstance(arg, str) else None
    return None


def _collection_time_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Statements that may execute when the module is imported, in source order.

    Every compound statement's bodies are entered (loops, ``with``, ``match``
    cases, class bodies, and ``except``/``else``/``finally`` of a ``try``); only
    ``if TYPE_CHECKING:`` bodies, function bodies, and the body of a ``try`` with
    an explicit ``except ImportError``/``ModuleNotFoundError`` are skipped.
    """
    for node in body:
        if isinstance(node, ast.If):
            if not _is_type_checking(node.test):
                yield from _collection_time_statements(node.body)
            yield from _collection_time_statements(node.orelse)
        elif isinstance(node, ast.Try | ast.TryStar):
            if not any(_catches_import_error(h) for h in node.handlers):
                yield from _collection_time_statements(node.body)
            for handler in node.handlers:
                yield from _collection_time_statements(handler.body)
            yield from _collection_time_statements(node.orelse)
            yield from _collection_time_statements(node.finalbody)
        elif isinstance(node, ast.For | ast.AsyncFor | ast.While):
            yield from _collection_time_statements(node.body)
            yield from _collection_time_statements(node.orelse)
        elif isinstance(node, ast.With | ast.AsyncWith | ast.ClassDef):
            yield from _collection_time_statements(node.body)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                yield from _collection_time_statements(case.body)
        else:
            yield node


def unguarded_optional_imports(
    source: str, optional: dict[str, frozenset[str]], filename: str = "<src>"
) -> list[str]:
    """``file:line: import X`` for each collection-time optional import not guarded."""
    guarded_extras: set[str] = set()
    problems: list[str] = []
    tree = ast.parse(source, filename=filename)
    for node in _collection_time_statements(tree.body):
        target = _importorskip_target(node)
        if target is not None:
            guarded_extras |= optional.get(target, frozenset())
            continue
        if isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots = [node.module.split(".")[0]]
        else:
            continue
        for root in roots:
            extras = optional.get(root)
            if extras and not extras & guarded_extras:
                problems.append(
                    f"{filename}:{node.lineno}: `{root}` (extra: "
                    f"{', '.join(sorted(extras))}) imported at module level before "
                    f"`pytest.importorskip({root!r})`"
                )
    return problems


OPTIONAL: Final = optional_import_extras(REPO_ROOT / "pyproject.toml")


def test_scanner_knows_the_real_optional_packages() -> None:
    for name in ("sqlalchemy", "alembic", "asyncpg", "fastapi", "psutil", "pynvml"):
        assert name in OPTIONAL, f"{name} should be an optional-extra import"
    assert "torch" in OPTIONAL
    # docker is in the `agent` extra since V0.4 (f9ca474), so src imports must guard it.
    assert "docker" in OPTIONAL
    # Base / dev-group packages are always installed by a plain `uv sync`.
    for name in ("pydantic", "grpc", "pytest", "testcontainers"):
        assert name not in OPTIONAL


def test_every_extra_distribution_has_an_import_name() -> None:
    """A new extra whose import name differs from its dist name must be mapped."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
    for reqs in project["project"]["optional-dependencies"].values():
        for req in reqs:
            dist = _dist_name(req)
            if "-" in dist:
                assert dist in DIST_TO_IMPORTS, (
                    f"add {dist!r} to DIST_TO_IMPORTS with its import name(s)"
                )


def test_no_test_module_imports_an_optional_extra_before_importorskip() -> None:
    problems: list[str] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        problems += unguarded_optional_imports(
            path.read_text(encoding="utf-8"), OPTIONAL, rel
        )
    assert not problems, (
        "these would abort `pytest` collection on a plain `uv sync` (no extras); "
        "move the import below a module-level pytest.importorskip, into the test "
        "function, or under `if TYPE_CHECKING:`\n  " + "\n  ".join(problems)
    )


# --- the scanner itself -------------------------------------------------------

_OPT: Final = {
    "sqlalchemy": frozenset({"controller"}),
    "alembic": frozenset({"controller"}),
    "psutil": frozenset({"agent"}),
}


@pytest.mark.parametrize(
    "source",
    [
        # the exact 0.1.8 regression: import first, skip second
        "import pytest\nfrom alembic import command\n"
        "pytest.importorskip('sqlalchemy')\n",
        "import sqlalchemy\n",
        "import os, sqlalchemy.orm\n",
        # a skip for a *different* extra does not guard
        "import pytest\npytest.importorskip('psutil')\nimport alembic\n",
        # conditional / class-level code still runs at import time
        "import sys\nif sys.platform == 'linux':\n    import psutil\n",
        "class T:\n    import psutil\n",
        "try:\n    import psutil\nfinally:\n    pass\n",
        # loops, async with, match: their bodies run at import time too
        "for _ in range(1):\n    import psutil\n",
        "for _ in []:\n    pass\nelse:\n    import psutil\n",
        "while True:\n    import psutil\n    break\n",
        "async with ctx:\n    import psutil\n",
        "match 1:\n    case 1:\n        import psutil\n",
        "match 1:\n    case _:\n        import psutil\n",
        # except/else handler bodies are not protected by the try's own guard
        "try:\n    import os\nexcept ImportError:\n    import psutil\n",
        "try:\n    import os\nexcept OSError:\n    import psutil\n",
        "try:\n    import os\nexcept ImportError:\n    pass\n"
        "else:\n    import psutil\n",
        # only an explicit ImportError/ModuleNotFoundError counts as a guard
        "try:\n    import psutil\nexcept Exception:\n    psutil = None\n",
        "try:\n    import psutil\nexcept BaseException:\n    psutil = None\n",
        "try:\n    import psutil\nexcept:\n    psutil = None\n",
        "try:\n    import psutil\nexcept OSError:\n    psutil = None\n",
    ],
)
def test_scanner_flags_unguarded_imports(source: str) -> None:
    assert unguarded_optional_imports(source, _OPT)


@pytest.mark.parametrize(
    "source",
    [
        "import pytest\npytest.importorskip('sqlalchemy')\nfrom alembic import command\n",
        "import pytest\nsa = pytest.importorskip('sqlalchemy.orm')\nimport sqlalchemy\n",
        "from pytest import importorskip\nimportorskip('psutil')\nimport psutil\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import sqlalchemy\n",
        "import typing\nif typing.TYPE_CHECKING:\n    import sqlalchemy\n",
        "try:\n    import psutil\nexcept ImportError:\n    psutil = None\n",
        "try:\n    import psutil\nexcept ModuleNotFoundError:\n    psutil = None\n",
        "try:\n    import psutil\nexcept (OSError, ImportError):\n    psutil = None\n",
        "for _ in range(1):\n    def f():\n        import psutil\n",
        "match 1:\n    case 1:\n        pass\n",
        "def test_x():\n    import sqlalchemy\n",
        "from . import sqlalchemy\n",
        "import pydantic\nimport meshforge.controller.store.settings\n",
    ],
)
def test_scanner_accepts_guarded_or_lazy_imports(source: str) -> None:
    assert unguarded_optional_imports(source, _OPT) == []
