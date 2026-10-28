"""V1.0 G7: ``release_check.py`` refuses to run auto rows in an incomplete env.

Without ``uv sync --all-extras`` whole test modules ``importorskip`` (pytest exit
4, rows FAIL) and without the built dashboard the e2e rows are MISSING - the
report would blame the product for the build environment (G6 progress note,
item 5). The preflight lists what is missing, prints the fix and exits 2 before
anything runs; ``--no-preflight`` runs anyway and the report says so.
"""

from __future__ import annotations

import json
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import pytest

REPO: Final = Path(__file__).resolve().parents[3]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import release_check as rc  # noqa: E402
import release_manifest as rm  # noqa: E402

pytestmark = pytest.mark.unit

SHA: Final = "d" * 40


def _pyproject(tmp_path: Path, extras: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\nname = "x"\nversion = "0"\n\n[project.optional-dependencies]\n'
        + textwrap.dedent(extras),
        encoding="utf-8",
    )
    return path


# ----------------------------------------------------------------- extras


def test_installed_requirements_pass(tmp_path: Path) -> None:
    py = _pyproject(tmp_path, 'dev = ["pytest>=1", "PyYAML>=5"]\n')
    assert rc.missing_extras(py) == []


def test_a_missing_distribution_names_its_extra(tmp_path: Path) -> None:
    py = _pyproject(
        tmp_path,
        'ml = ["pytest", "meshforge-no-such-dist-g7>=1"]\n',
    )
    assert rc.missing_extras(py) == ["[ml] meshforge-no-such-dist-g7 is not installed"]


def test_a_too_old_or_too_new_version_is_reported(tmp_path: Path) -> None:
    py = _pyproject(tmp_path, 'controller = ["pytest<1"]\n')
    (problem,) = rc.missing_extras(py)
    assert problem.startswith("[controller] pytest ")
    assert "does not satisfy <1" in problem


def test_requirements_for_another_platform_are_ignored(tmp_path: Path) -> None:
    py = _pyproject(
        tmp_path,
        """\
        agent = [
            "meshforge-no-such-dist-g7; sys_platform == 'no-such-os'",
            "pytest; python_version >= '3'",
        ]
        """,
    )
    assert rc.missing_extras(py) == []


def test_an_unreadable_requirement_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    py = _pyproject(tmp_path, 'gpu = ["this is not a requirement !!"]\n')
    (problem,) = rc.missing_extras(py)
    assert problem.startswith("[gpu] unreadable requirement")


def test_no_optional_dependencies_is_nothing_missing(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
    assert rc.missing_extras(py) == []


# --------------------------------------------------------------------- UI


def test_missing_ui_lists_each_unbuilt_file(tmp_path: Path) -> None:
    assert rc.missing_ui(tmp_path) == [f"{rel} is missing" for rel in rc.UI_BUILDS]
    for rel in rc.UI_BUILDS:
        (tmp_path / rel).parent.mkdir(parents=True)
        (tmp_path / rel).write_text("<html></html>", encoding="utf-8")
    assert rc.missing_ui(tmp_path) == []


def test_ui_builds_are_the_files_the_bundle_builder_requires() -> None:
    builder = (REPO / "scripts" / "build_bundle.py").read_text(encoding="utf-8")
    for rel in rc.UI_BUILDS:
        assert rel.removeprefix("src/") in builder


def test_preflight_combines_both(tmp_path: Path) -> None:
    py = _pyproject(tmp_path, 'ml = ["meshforge-no-such-dist-g7"]\n')
    problems = rc.preflight(py, tmp_path)
    assert problems[0] == "[ml] meshforge-no-such-dist-g7 is not installed"
    assert len(problems) == 1 + len(rc.UI_BUILDS)


# -------------------------------------------------------------------- CLI


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A clean fake HEAD; auto rows "pass" without running pytest."""
    state: dict[str, Any] = {"evaluated": 0, "preflights": 0, "problems": []}
    monkeypatch.setattr(rc, "resolve_sha", lambda ref: SHA if ref == "HEAD" else None)
    monkeypatch.setattr(rc, "tree_is_dirty", lambda: False)
    monkeypatch.setattr(rc, "changed_files", lambda old, new: [])

    def fake_preflight() -> list[str]:
        state["preflights"] += 1
        return list(state["problems"])

    def fake_evaluate(rows: Sequence[rm.Row], workdir: Path) -> list[rc.Result]:
        state["evaluated"] += len(rows)
        return [rc.Result(r.id, r.kind, "PASS", "fake") for r in rows]

    monkeypatch.setattr(rc, "preflight", fake_preflight)
    monkeypatch.setattr(rc, "evaluate_auto", fake_evaluate)
    return state


def _auto_ids() -> list[str]:
    return [r.id for r in rm.load_manifest().rows if r.kind == "auto"][:2]


def test_a_failed_preflight_exits_2_before_any_row_runs(
    cli: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli["problems"] = ["[gpu] torch is not installed", rc.UI_BUILDS[0] + " is missing"]
    out = tmp_path / "r"
    code = rc.main(["--only", "auto", "--rows", *_auto_ids(), "--out", str(out)])
    assert code == 2
    assert cli["evaluated"] == 0
    assert not out.with_suffix(".json").exists()  # no half report
    err = capsys.readouterr().err
    assert "cannot run the auto rows (2 problems)" in err
    assert "[gpu] torch is not installed" in err
    assert rc.PREFLIGHT_FIX in err


def test_the_preflight_also_guards_a_run_of_all_kinds(
    cli: dict[str, Any], tmp_path: Path
) -> None:
    cli["problems"] = ["x"]
    assert rc.main(["--out", str(tmp_path / "r")]) == 2
    assert cli["evaluated"] == 0


def test_a_clean_preflight_runs_and_says_ok(
    cli: dict[str, Any], tmp_path: Path
) -> None:
    out = tmp_path / "r"
    code = rc.main(["--only", "auto", "--rows", *_auto_ids(), "--out", str(out)])
    assert code == 0
    assert (cli["preflights"], cli["evaluated"]) == (1, 2)
    assert json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))[
        "preflight"
    ] == ("ok")
    assert "| Preflight | ok |" in out.with_suffix(".md").read_text(encoding="utf-8")


def test_no_preflight_runs_anyway_and_marks_the_report(
    cli: dict[str, Any], tmp_path: Path
) -> None:
    cli["problems"] = ["x"]
    out = tmp_path / "r"
    argv = ["--only", "auto", "--rows", *_auto_ids(), "--no-preflight"]
    assert rc.main([*argv, "--out", str(out)]) == 0
    assert (cli["preflights"], cli["evaluated"]) == (0, 2)
    data = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["preflight"] == "skipped"
    assert "not release evidence" in out.with_suffix(".md").read_text(encoding="utf-8")


def test_lab_only_never_needs_the_preflight(
    cli: dict[str, Any], tmp_path: Path
) -> None:
    cli["problems"] = ["x"]
    out = tmp_path / "r"
    rc.main(["--only", "lab", "--lab-dir", str(tmp_path / "none"), "--out", str(out)])
    assert cli["preflights"] == 0
    data = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["preflight"] == "not_needed"


def test_the_real_preflight_of_this_checkout_returns_a_list() -> None:
    # whatever this env has installed, the real check runs without raising
    problems = rc.preflight()
    assert isinstance(problems, list)
    assert all(isinstance(p, str) for p in problems)
