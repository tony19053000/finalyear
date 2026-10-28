"""V1.0 G5 (D420): the demo scenarios' pure parts, no fabric needed.

The scenarios themselves run end to end in ``tests/cluster/test_v10_g5_demos.py``.
Here: the demo projects compute what the demo's own checks expect (run as the
real programs, in a subprocess), a decision's chosen mode is read correctly,
``state.json`` round-trips, a hook command never goes through a shell, the
usage check happens before anything touches the fabric, and every scenario of
D420 for a shipped mode exists - and none for Mode 2 / Mode 4.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

DEMO_DIR = Path(__file__).resolve().parents[2] / "scripts" / "demo"


@pytest.fixture(scope="module")
def kit() -> ModuleType:
    sys.path.insert(0, str(DEMO_DIR))
    import demo_kit

    return demo_kit


@pytest.fixture(scope="module")
def demo() -> ModuleType:
    sys.path.insert(0, str(DEMO_DIR))
    import demo

    return demo


def test_the_scenarios_are_d420s_shipped_modes_only(demo: ModuleType) -> None:
    assert sorted(demo.SCENARIOS) == [
        "01-local",
        "02-mode1-offload",
        "03-mode3-split",
        "04-planner-explains",
        "05-install-join",
    ]
    text = (DEMO_DIR / "demo.py").read_text(encoding="utf-8").lower()
    assert "remote_gpu" not in text and "combined_gpu" not in text
    readme = (DEMO_DIR / "README.md").read_text(encoding="utf-8")
    assert "deferred, designed, not built" in readme.lower()


def test_whole_job_project_gives_the_expected_bytes(
    kit: ModuleType, tmp_path: Path
) -> None:
    project = kit.write_job_project(tmp_path / "p", name="demo-x", rounds=5000)
    manifest = (project / "compute.yaml").read_text(encoding="utf-8")
    assert manifest.startswith("name: demo-x\n") and "objective" not in manifest
    subprocess.run(
        [sys.executable, "job.py", "5000"], cwd=project, check=True, capture_output=True
    )
    got = (project / "results" / "result.json").read_bytes().decode()
    assert got == kit.expected_job_result(5000)


def test_busy_job_holds_and_records_its_extra_rounds(
    kit: ModuleType, tmp_path: Path
) -> None:
    project = kit.write_job_project(
        tmp_path / "p", name="demo-b", rounds=10, hold_s=0.3, objective="min_network"
    )
    manifest = (project / "compute.yaml").read_text(encoding="utf-8")
    assert "objective: min_network" in manifest and '"--hold-s", "0.3"' in manifest
    subprocess.run(
        [sys.executable, "job.py", "10", "--hold-s", "0.3"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    result = json.loads(
        (project / "results" / "result.json").read_text(encoding="utf-8")
    )
    assert result["rounds"] == 10 and result["extra_rounds"] > 0


def test_split_pieces_concatenate_to_the_expected_lines(
    kit: ModuleType, tmp_path: Path
) -> None:
    project = kit.write_split_project(
        tmp_path / "s", name="demo-s", items=7, chunk=3, rounds=50
    )
    manifest = (project / "compute.yaml").read_text(encoding="utf-8")
    assert "over: {range: {stop: 7}}" in manifest and "chunk: {items: 3}" in manifest
    parts = []
    for i, items in enumerate(([0, 1, 2], [3, 4, 5], [6])):
        items_file = tmp_path / f"items{i}.json"
        items_file.write_text(json.dumps(items), encoding="utf-8")
        out = tmp_path / f"out{i}"
        subprocess.run(
            [sys.executable, "piece.py", str(items_file), str(out), "50"],
            cwd=project,
            check=True,
            capture_output=True,
        )
        parts.append((out / "hashes.txt").read_text(encoding="utf-8"))
    assert "".join(parts).splitlines() == kit.expected_split_lines(7, 50)


def test_chosen_mode_reads_the_rank_one_row_of_the_chosen_node(kit: ModuleType) -> None:
    decision = {
        "chosen_node_id": "node_b",
        "candidates": [
            {"node_id": "node_a", "mode": "local", "rank": 2},
            {"node_id": "node_b", "mode": "offload", "rank": 1},
            {"node_id": "node_b", "mode": "split", "rank": None},
        ],
    }
    assert kit.chosen_mode(decision) == "mode1"
    decision["candidates"][2]["rank"] = 1
    decision["candidates"][1]["rank"] = 3
    assert kit.chosen_mode(decision) == "mode3"
    assert kit.chosen_mode(None) is None
    assert kit.chosen_mode({"chosen_node_id": "x", "candidates": []}) is None


def test_state_round_trips_and_a_broken_file_is_a_fresh_state(
    kit: ModuleType, tmp_path: Path
) -> None:
    path = tmp_path / "state.json"
    state = kit.DemoState(path)
    state.add_job("job_1")
    state.invites.append("inv_1")
    state.killed.append("node_9")
    state.save()
    again = kit.DemoState.load(path)
    assert (again.jobs, again.invites, again.killed) == (
        ["job_1"],
        ["inv_1"],
        ["node_9"],
    )
    path.write_text("{not json", encoding="utf-8")
    assert kit.DemoState.load(path).jobs == []


def test_hooks_fill_placeholders_and_never_use_a_shell(
    kit: ModuleType, tmp_path: Path
) -> None:
    marker = tmp_path / "hit"
    code = (
        "import sys, pathlib; "
        "pathlib.Path(sys.argv[1]).write_text(sys.argv[2] + sys.stdin.read())"
    )
    template = f'"{sys.executable}" -c "{code}" "{marker}" "{{node_id}};echo pwned"'
    with open(tmp_path / "log", "w", encoding="utf-8") as out:
        show = kit.Show(dry_run=True, out=out)
        kit.run_hook(show, template, stdin="CODE", node_id="node_x")
        with pytest.raises(kit.DemoError, match="exited 3"):
            kit.run_hook(show, f'"{sys.executable}" -c "raise SystemExit(3)"')
        with pytest.raises(kit.DemoError, match="failed"):
            kit.run_hook(show, "/nonexistent/hook {node_id}", node_id="n")
        with pytest.raises(kit.DemoError, match="bad hook command"):
            kit.run_hook(show, "echo {unknown}", node_id="n")
    # the ";echo pwned" reached the program as text: no shell ran it
    assert marker.read_text(encoding="utf-8") == "node_x;echo pwnedCODE"
    # the join code (stdin) is never printed
    assert "CODE" not in (tmp_path / "log").read_text(encoding="utf-8")


def test_expectations_are_recorded_and_fail_the_run(
    kit: ModuleType, tmp_path: Path
) -> None:
    with open(tmp_path / "log", "w", encoding="utf-8") as out:
        show = kit.Show(dry_run=True, out=out)
        show.expect(True, "fine")
        show.expect(False, "the job succeeded", "failed")
    assert show.failures == ["the job succeeded - got failed"]
    text = (tmp_path / "log").read_text(encoding="utf-8")
    assert (
        "EXPECTED   fine" in text
        and "UNEXPECTED the job succeeded - got failed" in text
    )


def test_split_dry_run_without_kill_cmd_is_a_usage_error_before_any_call(
    demo: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --compute points at a program that would fail loudly if it were ever run
    code = demo.main(
        [
            "03-mode3-split",
            "--dry-run",
            "--work",
            str(tmp_path),
            "--compute",
            "/nonexistent",
        ]
    )
    assert code == 2
    assert "needs --kill-cmd" in capsys.readouterr().out


def test_lane_to_kill_prefers_a_busy_remote_lane(demo: ModuleType) -> None:
    progress = {
        "lanes": [
            {"node_id": "node_local", "live": True, "current": 3},
            {"node_id": "node_a", "live": True, "current": None},
            {"node_id": "node_b", "live": True, "current": 4},
            {"node_id": "node_c", "live": False, "current": 5},
        ]
    }
    assert demo._lane_to_kill(progress, "node_local") == "node_b"
    progress["lanes"][2]["live"] = False
    assert demo._lane_to_kill(progress, "node_local") == "node_a"
    assert demo._lane_to_kill({"lanes": []}, None) is None
