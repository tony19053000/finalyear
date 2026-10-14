"""V0.8 G1 deliverable 6 (D171): ``scripts/export_openapi.py`` writes the
complete contract deterministically with no server, and ``--check`` catches
drift (the dashboard's gate)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_openapi.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_openapi", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_openapi"] = module
    spec.loader.exec_module(module)
    return module


def test_the_document_is_complete_and_deterministic() -> None:
    script = _load()
    first, second = script.render(), script.render()
    assert first == second and first.endswith("\n")
    doc = json.loads(first)
    paths = doc["paths"]
    for path in (
        "/v1/jobs",
        "/v1/session",
        "/v1/nodes/{node_id}/metrics",
        "/v1/auth/tokens",
        "/v1/fabric/invites",
        "/v1/secrets",
        "/v1/data",
        "/v1/blobs/have",
        "/healthz",
    ):
        assert path in paths, path
    job_list = doc["components"]["schemas"]["JobList"]
    assert "next_cursor" in job_list["properties"]
    assert "ErrorEnvelope" in doc["components"]["schemas"]


def test_out_and_check(tmp_path: Path) -> None:
    script = _load()
    target = tmp_path / "openapi.json"
    assert script.main(["--out", str(target)]) == 0
    assert script.main(["--check", str(target)]) == 0
    target.write_text(target.read_text().replace("next_cursor", "next_kursor"))
    assert script.main(["--check", str(target)]) == 1
    assert script.main(["--check", str(tmp_path / "missing.json")]) == 1
