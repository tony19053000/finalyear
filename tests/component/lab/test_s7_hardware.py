"""Component test for lab/spikes/s7_hardware.py (0.2.11): run it for real.

Runs the script as a subprocess (what ``uv run python lab/spikes/s7_hardware.py``
does) in ``--quick`` mode on this machine and checks the result envelope, the
generated report, and that no benchmark child process is left behind. It must
pass on any laptop: with or without torch, with or without a GPU - a missing
backend has to show up as a typed error, never as a crash or a missing section.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
LAB_DIR: Final = REPO_ROOT / "lab"
SCRIPT: Final = LAB_DIR / "spikes" / "s7_hardware.py"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from spikes._harness import MIN_VALID_REPS  # noqa: E402
from spikes._harness.envelope import validate  # noqa: E402
from spikes._s7.worker import Worker, WorkerError  # noqa: E402

pytestmark = pytest.mark.timeout(600)
EXIT_OK: Final = 0
EXIT_INCOMPLETE: Final = 3


def _run(*args: str, timeout: float = 540) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


def _python_children() -> set[int]:
    """PIDs of this process's living descendants (empty when psutil is absent)."""
    if importlib.util.find_spec("psutil") is None:
        return set()
    import psutil

    return {p.pid for p in psutil.Process(os.getpid()).children(recursive=True)}


@pytest.fixture(scope="module")
def quick_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Any, Any]:
    out_dir = tmp_path_factory.mktemp("s7") / "raw dir with space"
    before = _python_children()
    proc = _run("run", "--quick", "--out-dir", str(out_dir))
    after = _python_children()
    files = sorted(out_dir.glob("*.json"))
    assert files, f"no result file written\nstderr:\n{proc.stderr}"
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    return files[0], doc, (proc, after - before)


def test_quick_run_exits_cleanly_and_writes_one_json(
    quick_run: tuple[Path, Any, Any],
) -> None:
    path, doc, (proc, leaked) = quick_run
    assert proc.returncode in (EXIT_OK, EXIT_INCOMPLETE), proc.stderr
    assert proc.stdout == ""  # results go to the file, progress to stderr
    assert validate(doc, source=str(path)) is doc
    assert doc["spike"] == "S7"
    assert doc["topic"] == "S7-hardware"
    assert not leaked, f"benchmark child processes were left running: {leaked}"


def test_quick_mode_is_marked_so_it_cannot_pass_as_a_measurement(
    quick_run: tuple[Path, Any, Any],
) -> None:
    _, doc, (proc, _) = quick_run
    assert doc["params"]["quick"] is True
    assert doc["params"]["profile"] == "quick"
    assert doc["extra"]["quick"] is True
    assert any("QUICK" in w for w in doc["extra"]["warnings"])
    assert "QUICK MODE" in proc.stderr


def test_cpu_and_memory_metrics_have_at_least_five_repetitions(
    quick_run: tuple[Path, Any, Any],
) -> None:
    _, doc, _ = quick_run
    cases = doc["cases"]
    cpu = [c for c in cases if c.startswith("cpu-")]
    assert cpu, f"no CPU case ran: {sorted(cases)}"
    assert "memory-bandwidth" in cases
    for case_id in [*cpu, "memory-bandwidth"]:
        case = cases[case_id]
        assert case["aborted"] is False, (case_id, case["errors"])
        assert len(case["samples"]) >= MIN_VALID_REPS, case_id
        scores = [m for m in case["summary"] if not m.endswith("_s")] or [
            m for m in case["summary"] if m.endswith("_gb_s")
        ]
        assert scores, case_id
        for metric in scores:
            summary = case["summary"][metric]
            assert summary["n"] >= MIN_VALID_REPS
            assert summary["mean"] > 0
            assert summary["stdev"] is not None
            assert case["metrics"][metric], f"{case_id}/{metric} has no unit"


def test_every_case_records_its_size_formula_and_stability(
    quick_run: tuple[Path, Any, Any],
) -> None:
    _, doc, _ = quick_run
    for case_id, case in doc["cases"].items():
        setup = case["attrs"]["setup"]
        setup = setup[0] if isinstance(setup, list) else setup
        assert setup["backend"], case_id
        assert "formula" in setup, case_id
        stability = case["attrs"]["stability"]
        assert stability["n"] == len(case["samples"])
        assert isinstance(stability["thermal_throttling_suspected"], bool)


