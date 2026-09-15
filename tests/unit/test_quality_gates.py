"""Quality gates for task 0.1.9: ruff and mypy configuration really rejects bad code,
and the pre-commit hooks run the project's own tools.

Criterion 2 of V0.1 ("a deliberately bad commit -- lint/type/import-boundary -- is
rejected") is proved end-to-end with real commits by the lab/handoff procedure; these
tests pin the configuration that makes it true so a later edit cannot quietly weaken
it. The import-boundary half is covered by test_import_boundaries*.py.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
PYPROJECT = REPO / "pyproject.toml"


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _pyproject() -> dict[str, Any]:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_ruff_rejects_lint_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad_lint.py"
    bad.write_text("import os\n\n\ndef f(x=[]):\n    return x\n", encoding="utf-8")
    result = _run(
        "ruff", "check", "--no-cache", "--config", str(PYPROJECT), str(bad), cwd=REPO
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "F401" in result.stdout  # unused import
    assert "B006" in result.stdout  # mutable default argument


def test_ruff_format_check_rejects_unformatted_code(tmp_path: Path) -> None:
    bad = tmp_path / "bad_format.py"
    bad.write_text("x = {  'a':1 }\n", encoding="utf-8")
    result = _run(
        "ruff", "format", "--check", "--no-cache", "--config", str(PYPROJECT),
        str(bad), cwd=REPO,
    )  # fmt: skip
    assert result.returncode == 1, result.stdout + result.stderr


def test_mypy_rejects_untyped_and_ill_typed_code(tmp_path: Path) -> None:
    bad = tmp_path / "bad_types.py"
    bad.write_text(
        "def untyped(x):\n    return x\n\n\ndef wrong() -> int:\n    return 'no'\n",
        encoding="utf-8",
    )
    result = _run(
        "mypy",
        "--config-file",
        str(PYPROJECT),
        "--no-incremental",
        "--cache-dir",
        str(tmp_path / ".mypy_cache"),
        str(bad),
        cwd=tmp_path,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "[no-untyped-def]" in result.stdout
    assert "[return-value]" in result.stdout


def test_mypy_is_strict_for_common_controller_agent() -> None:
    mypy_cfg = _pyproject()["tool"]["mypy"]
    assert mypy_cfg.get("strict") is True
    required = {"meshforge.common.*", "meshforge.controller.*", "meshforge.agent.*"}
    strict_modules: set[str] = set()
    for override in mypy_cfg.get("overrides", []):
        modules = override["module"]
        modules = {modules} if isinstance(modules, str) else set(modules)
        if override.get("strict") is True:
            strict_modules |= modules
        # Nothing may relax the three core packages (the generated proto
        # subpackage is the one deliberate exemption).
        if override.get("ignore_errors") or override.get("strict") is False:
            relaxed = {m for m in modules if not m.startswith("meshforge.common.proto")}
            assert not (relaxed & required), override
    assert required <= strict_modules


def test_ruff_and_mypy_exclude_generated_protos() -> None:
    tool = _pyproject()["tool"]
    assert "src/meshforge/common/proto" in tool["ruff"]["extend-exclude"]
    assert tool["ruff"]["force-exclude"] is True
    excludes = tool["mypy"]["exclude"]
    excludes = [excludes] if isinstance(excludes, str) else excludes
    assert any("common/proto" in e for e in excludes)


def test_pre_commit_runs_project_tools_via_uv() -> None:
    with (REPO / ".pre-commit-config.yaml").open(encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    local = [r for r in config["repos"] if r["repo"] == "local"]
    assert len(local) == 1
    hooks = {h["id"]: h for h in local[0]["hooks"]}
    assert set(hooks) >= {
        "ruff-check",
        "ruff-format",
        "mypy",
        "import-linter",
        "gen-protos-check",
    }
    for hook in hooks.values():
        # Versions come from uv.lock; plain commands keep the hooks Windows-safe.
        assert hook["language"] == "system"
        assert hook["entry"].startswith("uv run "), hook
        assert "bash" not in hook["entry"]
        assert "&&" not in hook["entry"]
