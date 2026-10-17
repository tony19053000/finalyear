"""Task 0.1.4: import boundaries between MeshForge subpackages are enforced.

The contracts live in ``pyproject.toml`` ``[tool.importlinter]``. These tests run
import-linter on the real source tree (must pass) and on temporary copies of it
with one forbidden import injected (must fail), which proves the contracts are
actually enforced rather than merely present.

import-linter is run in a subprocess so the temporary ``meshforge`` copy can be
put first on ``PYTHONPATH`` without disturbing the installed package in this
interpreter. No shell is involved, so this works the same on Linux and Windows.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _import_guard import ALLOWED_DEPS, boundary_problems, top_level_entries

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Runs the import-linter CLI with the current interpreter (no reliance on a
# `lint-imports` executable being on PATH).
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


def test_real_tree_respects_import_boundaries(tmp_path: Path) -> None:
    result = _lint_imports(SRC_DIR, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 broken" in result.stdout


def test_unmodified_copy_passes(tmp_path: Path) -> None:
    """Control case: the copy mechanism itself does not cause failures."""
    src = _copy_src(tmp_path)
    result = _lint_imports(src, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


# (importing subpackage, imported module, contract name expected to break)
VIOLATIONS = [
    ("controller", "meshforge.agent", "controller and agent never import each other"),
    ("agent", "meshforge.controller", "controller and agent never import each other"),
    ("common", "meshforge.sdk", "common imports no other meshforge subpackage"),
    ("common", "meshforge.controller", "common imports no other meshforge subpackage"),
    ("controller", "meshforge.cli", "controller imports only common"),
    ("agent", "meshforge.gpu_service", "agent imports only common"),
    ("gpu_service", "meshforge.agent", "gpu_service imports only common"),
    ("sdk", "meshforge.controller", "sdk imports only common"),
    ("sdk", "meshforge.cli", "sdk imports only common"),
    ("cli", "meshforge.controller", "cli imports only common, sdk and bench"),
    ("cli", "meshforge.gpu_service", "cli imports only common, sdk and bench"),
    # V0.9 G2: the benchmark library sees only common, and nothing but cli sees it.
    ("bench", "meshforge.controller", "bench imports only common"),
    ("bench", "meshforge.cli", "bench imports only common"),
    ("controller", "meshforge.bench", "controller imports only common"),
    ("agent", "meshforge.bench", "agent imports only common"),
]


@pytest.mark.parametrize(
    ("importer", "imported", "contract"),
    VIOLATIONS,
    ids=[f"{a}->{b.rsplit('.', 1)[-1]}" for a, b, _ in VIOLATIONS],
)
def test_forbidden_import_is_detected(
    tmp_path: Path, importer: str, imported: str, contract: str
) -> None:
    src = _copy_src(tmp_path)
    offender = src / "meshforge" / importer / "_boundary_violation.py"
    offender.write_text(
        f"import {imported}  # deliberate boundary violation\n", encoding="utf-8"
    )

    result = _lint_imports(src, cwd=tmp_path)

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert contract in output
    assert f"meshforge.{importer}._boundary_violation" in output


def test_indirect_forbidden_import_is_detected(tmp_path: Path) -> None:
    """cli -> sdk is allowed, but sdk -> controller makes cli reach controller too."""
    src = _copy_src(tmp_path)
    (src / "meshforge" / "sdk" / "_bridge.py").write_text(
        "import meshforge.controller\n", encoding="utf-8"
    )
    (src / "meshforge" / "cli" / "_uses_bridge.py").write_text(
        "import meshforge.sdk._bridge\n", encoding="utf-8"
    )

    result = _lint_imports(src, cwd=tmp_path)

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "cli imports only common, sdk and bench" in output


def test_tree_is_fully_declared_in_contracts() -> None:
    """Closed world: every meshforge entry has a contract and an explicit policy."""
    problems = boundary_problems(SRC_DIR / "meshforge", PYPROJECT)
    assert not problems, "\n".join(problems)


def test_guard_sees_all_current_subpackages() -> None:
    """Control: the guard's discovery matches the architecture's package list."""
    assert top_level_entries(SRC_DIR / "meshforge") == set(ALLOWED_DEPS)


@pytest.mark.parametrize(
    ("relpath", "content"),
    [
        ("planner/__init__.py", ""),
        ("workloads/__init__.py", "import meshforge.agent\n"),
        ("nsdir/mod.py", "import meshforge.controller\n"),
        ("foo.py", "import meshforge.controller\n"),
    ],
    ids=["new-package", "new-package-importing", "namespace-dir", "stray-module"],
)
def test_guard_flags_undeclared_entry(
    tmp_path: Path, relpath: str, content: str
) -> None:
    src = _copy_src(tmp_path)
    target = src / "meshforge" / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    name = relpath.split("/")[0].removesuffix(".py")

    problems = boundary_problems(src / "meshforge", PYPROJECT)

    assert any(f"meshforge.{name} exists but no" in p for p in problems), problems
    assert any("[tool.importlinter]" in p for p in problems)


def test_guard_ignores_private_and_pycache(tmp_path: Path) -> None:
    src = _copy_src(tmp_path)
    (src / "meshforge" / "_private.py").write_text("", encoding="utf-8")
    (src / "meshforge" / "__pycache__").mkdir(exist_ok=True)
    assert boundary_problems(src / "meshforge", PYPROJECT) == []


def test_guard_flags_contract_not_forbidding_new_sibling(tmp_path: Path) -> None:
    """Giving a new package its own contract is not enough: every other
    contract must explicitly forbid it (or ALLOWED_DEPS must allow it).
    Otherwise controller -> planner -> agent could launder a forbidden path."""
    src = _copy_src(tmp_path)
    (src / "meshforge" / "planner").mkdir()
    (src / "meshforge" / "planner" / "__init__.py").write_text("", encoding="utf-8")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        PYPROJECT.read_text(encoding="utf-8") + "\n[[tool.importlinter.contracts]]\n"
        'id = "planner-imports-common-only"\n'
        'name = "planner imports only common"\n'
        'type = "forbidden"\n'
        'source_modules = ["meshforge.planner"]\n'
        'forbidden_modules = ["meshforge.controller", "meshforge.agent",'
        ' "meshforge.gpu_service", "meshforge.sdk", "meshforge.cli"]\n',
        encoding="utf-8",
    )
    allowed = {**ALLOWED_DEPS, "planner": frozenset({"common"})}

    problems = boundary_problems(src / "meshforge", pyproject, allowed_deps=allowed)

    assert not any("meshforge.planner exists" in p for p in problems), problems
    assert any(
        "contract for meshforge.controller permits ['common', 'planner']" in p
        for p in problems
    ), problems


def test_guard_flags_stale_contract_name(tmp_path: Path) -> None:
    src = _copy_src(tmp_path)
    shutil.rmtree(src / "meshforge" / "gpu_service")
    problems = boundary_problems(src / "meshforge", PYPROJECT)
    assert any("gpu_service is named" in p for p in problems), problems


def test_allowed_imports_pass(tmp_path: Path) -> None:
    """Imports the architecture permits must not be flagged."""
    src = _copy_src(tmp_path)
    allowed = {
        "controller": "meshforge.common",
        "agent": "meshforge.common",
        "gpu_service": "meshforge.common",
        "sdk": "meshforge.common",
        "cli": "meshforge.sdk",
    }
    for importer, imported in allowed.items():
        (src / "meshforge" / importer / "_allowed.py").write_text(
            f"import {imported}\n", encoding="utf-8"
        )

    result = _lint_imports(src, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
