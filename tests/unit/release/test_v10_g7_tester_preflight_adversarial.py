"""V1.0 G7 tester: break the release_check preflight / --only / --no-preflight contract."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Final

import pytest

REPO: Final = Path(__file__).resolve().parents[3]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import release_check as rc  # noqa: E402

pytestmark = pytest.mark.unit


def _pp(tmp_path: Path, extras: str) -> Path:
    p = tmp_path / "pyproject.toml"
    p.write_text(
        '[project]\nname="x"\nversion="0"\n[project.optional-dependencies]\n'
        + textwrap.dedent(extras),
        encoding="utf-8",
    )
    return p


def test_distribution_names_are_normalised() -> None:
    # pyproject spells it with underscores/dots/case; metadata lookup must still work
    assert rc.importlib.metadata.version("PyTest") == rc.importlib.metadata.version(
        "pytest"
    )


def test_underscore_vs_dash_names_do_not_false_alarm(tmp_path: Path) -> None:
    pp = _pp(tmp_path, 'a = ["Typing_Extensions", "packaging"]\n')
    assert rc.missing_extras(pp) == []


def test_marker_for_other_extra_name_and_python_version(tmp_path: Path) -> None:
    pp = _pp(
        tmp_path,
        "a = [\"nonexistent-dist-xyz; python_version < '3.0'\","
        " \"nonexistent-dist-zzz; sys_platform == 'plan9'\"]\n",
    )
    assert rc.missing_extras(pp) == []


def test_every_missing_dist_in_every_extra_is_reported_not_just_the_first(
    tmp_path: Path,
) -> None:
    pp = _pp(tmp_path, 'a = ["no-such-aaa", "no-such-bbb"]\nb = ["no-such-ccc"]\n')
    out = rc.missing_extras(pp)
    assert len(out) == 3
    assert any(x.startswith("[a] no-such-aaa") for x in out)
    assert any(x.startswith("[b] no-such-ccc") for x in out)


def test_unsatisfiable_pin_names_dist_and_specifier(tmp_path: Path) -> None:
    pp = _pp(tmp_path, 'a = ["pytest>=9999"]\n')
    (msg,) = rc.missing_extras(pp)
    assert "pytest" in msg and "9999" in msg


def test_directory_named_index_html_is_not_a_built_ui(tmp_path: Path) -> None:
    for rel in rc.UI_BUILDS:
        (tmp_path / rel).mkdir(parents=True)  # a DIRECTORY, not a file
    assert len(rc.missing_ui(tmp_path)) == len(rc.UI_BUILDS)


def test_empty_index_html_is_treated_as_built(tmp_path: Path) -> None:
    """Documents a gap: a 0-byte index.html passes the preflight (minor)."""
    for rel in rc.UI_BUILDS:
        f = tmp_path / rel
        f.parent.mkdir(parents=True)
        f.write_bytes(b"")
    assert rc.missing_ui(tmp_path) == []  # current behaviour, see report


def test_preflight_with_nonexistent_pyproject_raises_not_silently_passes(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        rc.preflight(tmp_path / "nope.toml", tmp_path)


def _run(*args: str, tmp: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "release_check.py"), *args],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=300,
        check=False,
    )


def test_real_cli_lab_only_does_not_run_preflight(tmp_path: Path) -> None:
    out = tmp_path / "r"
    proc = _run(
        "--only",
        "lab",
        "--lab-dir",
        str(tmp_path / "none"),
        "--out",
        str(out),
        tmp=tmp_path,
    )
    assert "cannot run the auto rows" not in proc.stderr
    data = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["preflight"] == "not_needed"
    # no lab evidence at all: the gate must NOT say pass
    assert proc.returncode == 1, proc.stderr


def test_real_cli_exit2_on_unknown_row_selection_before_preflight(
    tmp_path: Path,
) -> None:
    proc = _run("--rows", "no-such-row", "--out", str(tmp_path / "r"), tmp=tmp_path)
    assert proc.returncode == 2
    assert not (tmp_path / "r.json").exists()


@pytest.mark.skipif(
    bool(rc.preflight()) is False, reason="env is complete; nothing to refuse"
)
def test_real_cli_incomplete_env_exits_2_names_fix_writes_no_report(
    tmp_path: Path,
) -> None:
    out = tmp_path / "r"
    proc = _run("--only", "auto", "--out", str(out), tmp=tmp_path)
    assert proc.returncode == 2
    assert "cannot run the auto rows" in proc.stderr
    assert rc.PREFLIGHT_FIX in proc.stderr
    assert not out.with_suffix(".json").exists()
    assert not out.with_suffix(".md").exists()
    # all-kinds run is guarded identically
    proc2 = _run("--out", str(out), tmp=tmp_path)
    assert proc2.returncode == 2
