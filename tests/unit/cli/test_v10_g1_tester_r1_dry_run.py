"""V0.10 G1 tester r1: ``compute run --dry-run`` never contacts the controller.

A real subprocess (the installed entry module) with the controller URL pointed
at a socket this test owns: the dry run must exit without a single connection
attempt, for success and for refusals, and must not write to the project.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _project(root: Path, manifest: str, files: dict[str, bytes]) -> Path:
    root.mkdir()
    (root / "compute.yaml").write_text(manifest)
    (root / "main.py").write_text("print(1)\n")
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def _dry(tmp_path: Path, project: Path, *extra: str) -> tuple[int, str, str, int]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "home" / ".config"),
        "MESHFORGE_CLI__CONTROLLER_URL": f"http://127.0.0.1:{port}",
        "MESHFORGE_CLI__TOKEN": "t" * 40,
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "meshforge.cli",
            "run",
            "--dry-run",
            str(project),
            *extra,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    accepted = 0
    try:
        while True:
            conn, _ = listener.accept()
            conn.close()
            accepted += 1
    except TimeoutError:
        pass
    finally:
        listener.close()
    return done.returncode, done.stdout, done.stderr, accepted


def test_dry_run_split_job_makes_zero_connections_and_changes_nothing(
    tmp_path: Path,
) -> None:
    files = {f"d/{n}.txt": n.encode() for n in "cab"}
    root = _project(
        tmp_path / "p",
        "entrypoint: python main.py {items_file} {out}\nsplit:\n  over: d/*.txt\n",
        files,
    )
    before = _snapshot(root)
    code, out, err, accepted = _dry(tmp_path, root)
    assert code == 0, err
    assert accepted == 0
    assert "3 (first d/a.txt, last d/c.txt)" in out
    assert _snapshot(root) == before


@pytest.mark.parametrize(
    "manifest",
    [
        "entrypoint: python main.py {items_file} {out}\nsplit:\n  over: nothing/*.txt\n",
        "entrypoint: python main.py {items_file} {out}\n"
        "split:\n  over: {range: {stop: 9007199254740990}}\n",
        "entrypoint: python main.py {items_file} {out}\nsplit:\n  over: {list_file: missing.txt}\n",
        "entrypoint: python main.py {out}\n",  # placeholder without split
        "entrypoint: [unbalanced\n",  # bad YAML
    ],
)
def test_refusals_exit_nonzero_with_zero_connections(
    tmp_path: Path, manifest: str
) -> None:
    root = _project(tmp_path / "p", manifest, {})
    code, _out, err, accepted = _dry(tmp_path, root)
    assert code != 0 and err.strip()
    assert accepted == 0
    assert "Traceback" not in err


def test_dry_run_of_a_plain_job_and_of_an_empty_glob_never_dials(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path / "p", "entrypoint: python main.py\n", {})
    code, _out, err, accepted = _dry(tmp_path, root)
    assert code == 0, err
    assert accepted == 0


def test_dry_run_with_unicode_project_path_and_huge_item_count(tmp_path: Path) -> None:
    root = _project(
        tmp_path / "pröjekt 日本",
        "entrypoint: python main.py {items_file} {out}\n"
        "split:\n  over: {range: {stop: 100000}}\n  chunk: {items: 10}\n",
        {},
    )
    code, out, err, accepted = _dry(tmp_path, root)
    assert code == 0, err
    assert accepted == 0
    assert "100000" in out and "10000 chunks" in out
