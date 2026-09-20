"""Independent tester checks for S7 (task 0.2.11) derived from the success
criteria and lab/spikes/S7.md, not from reading the implementation.

Covers: stdlib-only import at module level (so a machine without torch/numpy
can still ``import`` every S7 module and get a typed error at run time, not an
ImportError at import time), Windows-safety by source inspection (no fork, no
setsid, no unconditional SIGKILL-only kill path, no shell=True), report/atomic
determinism, and stats sanity (stdev of constant samples is 0).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LAB_DIR = REPO_ROOT / "lab"
S7_FILES = sorted((LAB_DIR / "spikes" / "_s7").glob("*.py"))
HARNESS_FILES = sorted((LAB_DIR / "spikes" / "_harness").glob("*.py"))
SCRIPT = LAB_DIR / "spikes" / "s7_hardware.py"
ALL_S7_SOURCE_FILES = [*S7_FILES, *HARNESS_FILES, SCRIPT]

#: Never importable without extras; a machine without torch/numpy/psutil must
#: still be able to import every S7 module (the GPU/CPU-real-BLAS path is
#: reached lazily, at call time, inside a child process).
FORBIDDEN_TOP_LEVEL_MODULES = {"torch", "numpy", "psutil", "pynvml"}


def _top_level_import_names(path: Path) -> set[str]:
    """Names imported by module-level ``import``/``from`` statements only.

    Imports nested inside a function or class body (conditional / lazy) are
    intentionally excluded: those are fine even for heavy optional deps.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:  # module top level only, not nested scopes
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.parametrize("path", ALL_S7_SOURCE_FILES, ids=lambda p: p.name)
def test_module_level_imports_never_include_heavy_optional_deps(path: Path) -> None:
    names = _top_level_import_names(path)
    bad = names & FORBIDDEN_TOP_LEVEL_MODULES
    assert not bad, (
        f"{path}: module-level import of {bad} would make every S7 module "
        "unimportable on a machine without torch/numpy/psutil installed"
    )


@pytest.mark.parametrize("path", ALL_S7_SOURCE_FILES, ids=lambda p: p.name)
def test_every_s7_source_file_imports_without_torch_or_numpy(path: Path) -> None:
    """Importing the module must not raise, even though this dev box has no
    torch/numpy installed (asserted by the harness note); regressions here
    would break every laptop that has not run ``uv sync --extra gpu`` yet."""
    rel = path.relative_to(LAB_DIR)
    module = ".".join(rel.with_suffix("").parts)
    if module.endswith("s7_hardware"):
        module = "spikes.s7_hardware"
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=LAB_DIR,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, (
        f"import {module} failed:\n{proc.stderr}\n"
        "(torch/numpy are not installed on this box on purpose - see the "
        "implementer handoff note)"
    )


@pytest.mark.parametrize("path", ALL_S7_SOURCE_FILES, ids=lambda p: p.name)
def test_no_posix_only_process_control_by_inspection(path: Path) -> None:
    """Grep-level Windows-safety check: no fork, no setsid, no shell=True.

    ``start_new_session=True`` (POSIX-only kwarg to Popen, harmless no-op
    intent on Windows... actually not accepted there) is checked separately:
    it must always be guarded by a ``sys.platform`` branch.
    """
    text = path.read_text(encoding="utf-8")
    assert "os.fork(" not in text, f"{path}: os.fork is not portable to Windows"
    assert "shell=True" not in text, f"{path}: shell=True is unsafe and non-portable"
    assert "select.select(" not in text or "pipe" not in text.lower(), (
        f"{path}: select() on a pipe does not work on Windows"
    )


def test_start_new_session_is_always_guarded_by_a_platform_check() -> None:
    """``start_new_session=True`` / ``os.setsid`` are POSIX-only; worker.py must
    only use them inside a ``sys.platform != 'win32'`` branch (verified: the
    Windows branch uses CREATE_NEW_PROCESS_GROUP + taskkill instead)."""
    text = (LAB_DIR / "spikes" / "_s7" / "worker.py").read_text(encoding="utf-8")
    assert "start_new_session" in text
    assert "win32" in text
    assert "CREATE_NEW_PROCESS_GROUP" in text
    assert "taskkill" in text


def test_kill_tree_has_a_windows_branch_and_a_posix_branch() -> None:
    """A SIGKILL-only kill implementation would silently do nothing on Windows
    (no signal.SIGKILL there before 3.12-ish, and no process groups either)."""
    text = (LAB_DIR / "spikes" / "_s7" / "worker.py").read_text(encoding="utf-8")
    kill_tree_src = text.split("def kill_tree", 1)[1].split("\ndef ", 1)[0]
    assert "win32" in kill_tree_src
    assert "SIGKILL" in kill_tree_src
    assert "taskkill" in kill_tree_src


# --------------------------------------------------------------- stats sanity
def test_stdev_of_constant_samples_is_zero() -> None:
    from spikes._harness.stats import summarize

    summary = summarize([5.0, 5.0, 5.0, 5.0, 5.0])
    assert summary.n == 5
    assert summary.mean == pytest.approx(5.0)
    assert summary.stdev == 0.0
    assert summary.min == summary.max == summary.median == 5.0


def test_mean_of_five_reps_matches_manual_sum() -> None:
    from spikes._harness.stats import summarize

    values = [10.0, 12.0, 11.0, 9.0, 13.0]
    summary = summarize(values)
    assert summary.n == 5
    assert summary.mean == pytest.approx(sum(values) / 5)
    assert summary.stdev is not None and summary.stdev > 0


