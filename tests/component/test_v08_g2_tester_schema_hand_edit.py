"""V0.8 G2 tester r1: a hand-edited schema.d.ts is caught by the real
generator check (the unit digest test cannot see it)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.component

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    if not (FRONTEND / "node_modules" / "openapi-typescript").is_dir():
        pytest.skip("frontend/node_modules is missing - run `cd frontend && npm ci`")
    box = tmp_path / "frontend"
    (box / "src" / "api").mkdir(parents=True)
    shutil.copytree(FRONTEND / "scripts", box / "scripts")
    shutil.copy(FRONTEND / "openapi.json", box / "openapi.json")
    shutil.copy(FRONTEND / "package.json", box / "package.json")
    shutil.copy(
        FRONTEND / "src" / "api" / "schema.d.ts", box / "src" / "api" / "schema.d.ts"
    )
    (box / "node_modules").symlink_to(
        FRONTEND / "node_modules", target_is_directory=True
    )
    return box


def _check(box: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "scripts/gen-api.mjs", "--check"],
        cwd=box,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_untouched_copy_passes(sandbox: Path) -> None:
    assert _check(sandbox).returncode == 0


def test_hand_edited_schema_fails(sandbox: Path) -> None:
    schema = sandbox / "src" / "api" / "schema.d.ts"
    text = schema.read_text(encoding="utf-8")
    assert "string" in text
    schema.write_text(
        text.replace("session_id: string", "session_id: number", 1), encoding="utf-8"
    )
    result = _check(sandbox)
    assert result.returncode == 1, result.stdout
    assert "out of date" in result.stderr


def test_schema_with_digest_kept_but_body_edited_fails(sandbox: Path) -> None:
    """The cheat the unit digest test cannot see: header digest intact."""
    schema = sandbox / "src" / "api" / "schema.d.ts"
    schema.write_text(
        schema.read_text(encoding="utf-8") + "\nexport type Sneaky = 1;\n",
        encoding="utf-8",
    )
    assert _check(sandbox).returncode == 1


def test_backend_change_in_openapi_json_without_regeneration_fails(
    sandbox: Path,
) -> None:
    spec = sandbox / "openapi.json"
    spec.write_text(
        spec.read_text(encoding="utf-8").replace('"/v1/nodes"', '"/v1/nodez"', 1),
        encoding="utf-8",
    )
    assert _check(sandbox).returncode == 1


def test_missing_schema_fails(sandbox: Path) -> None:
    (sandbox / "src" / "api" / "schema.d.ts").unlink()
    assert _check(sandbox).returncode == 1
