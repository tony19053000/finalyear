"""Independent tester (0.2.11): failure injection against the real S7 spike.

Real child processes, real subprocess calls to ``lab/spikes/s7_hardware.py``.
Never edits lab/ code; only exercises it adversarially from the outside.
"""

from __future__ import annotations

import getpass
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
LAB_DIR: Final = REPO_ROOT / "lab"
SCRIPT: Final = LAB_DIR / "spikes" / "s7_hardware.py"
CHILD_SCRIPT: Final = LAB_DIR / "spikes" / "_s7" / "child.py"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from spikes._s7.worker import Worker, WorkerError  # noqa: E402

pytestmark = pytest.mark.timeout(300)
EXIT_OK: Final = 0
EXIT_INCOMPLETE: Final = 3


def _run(*args: str, timeout: float = 240) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


def _benchmark_children() -> set[int]:
    proc = Path("/proc")
    if not proc.is_dir():
        return set()
    pids: set[int] = set()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if str(CHILD_SCRIPT).encode() in cmdline:
            pids.add(int(entry.name))
    return pids


# ------------------------------------------------------------- garbage child
def test_child_that_prints_garbage_non_json_line_is_a_typed_bad_answer(
    tmp_path: Path,
) -> None:
    script = tmp_path / "garbage_child.py"
    script.write_text(
        "import sys\n"
        "for line in sys.stdin:\n"
        "    sys.stdout.write('this is not json at all {{{\\n')\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    worker = Worker(script=script)
    worker.start()
    try:
        with pytest.raises(WorkerError) as exc:
            worker.setup({"kind": "cpu_python"}, 10.0)
        assert exc.value.failure_type == "bad_answer"
    finally:
        worker.close()


def test_child_that_prints_a_valid_json_that_is_not_an_object_is_rejected(
    tmp_path: Path,
) -> None:
    script = tmp_path / "list_child.py"
    script.write_text(
        "import sys\nfor line in sys.stdin:\n    sys.stdout.write('[1, 2, 3]\\n')\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    worker = Worker(script=script)
    worker.start()
    try:
        with pytest.raises(WorkerError) as exc:
            worker.setup({"kind": "cpu_python"}, 10.0)
        assert exc.value.failure_type == "bad_answer"
    finally:
        worker.close()


def test_child_that_prints_partial_then_crashes_is_reported_not_hung(
    tmp_path: Path,
) -> None:
    """One well-formed line then a crash before answering the next request."""
    script = tmp_path / "half_answer_child.py"
    script.write_text(
        "import sys, json\n"
        "line = sys.stdin.readline()\n"
        "sys.stdout.write(json.dumps({'ok': True, 'info': {}}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stdin.readline()\n"
        "sys.exit(9)\n",
        encoding="utf-8",
    )
    worker = Worker(script=script)
    worker.start()
    try:
        worker.setup({"kind": "cpu_python"}, 10.0)  # first request succeeds
        with pytest.raises(WorkerError) as exc:
            worker.rep(10.0)  # child dies before answering the second
        assert exc.value.failure_type == "child_crashed"
    finally:
        worker.close()


def test_child_that_writes_invalid_utf8_bytes_does_not_crash_the_parent(
    tmp_path: Path,
) -> None:
    """The parent's pipes are opened with ``errors="replace"``; invalid bytes
    become U+FFFD, so the line is unreadable JSON rather than a parent crash."""
    script = tmp_path / "binary_child.py"
    script.write_text(
        "import sys\n"
        "for line in sys.stdin:\n"
        "    sys.stdout.buffer.write(b'\\xff\\xfe not valid utf-8 \\x80\\x81\\n')\n"
        "    sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    worker = Worker(script=script)
    worker.start()
    try:
        with pytest.raises(WorkerError) as exc:
            worker.setup({"kind": "cpu_python"}, 10.0)
        assert exc.value.failure_type == "bad_answer"
    finally:
        worker.close()


def test_child_that_exits_immediately_is_reported_as_gone_or_crashed(
    tmp_path: Path,
) -> None:
    script = tmp_path / "instant_exit_child.py"
    script.write_text("", encoding="utf-8")  # exits 0 immediately, no output
    worker = Worker(script=script)
    worker.start()
    try:
        with pytest.raises(WorkerError) as exc:
            worker.setup({"kind": "cpu_python"}, 10.0)
        assert exc.value.failure_type in {"child_crashed", "child_gone"}
        assert exc.value.fatal is True
    finally:
        worker.close()


# ------------------------------------------------------------- results dir
def test_report_skips_a_foreign_spike_result_without_crashing(tmp_path: Path) -> None:
    """An S1 envelope sitting in the S7 results folder must not appear in the
    S7 report and must not be treated as a corrupt/skipped file (it is valid
    JSON, just not S7's)."""
    from spikes._harness.envelope import make_envelope, write_envelope

    results = tmp_path / "raw"
    doc = make_envelope(
        spike="S1", topic="S1-network", link="wifi", params={}, cases=[], errors=[]
    )
    write_envelope(doc, results / "foreign.json")
    out = tmp_path / "r.md"
    proc = _run("report", "--results", str(results), "--out", str(out))
    assert proc.returncode == EXIT_OK, proc.stderr
    assert "skipped invalid result file" not in proc.stderr
    text = out.read_text(encoding="utf-8")
    assert "S1-network" not in text
    assert "no S7 result files found" in proc.stderr


def test_report_dir_with_only_corrupt_files_is_not_a_crash(tmp_path: Path) -> None:
    results = tmp_path / "raw"
    results.mkdir()
    (results / "a.json").write_bytes(b"\x00\x01\x02garbage")
    (results / "b.json").write_text("[]", encoding="utf-8")  # valid JSON, not a dict
    (results / "c.json").write_text("{}", encoding="utf-8")  # dict, missing keys
    out = tmp_path / "r.md"
    proc = _run("report", "--results", str(results), "--out", str(out))
    assert proc.returncode == EXIT_OK, proc.stderr
    assert proc.stderr.count("skipped invalid result file") == 3
    assert out.exists()


# ----------------------------------------------------------------- determinism
def test_report_output_is_byte_for_byte_identical_across_two_runs(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "raw"
    proc = _run(
        "run",
        "--quick",
        "--skip-gpu",
        "--reps",
        "5",
        "--warmup",
        "0",
        "--pause",
        "0",
        "--name",
        "L1",
        "--out-dir",
        str(out_dir),
    )
    assert proc.returncode in (EXIT_OK, EXIT_INCOMPLETE), proc.stderr
    out1, out2 = tmp_path / "r1.md", tmp_path / "r2.md"
    p1 = _run(
        "report",
        "--results",
        str(out_dir),
        "--out",
        str(out1),
        "--labels",
        "L1=L1",
        "--date",
        "2026-09-20",
    )
    p2 = _run(
        "report",
        "--results",
        str(out_dir),
        "--out",
        str(out2),
        "--labels",
        "L1=L1",
        "--date",
        "2026-09-20",
    )
    assert p1.returncode == p2.returncode == EXIT_OK
    assert out1.read_bytes() == out2.read_bytes()


# --------------------------------------------------------------- process leak
def test_no_leftover_child_process_after_budget_exhaustion(tmp_path: Path) -> None:
    out_dir = tmp_path / "raw"
    before = _benchmark_children()
    proc = _run(
        "run",
        "--quick",
        "--skip-gpu",
        "--reps",
        "1000",
        "--warmup",
        "0",
        "--pause",
        "0",
        "--timeout",
        "10",
        "--out-dir",
        str(out_dir),
        timeout=90,
    )
    assert proc.returncode == EXIT_INCOMPLETE, proc.stderr
    time.sleep(0.5)  # let any reaping settle
    after = _benchmark_children()
    leaked = after - before
    assert not leaked, f"benchmark children still running after timeout: {leaked}"


# ----------------------------------------------------------------- privacy
def test_real_quick_run_output_has_no_home_path_or_username(tmp_path: Path) -> None:
    out_dir = tmp_path / "raw"
    proc = _run("run", "--quick", "--skip-gpu", "--out-dir", str(out_dir))
    assert proc.returncode in (EXIT_OK, EXIT_INCOMPLETE), proc.stderr
    home = str(Path.home())
    user = getpass.getuser()
    files = list(out_dir.glob("*.json"))
    assert files
    text = files[0].read_text(encoding="utf-8")
    assert home not in text, f"home directory leaked into the S7 JSON: {home!r}"
    if user and len(user) > 2:  # skip trivially short usernames (false positives)
        # The hostname may contain the user name (CI: `runner` / `runnervm...`).
        text = text.replace(socket.gethostname(), "")
        assert user not in text, f"username leaked into the S7 JSON: {user!r}"
    assert home not in proc.stderr
    out = tmp_path / "r.md"
    rp = _run(
        "report", "--results", str(out_dir), "--out", str(out), "--labels", "L1=L1"
    )
    assert rp.returncode == EXIT_OK
    report_text = out.read_text(encoding="utf-8")
    assert home not in report_text
    if user and len(user) > 2:
        assert user not in report_text.replace(socket.gethostname(), "")


# --------------------------------------------------------- real run sanity
def test_real_quick_run_produces_sane_scores(tmp_path: Path) -> None:
    """Smoke run: values must exist, be positive, and every score n >= 5."""
    out_dir = tmp_path / "raw"
    proc = _run("run", "--quick", "--skip-gpu", "--out-dir", str(out_dir))
    assert proc.returncode in (EXIT_OK, EXIT_INCOMPLETE), proc.stderr
    files = list(out_dir.glob("*.json"))
    assert files
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    assert doc["extra"]["quick"] is True
    cases = doc["cases"]
    assert any(c.startswith("cpu-") for c in cases)
    assert "memory-bandwidth" in cases
    for case_id, case in cases.items():
        for metric, summary in case["summary"].items():
            if metric.endswith("_s") and not metric.endswith("_gb_s"):
                continue
            if summary["n"] == 0:
                continue  # unsupported op on this backend (e.g. stdlib fallback)
            assert summary["n"] >= 5, case_id
            assert summary["mean"] > 0, (case_id, metric)


@pytest.mark.timeout(180)
def test_real_non_quick_run_matches_s7_md_sanity_ranges(tmp_path: Path) -> None:
    """One real (non-quick) run, sanity-checked against the ranges documented
    in ``lab/spikes/S7.md`` §3. This machine has no torch/numpy (per the
    implementer note), so only the ``python_mflops`` / ``memory-bandwidth``
    ranges apply here; the GPU/GFLOPS ranges are the 🧑 lab-run's job."""
    out_dir = tmp_path / "raw"
    proc = _run("run", "--skip-gpu", "--out-dir", str(out_dir), timeout=170)
    assert proc.returncode in (EXIT_OK, EXIT_INCOMPLETE), proc.stderr
    files = list(out_dir.glob("*.json"))
    assert files
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    if doc["extra"]["capabilities"].get("torch") or doc["extra"]["capabilities"].get(
        "numpy"
    ):
        pytest.skip("torch/numpy present: not the fallback-only scenario assumed here")
    py1t = doc["cases"]["cpu-python-1t"]["summary"]["python_mflops"]
    assert py1t["n"] >= 5
    # S7.md: "python_mflops: 30-60 MFLOP/s per process". Generous margin either
    # side (different CPUs / interpreter builds) without being a no-op check.
    assert 10.0 <= py1t["mean"] <= 150.0, py1t
    mem = doc["cases"]["memory-bandwidth"]["summary"]["copy_gb_s"]
    assert mem["n"] >= 5
    # S7.md: "Memory bandwidth: 10-40 GB/s". Widened to catch gross formula
    # errors (off by 2^30 vs 1e9, off by the stream count) without being
    # fragile to one particular laptop's RAM speed.
    assert 1.0 <= mem["mean"] <= 200.0, mem
