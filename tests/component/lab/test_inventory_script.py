"""Component test for lab/inventory.py (0.2.2): run the real script on this machine.

Runs ``python lab/inventory.py`` as a subprocess (the same interpreter/venv as the
test run, i.e. what ``uv run python lab/inventory.py`` does) and validates the JSON
against the schema, the docs/LAB.md §1 field map, and the privacy rules. Works on
any machine: missing tools/drivers must show up as recorded errors, never a crash.
"""

from __future__ import annotations

import getpass
import json
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
SCRIPT: Final = REPO_ROOT / "lab" / "inventory.py"
if str(REPO_ROOT / "lab") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "lab"))

from _inventory.model import LAB_FIELDS, SECTIONS  # noqa: E402

pytestmark = pytest.mark.timeout(600)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=540,
        check=False,
    )


def _resolve(doc: dict[str, Any], path: str) -> Any:
    node: Any = doc
    for part in path.split("."):
        node = node[part]
    return node


@pytest.fixture(scope="module")
def written(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, subprocess.CompletedProcess[str]]:
    out = (
        tmp_path_factory.mktemp("inv")
        / "dir with space ü"
        / f"{socket.gethostname()}.json"
    )
    return out, _run("--out", str(out))


def test_script_writes_valid_inventory(
    written: tuple[Path, subprocess.CompletedProcess[str]],
) -> None:
    out, proc = written
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 1
    assert doc["hostname"] == socket.gethostname()
    assert datetime.fromisoformat(doc["generated_at_utc"].replace("Z", "+00:00")).tzinfo
    assert tuple(sorted(doc["sections"])) == tuple(sorted(SECTIONS))
    for name, probe in doc["sections"].items():
        assert isinstance(probe["ok"], bool), name
        assert isinstance(probe["source"], str) and probe["source"], name
        assert ("value" in probe) is probe["ok"], name
        assert ("error" in probe) is (not probe["ok"]), name
    assert doc["failed_sections"] == sorted(
        n for n, p in doc["sections"].items() if not p["ok"]
    )
    for row, paths in LAB_FIELDS.items():
        for path in paths:
            assert _resolve(doc, path) is not None, (row, path)


def test_core_sections_work_on_this_machine(
    written: tuple[Path, subprocess.CompletedProcess[str]],
) -> None:
    out, _ = written
    s = json.loads(out.read_text(encoding="utf-8"))["sections"]
    for name in (
        "os",
        "cpu",
        "memory",
        "disks",
        "python",
        "nvidia",
        "network",
        "torch",
    ):
        assert s[name]["ok"], (name, s[name])
    assert s["cpu"]["value"]["logical_threads"] >= 1
    assert s["memory"]["value"]["total_bytes"] > 2**28
    nv = s["nvidia"]["value"]
    smi = nv["nvidia_smi"]
    # Either nvidia-smi works, or the failure is recorded with its text (Laptop 1 today).
    assert smi["ok"] or smi["error"]
    assert nv["driver_working"] is bool(smi["ok"] or nv["nvml"]["ok"])


def test_git_commit_matches_checkout(
    written: tuple[Path, subprocess.CompletedProcess[str]],
) -> None:
    out, _ = written
    git = json.loads(out.read_text(encoding="utf-8"))["sections"]["git"]
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if head.returncode != 0:
        pytest.skip(f"git unavailable: {head.stderr.strip()}")
    assert git["ok"], git
    assert git["value"]["commit"] == head.stdout.strip()
    assert isinstance(git["value"]["dirty"], bool)


def test_no_user_paths_in_output(
    written: tuple[Path, subprocess.CompletedProcess[str]],
) -> None:
    out, _ = written
    text = out.read_text(encoding="utf-8")
    assert str(Path.home()) not in text
    assert json.dumps(str(Path.home()))[1:-1] not in text  # JSON-escaped (Windows \\)
    user = getpass.getuser()
    for needle in (f"/{user}/", f"\\\\{user}\\\\"):
        assert needle not in text


def test_summary_on_stderr(
    written: tuple[Path, subprocess.CompletedProcess[str]],
) -> None:
    _, proc = written
    for label in (
        "hostname",
        "cpu",
        "ram",
        "gpus",
        "nvidia",
        "torch",
        "docker",
        "written to",
    ):
        assert label in proc.stderr


def test_stdout_mode_pretty() -> None:
    proc = _run("--pretty", "--no-summary")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("{\n")
    assert proc.stderr == ""
    assert json.loads(proc.stdout)["schema_version"] == 1


def test_usage_error_exits_2() -> None:
    proc = _run("--no-such-flag")
    assert proc.returncode == 2
    assert "usage" in proc.stderr.lower()


def test_unwritable_output_exits_1(tmp_path: Path) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    proc = _run("--out", str(blocker / "inv.json"), "--no-summary")
    assert proc.returncode == 1
    assert "cannot write" in proc.stderr
