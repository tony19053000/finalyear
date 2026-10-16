"""V0.8 G2 deliverable 3 (D171): re-run the real generator (openapi-typescript
via ``frontend/scripts/gen-api.mjs --check``) and require byte-identical
output to the committed ``frontend/src/api/schema.d.ts``.

The unit test ``tests/unit/test_v08_g2_openapi_drift.py`` proves the same
without Node (recorded digest); this one also catches a hand edit of
schema.d.ts or a generator upgrade. Skips with the reason when Node 22 or
``frontend/node_modules`` (``npm ci``) is missing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.component

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def test_schema_d_ts_is_exactly_what_openapi_typescript_generates() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed (Node 22 builds the dashboard)")
    if not (FRONTEND / "node_modules" / "openapi-typescript").is_dir():
        pytest.skip("frontend/node_modules is missing - run `cd frontend && npm ci`")
    result = subprocess.run(
        [node, "scripts/gen-api.mjs", "--check"],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
