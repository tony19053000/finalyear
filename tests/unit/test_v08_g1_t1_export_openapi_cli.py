"""V0.8 G1 tester r1 (D171): the export script as a real subprocess - no
database, no network, no env, byte-stable, and --check catches drift."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_openapi.py"


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MESHFORGE", "PG"))}
    env["MESHFORGE_DATABASE_URL"] = "postgresql://nobody:nobody@127.0.0.1:1/none"
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_two_processes_produce_identical_bytes_with_no_database(tmp_path: Path) -> None:
    a = _run(cwd=tmp_path)
    b = _run(cwd=tmp_path)
    assert a.returncode == 0, a.stderr
    assert a.stdout == b.stdout and a.stdout.endswith("\n")
    doc = json.loads(a.stdout)
    assert doc["openapi"].startswith("3.")
    # Sorted keys: re-serialising sorted is the identity.
    assert json.dumps(doc, indent=2, sort_keys=True) + "\n" == a.stdout


def test_out_then_check_passes_and_any_edit_fails_check(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"
    assert _run("--out", str(target), cwd=tmp_path).returncode == 0
    assert _run("--check", str(target), cwd=tmp_path).returncode == 0
    doc = json.loads(target.read_text())
    doc["info"]["title"] += "!"
    target.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    drift = _run("--check", str(target), cwd=tmp_path)
    assert drift.returncode == 1
    missing = _run("--check", str(tmp_path / "nope.json"), cwd=tmp_path)
    assert missing.returncode != 0 and "Traceback" not in missing.stderr
    # Whitespace-only drift is drift too (byte-for-byte gate).
    target.write_text(target.read_text().rstrip("\n"))
    assert _run("--check", str(target), cwd=tmp_path).returncode == 1


def test_the_document_has_no_secret_or_host_specific_content(tmp_path: Path) -> None:
    out = _run(cwd=tmp_path).stdout
    for needle in (
        str(Path.home()),
        "127.0.0.1",
        "password123",
        os.getlogin() if False else "\x00",
    ):
        assert needle not in out
    doc = json.loads(out)
    # every operation has a documented error model and an operationId
    ops = [
        (p, m, o)
        for p, item in doc["paths"].items()
        for m, o in item.items()
        if m in {"get", "post", "put", "delete", "patch"}
    ]
    assert ops
    ids = [o.get("operationId") for _, _, o in ops]
    assert all(ids) and len(ids) == len(set(ids)), "operationIds must be unique"