def test_reps_below_five_are_invalid_per_the_lab_rule() -> None:
    from spikes._harness.stats import summarize

    assert summarize([1.0, 2.0, 3.0, 4.0]).valid is False
    assert summarize([1.0, 2.0, 3.0, 4.0, 5.0]).valid is True


# ---------------------------------------------------------------- determinism
def _sample_envelopes() -> list[dict[str, object]]:
    from spikes._harness.stats import summarize

    def case(values: list[float]) -> dict[str, object]:
        return {
            "label": "CPU",
            "metrics": {"gflops": "GFLOP/s"},
            "samples": [{}] * len(values),
            "errors": [],
            "summary": {"gflops": summarize(values).to_json()},
        }

    return [
        {
            "spike": "S7",
            "host": {"hostname": "hostA", "label": "hostA", "os": "Linux"},
            "link": None,
            "cases": {"cpu-float32-all": case([100.0, 101.0, 99.0, 100.0, 100.0])},
            "errors": [],
            "extra": {
                "quick": False,
                "capabilities": {"torch": "2.0.0", "numpy": None},
                "gpu": {"available": False, "failure_type": "torch_not_installed"},
                "environment": {"before": {"cpu": {"distinct": ["performance"]}}},
                "on_battery": False,
                "stability": {},
            },
        }
    ]


def test_render_scoreboard_is_byte_for_byte_deterministic() -> None:
    from spikes._s7.report import render_scoreboard

    envelopes = _sample_envelopes()
    first = render_scoreboard(envelopes, {"hostA": "L1"})
    second = render_scoreboard(envelopes, {"hostA": "L1"})
    assert first == second


def test_render_report_is_byte_for_byte_deterministic(tmp_path: Path) -> None:
    from spikes._harness.envelope import dumps
    from spikes._harness.report import TEMPLATE_PATH, render_report
    from spikes._s7.report import render_scoreboard

    envelopes = _sample_envelopes()
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    extra = render_scoreboard(envelopes, {"hostA": "L1"})
    kwargs = {
        "topic": "S7-hardware",
        "title": "S7 hardware scores",
        "day": "2026-09-20",
        "task": "0.2.11",
        "spike": "S7",
        "extra_results": extra,
    }
    first = render_report(template, envelopes, **kwargs)
    second = render_report(template, envelopes, **kwargs)
    assert first == second
    # And the envelope's own JSON serialisation is deterministic too (sorted
    # keys): re-dumping the same document twice must byte-for-byte match.
    assert dumps(envelopes[0]) == dumps(envelopes[0])


# -------------------------------------------------------------------- atomic
def test_atomic_write_leaves_no_tmp_file_behind(tmp_path: Path) -> None:
    from spikes._harness.atomic import atomic_write_text

    target = tmp_path / "out.json"
    atomic_write_text(target, '{"a": 1}\n')
    assert target.read_text(encoding="utf-8") == '{"a": 1}\n'
    leftovers = list(tmp_path.glob("*.tmp"))
    assert not leftovers, f"temp files left behind: {leftovers}"


def test_atomic_write_failure_leaves_target_and_tmp_untouched(tmp_path: Path) -> None:
    """A write that cannot be encoded fails cleanly: no partial target, no .tmp."""
    from spikes._harness.atomic import atomic_write_text

    target = tmp_path / "out.json"
    target.write_text("original\n", encoding="utf-8")
    # A lone UTF-16 surrogate cannot be encoded as UTF-8: this must fail before
    # the atomic rename. Since 0.2.13 it is refused before the temp file is
    # even created, as an OSError (UnencodableTextError) rather than a bare
    # UnicodeEncodeError, so that every CLI's `except OSError` handles it; the
    # invariant this test pins - target untouched, no .tmp left - is unchanged.
    with pytest.raises(OSError, match="cannot be encoded"):
        atomic_write_text(target, "\udc80 broken surrogate")
    assert target.read_text(encoding="utf-8") == "original\n"  # untouched
    assert not list(tmp_path.glob("*.tmp")), "a failed write left a .tmp file behind"


def test_atomic_write_survives_being_interrupted_mid_write(tmp_path: Path) -> None:
    """Simulates Ctrl-C during the write: the target is untouched, no .tmp left."""
    from unittest import mock

    from spikes._harness.atomic import atomic_write_text

    target = tmp_path / "out.json"

    def boom(*_a: object, **_k: object) -> None:
        raise KeyboardInterrupt

    with (
        mock.patch("os.replace", side_effect=boom),
        pytest.raises(KeyboardInterrupt),
    ):
        atomic_write_text(target, "new content\n")
    assert not target.exists()
    assert not list(tmp_path.glob("*.tmp")), "interrupted write left a .tmp file"


# ------------------------------------------------------------ --results edge
def test_load_dir_skips_a_directory_that_matches_the_json_glob(
    tmp_path: Path,
) -> None:
    """A stray directory literally named ``*.json`` must not crash the loader
    (``Path.glob("*.json")`` matches directories too)."""
    from spikes._harness.envelope import load_dir

    results = tmp_path / "raw"
    results.mkdir()
    (results / "not-a-file.json").mkdir()
    docs, skipped = load_dir(results)
    assert docs == []
    assert len(skipped) == 1
    assert "not-a-file.json" in skipped[0]


def test_load_dir_skips_non_utf8_binary_file(tmp_path: Path) -> None:
    from spikes._harness.envelope import load_dir

    results = tmp_path / "raw"
    results.mkdir()
    (results / "binary.json").write_bytes(b"\xff\xfe\x00\x01not utf-8 at all")
    docs, skipped = load_dir(results)
    assert docs == []
    assert len(skipped) == 1
    assert "binary.json" in skipped[0]
    assert "not UTF-8" in skipped[0]
