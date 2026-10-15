"""V0.8 G2 deliverable 3 (D171 drift gate, D184 item 3).

The dashboard's typed client is generated in two committed steps:

1. ``scripts/export_openapi.py`` -> ``frontend/openapi.json`` (no server);
2. ``frontend/scripts/gen-api.mjs`` (openapi-typescript) -> ``frontend/src/api/schema.d.ts``,
   whose first line records the SHA-256 of the ``openapi.json`` it came from.

This test fails when either step is stale: a backend contract change without
``cd frontend && npm run gen:api`` breaks CI, not the dashboard at evaluation.
It needs no Node: step 2 is proven by the recorded digest. The component test
``tests/component/test_v08_g2_schema_regenerated.py`` re-runs the generator
itself where Node and ``frontend/node_modules`` exist.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

pytest.importorskip(
    "fastapi", reason="exporting the OpenAPI document needs the controller extra"
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "export_openapi.py"
SPEC = ROOT / "frontend" / "openapi.json"
SCHEMA = ROOT / "frontend" / "src" / "api" / "schema.d.ts"
HEADER_PREFIX = "// openapi.json sha256: "
REGENERATE = (
    "run `cd frontend && npm run gen:api` and commit openapi.json + src/api/schema.d.ts"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_openapi_g2", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_openapi_g2"] = module
    spec.loader.exec_module(module)
    return module


def test_committed_openapi_json_matches_the_controller() -> None:
    script = _load()
    assert script.main(["--check", str(SPEC)]) == 0, (
        f"frontend/openapi.json differs from the controller's contract: {REGENERATE}"
    )


def test_schema_d_ts_was_generated_from_the_committed_openapi_json() -> None:
    first_line = SCHEMA.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith(HEADER_PREFIX), (
        f"schema.d.ts has no source digest: {REGENERATE}"
    )
    recorded = first_line.removeprefix(HEADER_PREFIX).strip()
    actual = hashlib.sha256(SPEC.read_bytes()).hexdigest()
    assert recorded == actual, (
        f"schema.d.ts was generated from another openapi.json: {REGENERATE}"
    )


def test_the_types_the_dashboard_uses_exist_in_the_schema() -> None:
    text = SCHEMA.read_text(encoding="utf-8")
    for name in (
        "SessionOut",
        "LogoutOut",
        "WhoAmI",
        "NodeList",
        "NodeSummary",
        "ErrorDetail",
        "FailureType",
    ):
        assert f"        {name}: " in text, name
    for path in ('"/v1/session"', '"/v1/auth/whoami"', '"/v1/nodes"'):
        assert path in text, path
