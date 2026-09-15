"""Adversarial probing of the import-linter contracts from task 0.1.4.

These tests independently try to break the import-boundary check with import
styles the "obvious" violation (a bare top-level `import meshforge.agent`,
already covered by ``test_import_boundaries.py``) does not exercise: lazy
imports inside functions, ``from x import y`` form, ``TYPE_CHECKING``-guarded
imports, relative imports, ``try/except ImportError`` guarded imports, fully
dynamic ``importlib.import_module`` calls, a brand-new (not-yet-listed)
subpackage, and a stray top-level module.

Several of these are expected to be CAUGHT (import-linter/grimp performs
static AST analysis, so it resolves the real target module regardless of
import *syntax*). Dynamic ``importlib`` imports remain a documented
limitation of static analysis. The unlisted-subpackage and stray-module gaps
(import-linter is allow-by-omission) are closed by the closed-world guard in
``_import_guard.py``; those tests assert import-linter alone still misses them
AND the guard catches them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _import_guard import boundary_problems

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
PYPROJECT = REPO_ROOT / "pyproject.toml"

_RUNNER = "from importlinter.cli import lint_imports_command; lint_imports_command()"


def _lint_imports(src_dir: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(src_dir) + (os.pathsep + existing if existing else "")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _RUNNER,
            "--config",
            str(PYPROJECT),
            "--no-cache",
            "--no-logo",
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def _copy_src(tmp_path: Path) -> Path:
    dest = tmp_path / "src"
    shutil.copytree(
        SRC_DIR, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    return dest


# ---------------------------------------------------------------------------
# Import styles that import-linter DOES catch (regression guards: these prove
# the contracts survive obfuscation, not just the naive top-level case).
# ---------------------------------------------------------------------------


def test_lazy_import_inside_function_is_detected(tmp_path: Path) -> None:
    """A forbidden import hidden inside a function body must still be caught."""
    src = _copy_src(tmp_path)
    (src / "meshforge" / "agent" / "_lazy.py").write_text(
        "def do_thing():\n"
        "    import meshforge.controller\n"
        "    return meshforge.controller\n",
        encoding="utf-8",
    )

    result = _lint_imports(src, cwd=tmp_path)

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "controller and agent never import each other" in output


def test_from_import_form_is_detected(tmp_path: Path) -> None:
    """``from meshforge import agent`` must be caught the same as ``import meshforge.agent``."""
    src = _copy_src(tmp_path)
    (src / "meshforge" / "controller" / "_fromimp.py").write_text(
        "from meshforge import agent\n", encoding="utf-8"
    )

    result = _lint_imports(src, cwd=tmp_path)

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "controller imports only common" in output


def test_type_checking_guarded_import_is_detected(tmp_path: Path) -> None:
    """A forbidden import used only for type hints (behind ``TYPE_CHECKING``)
    is still real coupling and must be caught, not silently allowed."""
    src = _copy_src(tmp_path)
    (src / "meshforge" / "controller" / "_typecheck.py").write_text(
        "from typing import TYPE_CHECKING\n\n"
        "if TYPE_CHECKING:\n"
        "    import meshforge.agent.main as x\n",
        encoding="utf-8",
    )

    result = _lint_imports(src, cwd=tmp_path)

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "controller imports only common" in output


def test_relative_import_is_detected(tmp_path: Path) -> None:
    """A relative import (``from ...agent import main``) from a nested
    sub-subpackage must resolve to the same absolute-module violation."""
    src = _copy_src(tmp_path)
    sub = src / "meshforge" / "controller" / "sub"
    sub.mkdir()
    (sub / "__init__.py").write_text("", encoding="utf-8")
    (sub / "_rel.py").write_text("from ...agent import main\n", encoding="utf-8")

    result = _lint_imports(src, cwd=tmp_path)

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "controller and agent never import each other" in output


def test_try_except_guarded_import_is_detected(tmp_path: Path) -> None:
    """An import wrapped in ``try/except ImportError`` (a common pattern for
    "soft" optional dependencies) must still count as a real dependency."""
    src = _copy_src(tmp_path)
    (src / "meshforge" / "controller" / "_tryexcept.py").write_text(
        "try:\n    import meshforge.agent\nexcept ImportError:\n    pass\n",
        encoding="utf-8",
    )

    result = _lint_imports(src, cwd=tmp_path)

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "controller and agent never import each other" in output


# ---------------------------------------------------------------------------
# Documented limitations / architecture gaps: these are NOT caught today.
# ---------------------------------------------------------------------------


def test_dynamic_importlib_import_module_is_not_detected(tmp_path: Path) -> None:
    """KNOWN LIMITATION: import-linter/grimp perform static AST analysis of
    import statements. A string-based ``importlib.import_module("...")`` call
    is invisible to it, so this boundary violation slips through CI silently.

    This is a fundamental limitation of static-analysis import checkers, not
    a bug in this implementation's contracts. It should be documented (e.g. in
    a code-review checklist or CONTRIBUTING.md) that dynamic imports of
    meshforge subpackages must be reviewed by hand.
    """
    src = _copy_src(tmp_path)
    (src / "meshforge" / "agent" / "_dyn.py").write_text(
        "import importlib\nmod = importlib.import_module('meshforge.controller')\n",
        encoding="utf-8",
    )

    result = _lint_imports(src, cwd=tmp_path)

    # Documents current (undesirable but expected) behaviour: the run is
    # reported clean even though the violation exists at runtime.
    assert result.returncode == 0, (
        "import-linter unexpectedly caught a dynamic importlib.import_module "
        "boundary violation -- if this now passes, dynamic-import detection "
        "has improved; update CONTRIBUTING.md/ADR to say dynamic imports of "
        "meshforge subpackages ARE checked and remove this xfail-documentation "
        "test.\n" + result.stdout + result.stderr
    )


# ---------------------------------------------------------------------------
# Allow-by-omission gaps: import-linter ALONE does not catch these (contracts
# only constrain modules they name), so each test asserts both halves: the
# gap still exists in import-linter, AND the closed-world guard
# (``_import_guard.boundary_problems``, run in CI via
# ``test_import_boundaries.py``) now catches it.
# ---------------------------------------------------------------------------


def test_new_unlisted_subpackage_is_caught_by_guard(tmp_path: Path) -> None:
    """A brand-new subpackage (e.g. meshforge.workloads) importing controller,
    agent AND gpu_service must not slip through CI unnoticed."""
    src = _copy_src(tmp_path)
    workloads = src / "meshforge" / "workloads"
    workloads.mkdir()
    (workloads / "__init__.py").write_text(
        "import meshforge.controller\n"
        "import meshforge.agent\n"
        "import meshforge.gpu_service\n",
        encoding="utf-8",
    )

    result = _lint_imports(src, cwd=tmp_path)
    problems = boundary_problems(src / "meshforge", PYPROJECT)

    # import-linter by itself still misses it (allow-by-omission) ...
    assert result.returncode == 0, result.stdout + result.stderr
    # ... but the guard fails CI and points at [tool.importlinter].
    assert any(
        "meshforge.workloads exists but no" in p and "[tool.importlinter]" in p
        for p in problems
    ), problems


def test_stray_top_level_module_is_caught_by_guard(tmp_path: Path) -> None:
    """A stray meshforge/foo.py importing controller must not slip through."""
    src = _copy_src(tmp_path)
    (src / "meshforge" / "foo.py").write_text(
        "import meshforge.controller\n", encoding="utf-8"
    )

    result = _lint_imports(src, cwd=tmp_path)
    problems = boundary_problems(src / "meshforge", PYPROJECT)

    assert result.returncode == 0, result.stdout + result.stderr
    assert any(
        "meshforge.foo exists but no" in p and "[tool.importlinter]" in p
        for p in problems
    ), problems
