"""V0.8 G7 item 0 (D206): tests that read the built dashboard detect a STALE
build instead of failing on one of its symptoms.

The build (``src/meshforge/controller/web/dist``) is git-ignored, so a checkout
can hold a build made from older frontend sources - the D206 gate failed twice
that way ("the build is not split"). ``npm run build`` now stamps
``dist/build-info.json`` with a digest of the build inputs
(``frontend/scripts/build-stamp.mjs``); ``mf_dashboard.dist_problem`` recomputes
it in Python and every dist-reading test skips with the rebuild command.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from mf_dashboard import (
    BUILD_STAMP,
    FRONTEND,
    REBUILD,
    dist_problem,
    sources_digest,
)

pytestmark = pytest.mark.component


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


def test_python_and_the_build_script_compute_the_same_digest() -> None:
    done = subprocess.run(
        [_node(), "scripts/build-stamp.mjs", "--print"],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert done.stdout.strip() == sources_digest()


def test_the_build_script_writes_the_stamp(tmp_path: Path) -> None:
    done = subprocess.run(
        [_node(), "scripts/build-stamp.mjs", str(tmp_path)],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    stamp = json.loads((tmp_path / BUILD_STAMP).read_text("utf-8"))
    assert stamp["schema"] == 1
    assert stamp["sources_sha256"] == sources_digest()
    assert stamp["source_files"] > 50
    assert "source files" in done.stdout


def _frontend(root: Path) -> Path:
    """A tiny stand-in frontend tree with the same layout rules."""
    fe = root / "fe"
    (fe / "src" / "test").mkdir(parents=True)
    (fe / "public").mkdir()
    (fe / "index.html").write_text("<!doctype html>")
    (fe / "package.json").write_text("{}")
    (fe / "src" / "main.tsx").write_text("export const a = 1;\n")
    (fe / "src" / "main.test.tsx").write_text("test\n")
    (fe / "src" / "test" / "setup.ts").write_text("setup\n")
    (fe / "public" / "favicon.svg").write_text("<svg/>")
    return fe


def _built(root: Path, digest: str | None) -> Path:
    dist = root / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>")
    if digest is not None:
        (dist / BUILD_STAMP).write_text(json.dumps({"sources_sha256": digest}))
    return dist


def test_a_fresh_build_has_no_problem(tmp_path: Path) -> None:
    fe = _frontend(tmp_path)
    dist = _built(tmp_path, sources_digest(fe))
    assert dist_problem(dist, fe) is None


def test_not_built_says_so(tmp_path: Path) -> None:
    fe = _frontend(tmp_path)
    problem = dist_problem(tmp_path / "nowhere", fe)
    assert problem is not None and "not built" in problem and REBUILD in problem


@pytest.mark.parametrize("stamp", [None, "{not json", '{"other": 1}', "[1]"])
def test_a_build_without_a_valid_stamp_may_be_stale(
    tmp_path: Path, stamp: str | None
) -> None:
    fe = _frontend(tmp_path)
    dist = _built(tmp_path, None)
    if stamp is not None:
        (dist / BUILD_STAMP).write_text(stamp)
    problem = dist_problem(dist, fe)
    assert problem is not None and BUILD_STAMP in problem and REBUILD in problem


@pytest.mark.parametrize(
    "change",
    ["src/main.tsx", "index.html", "package.json", "public/favicon.svg", "src/new.ts"],
)
def test_a_changed_build_input_makes_the_build_stale(
    tmp_path: Path, change: str
) -> None:
    fe = _frontend(tmp_path)
    dist = _built(tmp_path, sources_digest(fe))
    (fe / change).write_text("changed\n")
    problem = dist_problem(dist, fe)
    assert problem is not None and "STALE" in problem and REBUILD in problem


@pytest.mark.parametrize(
    "change", ["src/main.test.tsx", "src/test/setup.ts", "src/test/new.ts"]
)
def test_tests_are_not_build_inputs(tmp_path: Path, change: str) -> None:
    fe = _frontend(tmp_path)
    dist = _built(tmp_path, sources_digest(fe))
    (fe / change).write_text("changed\n")
    assert dist_problem(dist, fe) is None


def test_the_checkout_build_is_judged_without_error() -> None:
    """The real dist: either fresh (None) or a reason that says to rebuild -
    never an exception, whatever state the checkout is in."""
    problem = dist_problem()
    assert problem is None or REBUILD in problem