def test_gpu_section_is_present_or_an_explicit_error(
    quick_run: tuple[Path, Any, Any],
) -> None:
    _, doc, _ = quick_run
    gpu = doc["extra"]["gpu"]
    gpu_cases = [c for c in doc["cases"] if c.startswith("gpu-")]
    if gpu["available"]:
        assert gpu_cases, "a CUDA device was reported but no GPU case ran"
        assert gpu["devices"]
    else:
        assert not gpu_cases
        assert gpu["failure_type"] in {
            "torch_not_installed",
            "cuda_unavailable",
            "cuda_broken",
            "child_crashed",
            "worker_timeout",
            "child_not_started",
        }
        assert gpu["error"]
        types = {e["failure_type"] for e in doc["errors"]}
        assert gpu["failure_type"] in types or "gpu_skipped" in types


def test_environment_is_recorded_before_and_after(
    quick_run: tuple[Path, Any, Any],
) -> None:
    _, doc, _ = quick_run
    env = doc["extra"]["environment"]
    assert env["before"]["when"] == "before"
    assert env["after"]["when"] == "after"
    assert "power" in env["before"]
    assert doc["extra"]["on_battery"] in (True, False, None)
    if doc["extra"]["on_battery"]:
        assert any("BATTERY" in w for w in doc["extra"]["warnings"])


def test_planner_fields_are_documented_in_the_result(
    quick_run: tuple[Path, Any, Any],
) -> None:
    _, doc, _ = quick_run
    fields = doc["extra"]["planner_fields"]
    assert "extra.on_battery" in fields
    assert any(f.startswith("cpu-") for f in fields)
    assert doc["extra"]["metric_units"]["gflops"] == "GFLOP/s"


def test_report_builds_one_table_per_metric(
    quick_run: tuple[Path, Any, Any], tmp_path: Path
) -> None:
    path, doc, _ = quick_run
    out = tmp_path / "report.md"
    proc = _run(
        "report",
        "--results",
        str(path.parent),
        "--out",
        str(out),
        "--labels",
        f"L1={doc['host']['hostname']},L4=absent-laptop",
    )
    assert proc.returncode == EXIT_OK, proc.stderr
    text = out.read_text(encoding="utf-8")
    assert "S7 hardware scores" in text
    assert "### Hardware scores per laptop" in text
    assert "| Case | L1 | L4 |" in text
    assert "TBD" in text  # the laptop that has not run yet
    assert "### Run conditions" in text
    assert "Roadmap task: 0.2.11" in text


def test_report_without_results_still_writes_a_tbd_report(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    out = tmp_path / "r.md"
    proc = _run("report", "--results", str(empty), "--out", str(out))
    assert proc.returncode == EXIT_OK, proc.stderr
    assert "no S7 result files found" in proc.stderr
    assert "TBD" in out.read_text(encoding="utf-8")


def test_report_skips_a_corrupt_result_file(tmp_path: Path) -> None:
    results = tmp_path / "raw"
    results.mkdir()
    (results / "broken.json").write_text("{not json", encoding="utf-8")
    out = tmp_path / "r.md"
    proc = _run("report", "--results", str(results), "--out", str(out))
    assert proc.returncode == EXIT_OK, proc.stderr
    assert "skipped invalid result file" in proc.stderr
    assert "Skipped result files" in out.read_text(encoding="utf-8")


def test_a_hung_child_is_killed_and_reported(tmp_path: Path) -> None:
    """A benchmark that never answers costs one timeout, not the whole run."""
    script = tmp_path / "hung_child.py"
    script.write_text("import time\nwhile True: time.sleep(1)\n", encoding="utf-8")
    worker = Worker(script=script)
    worker.start()
    pid = worker.pid
    assert pid is not None
    try:
        with pytest.raises(WorkerError) as exc:
            worker.setup({"kind": "cpu_python"}, 2.0)
        assert exc.value.failure_type == "worker_timeout"
        assert exc.value.fatal is True
    finally:
        worker.close()
    if importlib.util.find_spec("psutil") is not None:
        import psutil

        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() in {
            psutil.STATUS_ZOMBIE,
            psutil.STATUS_DEAD,
        }


def test_a_child_that_dies_is_reported_as_crashed(tmp_path: Path) -> None:
    script = tmp_path / "dying_child.py"
    script.write_text(
        "import sys\nsys.stderr.write('kaboom\\n')\nraise SystemExit(7)\n",
        encoding="utf-8",
    )
    worker = Worker(script=script)
    worker.start()
    try:
        with pytest.raises(WorkerError) as exc:
            worker.setup({"kind": "cpu_python"}, 10.0)
        assert exc.value.failure_type in {"child_crashed", "child_gone"}
        assert exc.value.fatal is True
    finally:
        worker.close()


def test_a_missing_child_script_is_reported(tmp_path: Path) -> None:
    worker = Worker(script=tmp_path / "does_not_exist.py")
    worker.start()
    try:
        with pytest.raises(WorkerError) as exc:
            worker.setup({"kind": "cpu_python"}, 10.0)
        assert exc.value.fatal is True
    finally:
        worker.close()
