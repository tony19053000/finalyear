"""V0.17 G3: the agent console's typed client cannot drift from its API.

Same two committed steps as the dashboard (tests/unit/test_v08_g2_openapi_drift.py):
``scripts/export_openapi.py --console`` -> ``frontend/console-openapi.json`` ->
``frontend/scripts/gen-api.mjs --console`` -> ``frontend/src/agentConsole/schema.d.ts``
(first line = the SHA-256 of the JSON it came from). No Node needed here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

pytest.importorskip("fastapi", reason="exporting the console API needs the agent extra")

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "export_openapi.py"
SPEC = ROOT / "frontend" / "console-openapi.json"
SCHEMA = ROOT / "frontend" / "src" / "agentConsole" / "schema.d.ts"
HEADER_PREFIX = "// console-openapi.json sha256: "
REGENERATE = "run `cd frontend && npm run gen:console-api` and commit both files"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_openapi_g3", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_openapi_g3"] = module
    spec.loader.exec_module(module)
    return module


def test_committed_console_openapi_matches_the_agent() -> None:
    assert _load().main(["--console", "--check", str(SPEC)]) == 0, REGENERATE


def test_console_schema_was_generated_from_the_committed_json() -> None:
    first = SCHEMA.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith(HEADER_PREFIX), REGENERATE
    recorded = first.removeprefix(HEADER_PREFIX).strip()
    assert recorded == hashlib.sha256(SPEC.read_bytes()).hexdigest(), REGENERATE


def test_the_console_and_dashboard_documents_are_separate() -> None:
    console = SPEC.read_text(encoding="utf-8")
    dashboard = (ROOT / "frontend" / "openapi.json").read_text(encoding="utf-8")
    assert '"/console/v1/status"' in console and '"/v1/nodes"' not in console
    assert '"/console/v1/status"' not in dashboard
