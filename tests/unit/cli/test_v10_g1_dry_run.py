"""V0.10 G1: ``compute run --dry-run`` prints the job and a split job's items
(count, first/last, chunking) without settings, upload or controller."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from meshforge.cli import shared as cli_shared
from meshforge.cli.app import app


@pytest.fixture(autouse=True)
def _no_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("a dry run must not contact the controller")

    monkeypatch.setattr(cli_shared, "_client", _boom)
    monkeypatch.setattr(cli_shared, "_settings", _boom)


def _project(root: Path, manifest: str, files: dict[str, bytes]) -> Path:
    root.mkdir()
    (root / "compute.yaml").write_text(manifest)
    (root / "main.py").write_text("print('hi')\n")
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    return root


def _run(*argv: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(app, ["run", *argv])
    return result.exit_code, result.stdout, result.stderr


def test_glob_split_shows_count_first_last_and_chunking(tmp_path: Path) -> None:
    files = {f"images/{n}.jpg": n.encode() for n in ["b", "A", "c", "é"]}
    root = _project(
        tmp_path / "p",
        "entrypoint: python main.py {items_file} {out}\n"
        "split:\n  over: {glob: 'images/*.jpg'}\n  chunk: {items: 3}\n",
        files,
    )
    code, out, err = _run("--dry-run", str(root))
    assert code == 0, err
    assert "dry run: nothing uploaded" in err
    rows = dict(line.split(None, 1) for line in out.splitlines() if line.strip())
    assert rows["split"] == "glob images/*.jpg"
    assert rows["items"] == "4 (first images/A.jpg, last images/é.jpg)"
    assert rows["chunking"] == "3 items per chunk -> 2 chunks"
    assert rows["combine"] == "concat"
    assert rows["nodes"] == "min 2, max all suitable"


def test_auto_chunking_and_range(tmp_path: Path) -> None:
    root = _project(
        tmp_path / "p",
        "entrypoint: python main.py\nsplit:\n  over: {range: {stop: 1000}}\n"
        "  combine: merge-json\n  max_nodes: 3\n",
        {},
    )
    code, out, _ = _run("--dry-run", str(root))
    assert code == 0
    assert "1000 (first 0, last 999)" in out
    assert "auto (the planner sizes chunks at dispatch)" in out
    assert "merge-json" in out and "min 2, max 3" in out


def test_lines_with_header(tmp_path: Path) -> None:
    root = _project(
        tmp_path / "p",
        "entrypoint: python main.py\nsplit:\n  over: {lines: t.csv, header: true}\n"
        "  combine: merge-csv\n",
        {"t.csv": b"h\n1\n2\n3\n"},
    )
    code, out, _ = _run("--dry-run", str(root))
    assert code == 0
    assert "lines t.csv (header repeated per chunk)" in out
    assert "3 (records 1..3)" in out


def test_zero_items_fails_with_the_yaml_path(tmp_path: Path) -> None:
    root = _project(
        tmp_path / "p", "entrypoint: python main.py\nsplit:\n  over: '*.jpg'\n", {}
    )
    code, _, err = _run("--dry-run", str(root))
    assert code == 1
    assert "split.over.glob: matched no files" in err


def test_not_a_split_job(tmp_path: Path) -> None:
    root = _project(tmp_path / "p", "entrypoint: python main.py\n", {})
    code, out, _ = _run("--dry-run", "--name", "My Job!", str(root))
    assert code == 0
    assert "no (the job runs whole on one node)" in out
    assert "My-Job" in out


def test_items_in_a_named_input_are_left_to_the_controller(tmp_path: Path) -> None:
    root = _project(
        tmp_path / "p",
        "entrypoint: python main.py\ninputs:\n  - {ref: ds@1, mount: data}\n"
        "split:\n  over: {list_file: data/ids.txt}\n",
        {},
    )
    code, out, _ = _run("--dry-run", str(root))
    assert code == 0
    assert "resolved by the controller at submit (named input data)" in out
    glob_root = _project(
        tmp_path / "q",
        "entrypoint: python main.py\ninputs:\n  - {ref: ds@1, mount: data}\n"
        "split:\n  over: '**/*.jpg'\n",
        {"local/a.jpg": b"a"},
    )
    code, out, _ = _run("--dry-run", str(glob_root))
    assert code == 0
    assert (
        "1 (first local/a.jpg, last local/a.jpg) + matches in named inputs data" in out
    )


def test_dry_run_with_follow_is_a_usage_error(tmp_path: Path) -> None:
    root = _project(tmp_path / "p", "entrypoint: python main.py\n", {})
    code, _, err = _run("--dry-run", "--follow", str(root))
    assert code == 2
    assert "nothing to follow" in err
