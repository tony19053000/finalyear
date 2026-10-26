"""V1.0 G0 (D406/D408, ADR-0023): the verdict rules of ``scripts/release_check.py``.

Pure parts only: how an auto row is judged from per-item pytest outcomes, how a
lab row is judged from evidence files (release build, SHA, the ``affected_by``
redo rule), the exit code, ``record-lab`` -> ingestion, and the outcomes plugin.
The real end-to-end run (``release_check.py --only auto``) is the G0 acceptance
step, not a unit test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest

REPO: Final = Path(__file__).resolve().parents[3]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import release_check as rc  # noqa: E402
import release_manifest as rm  # noqa: E402

SHA_A: Final = "a" * 40
SHA_B: Final = "b" * 40


def _auto(tests: tuple[str, ...] = ("tests/unit/a.py",), commands: Any = ()) -> rm.Row:
    return rm.Row(
        id="V0.1-c1",
        version="V0.1",
        criterion=1,
        text="t",
        kind="auto",
        tests=tests,
        commands=commands,
    )


def _lab() -> rm.Row:
    return rm.Row(
        id="V0.3-c2",
        version="V0.3",
        criterion=2,
        text="t",
        kind="lab",
        section="L2",
        analyzer="lab/fabric_check.py",
        affected_by=("src/meshforge/agent/session/*",),
    )


def _o(outcome: str, reason: str = "") -> dict[str, str]:
    return {"outcome": outcome, "reason": reason}


# ------------------------------------------------------------------ auto


def test_auto_pass_needs_a_passed_test_per_node_id() -> None:
    outcomes = {
        "tests/unit/a.py::t1": _o("passed"),
        "tests/unit/a.py::t2": _o("skipped", "gpu"),
    }
    res = rc.judge_auto(_auto(), outcomes, {}, {})
    assert (res.status, res.detail) == ("PASS", "1p/1s")


def test_all_skipped_is_missing_with_the_reason_never_a_pass() -> None:
    outcomes = {"tests/unit/a.py::t1": _o("skipped", "needs docker")}
    res = rc.judge_auto(_auto(), outcomes, {}, {})
    assert res.status == "MISSING"
    assert "needs docker" in res.detail


def test_one_failure_fails_the_row_even_with_passes() -> None:
    tests = ("tests/unit/a.py", "tests/unit/b.py::t")
    outcomes = {
        "tests/unit/a.py::t1": _o("passed"),
        "tests/unit/b.py::t": _o("error", "setup"),
    }
    res = rc.judge_auto(_auto(tests), outcomes, {}, {})
    assert res.status == "FAIL"
    assert "tests/unit/b.py::t" in res.detail


def test_a_node_id_with_no_item_is_a_failure() -> None:
    res = rc.judge_auto(_auto(("tests/unit/gone.py",)), {}, {}, {})
    assert (res.status, res.detail) == ("FAIL", "tests/unit/gone.py: no test ran")


def test_a_crashed_pytest_fails_its_rows_despite_green_items() -> None:
    outcomes = {"tests/unit/a.py::t1": _o("passed")}
    res = rc.judge_auto(_auto(), outcomes, {"tests/unit/a.py": 139}, {})
    assert res.status == "FAIL"
    assert "pytest exited 139" in res.detail


def test_commands_count_by_exit_code() -> None:
    cmd = ("scripts/gen_protos.py", "--check")
    outcomes = {"tests/unit/a.py::t1": _o("passed")}
    ok = rc.judge_auto(_auto(commands=(cmd,)), outcomes, {}, {cmd: (0, "")})
    bad = rc.judge_auto(_auto(commands=(cmd,)), outcomes, {}, {cmd: (1, "drift: x.py")})
    assert ok.status == "PASS"
    assert bad.status == "FAIL"
    assert "drift: x.py" in bad.detail


# ------------------------------------------------------------------ lab


def _entry(**kw: str) -> rc.LabEntry:
    base = {
        "row": "V0.3-c2",
        "verdict": "PASS",
        "sha": SHA_A,
        "package_version": "1.0.0-rc.1",
        "recorded_at": "2026-11-02T10:00:00+00:00",
        "detail": "OFFLINE 6.1 s",
        "source": "lab/reports/raw/x.json",
    }
    return rc.LabEntry(**{**base, **kw})


@pytest.fixture
def git(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake git: known SHAs and the files changed between them."""
    state: dict[str, Any] = {"known": {SHA_A, SHA_B}, "changed": []}
    monkeypatch.setattr(
        rc, "resolve_sha", lambda ref: ref if ref in state["known"] else None
    )
    monkeypatch.setattr(rc, "changed_files", lambda old, new: list(state["changed"]))
    return state


def test_no_evidence_is_missing(git: dict[str, Any]) -> None:
    res = rc.judge_lab(_lab(), [], SHA_A)
    assert (res.status, res.detail) == ("MISSING", "no evidence yet (lab section L2)")


def test_pass_at_the_target_sha(git: dict[str, Any]) -> None:
    assert rc.judge_lab(_lab(), [_entry()], SHA_A).status == "PASS"


@pytest.mark.parametrize("version", ["dev", "DEV", "unknown", "none"])
def test_evidence_from_a_dev_checkout_never_counts(
    git: dict[str, Any], version: str
) -> None:
    res = rc.judge_lab(_lab(), [_entry(package_version=version)], SHA_A)
    assert res.status == "MISSING"
    assert "D406" in res.detail


def test_a_fail_verdict_fails(git: dict[str, Any]) -> None:
    res = rc.judge_lab(
        _lab(), [_entry(verdict="FAIL", detail="OFFLINE after 12 s")], SHA_A
    )
    assert res.status == "FAIL"
    assert "OFFLINE after 12 s" in res.detail


def test_the_newest_entry_wins_whatever_the_file_order(git: dict[str, Any]) -> None:
    old_fail = _entry(verdict="FAIL", recorded_at="2026-11-01T10:00:00+00:00")
    new_pass = _entry(recorded_at="2026-11-02T10:00:00")  # naive = UTC
    assert rc.judge_lab(_lab(), [new_pass, old_fail], SHA_A).status == "PASS"
    assert rc.judge_lab(_lab(), [old_fail, new_pass], SHA_A).status == "PASS"


def test_older_sha_stays_valid_when_no_affected_file_changed(
    git: dict[str, Any],
) -> None:
    git["changed"] = ["docs/STATUS.md", "src/meshforge/controller/api/routes.py"]
    res = rc.judge_lab(_lab(), [_entry(sha=SHA_A)], SHA_B)
    assert res.status == "PASS"
    assert "no affected file changed since" in res.detail


def test_older_sha_is_stale_when_an_affected_file_changed(git: dict[str, Any]) -> None:
    git["changed"] = ["docs/STATUS.md", "src/meshforge/agent/session/client.py"]
    res = rc.judge_lab(_lab(), [_entry(sha=SHA_A)], SHA_B)
    assert res.status == "MISSING"
    assert "stale" in res.detail
    assert "src/meshforge/agent/session/client.py" in res.detail


def test_an_unknown_evidence_sha_is_missing(git: dict[str, Any]) -> None:
    res = rc.judge_lab(_lab(), [_entry(sha="c" * 40)], SHA_A)
    assert res.status == "MISSING"
    assert "unknown to git" in res.detail


def test_affected_globs_cross_directories() -> None:
    files = ["src/meshforge/agent/session/deep/x.py", "src/meshforge/agent/runtime.py"]
    assert rc.affected(files, ["src/meshforge/agent/session/*"]) == [files[0]]


# ------------------------------------------------------------------ evidence files


def _evidence_file(directory: Path, name: str, payload: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def test_load_lab_evidence_reads_only_schema_files_and_warns_on_bad_entries(
    tmp_path: Path,
) -> None:
    good = {
        "row": "V0.3-c2",
        "verdict": "PASS",
        "sha": SHA_A,
        "package_version": "1.0.0-rc.1",
        "recorded_at": "2026-11-02T10:00:00+00:00",
    }
    _evidence_file(tmp_path, "other.json", {"anything": 1})
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    _evidence_file(
        tmp_path / "day1",
        "ev.json",
        {
            "schema": rc.EVIDENCE_SCHEMA,
            "results": [
                good,
                {**good, "verdict": "MAYBE"},
                {**good, "recorded_at": "yesterday"},
                {k: v for k, v in good.items() if k != "sha"},
                "nope",
            ],
        },
    )
    _evidence_file(tmp_path, "bad.json", {"schema": rc.EVIDENCE_SCHEMA, "results": {}})
    entries, warnings = rc.load_lab_evidence(tmp_path)
    assert list(entries) == ["V0.3-c2"]
    assert len(entries["V0.3-c2"]) == 1
    assert len(warnings) == 5
    assert any("verdict must be one of" in w for w in warnings)
    assert any("'results' must be a list" in w for w in warnings)


def test_a_missing_lab_dir_is_no_evidence(tmp_path: Path) -> None:
    assert rc.load_lab_evidence(tmp_path / "absent") == ({}, [])


def test_record_lab_writes_a_file_the_check_reads_back(tmp_path: Path) -> None:
    out = tmp_path / "acceptance"
    head = rc.resolve_sha("HEAD")
    assert head is not None
    code = rc.main(
        [
            "record-lab",
            "V0.3-c2",
            "--verdict",
            "PASS",
            "--package-version",
            "1.0.0-rc.1",
            "--detail",
            "OFFLINE 6.1 s, ONLINE 3.4 s",
            "--out-dir",
            str(out),
        ]
    )
    assert code == 0
    entries, warnings = rc.load_lab_evidence(out)
    assert warnings == []
    (entry,) = entries["V0.3-c2"]
    assert (entry.sha, entry.verdict, entry.package_version) == (
        head,
        "PASS",
        "1.0.0-rc.1",
    )
    row = rm.load_manifest().by_id()["V0.3-c2"]
    assert rc.judge_lab(row, [entry], head).status == "PASS"


@pytest.mark.parametrize(
    "args",
    [
        [
            "V0.6-c2",
            "--verdict",
            "PASS",
            "--package-version",
            "1.0.0-rc.1",
        ],  # an auto row
        ["V0.99-c1", "--verdict", "PASS", "--package-version", "1.0.0-rc.1"],
        ["V0.3-c2", "--verdict", "PASS", "--package-version", "dev"],
        [
            "V0.3-c2",
            "--verdict",
            "PASS",
            "--package-version",
            "1.0.0-rc.1",
            "--sha",
            "zz",
        ],
        [
            "V0.3-c2",
            "--verdict",
            "PASS",
            "--package-version",
            "1",
            "--evidence",
            "/no/such",
        ],
    ],
)
def test_record_lab_refuses_what_could_never_count(
    tmp_path: Path, args: list[str]
) -> None:
    assert rc.main(["record-lab", *args, "--out-dir", str(tmp_path)]) == 2
    assert not any(tmp_path.iterdir())


# ------------------------------------------------------------------ exit + report


def _r(kind: str, status: str) -> rc.Result:
    return rc.Result(f"{kind}-{status}", kind, status, "")


def test_exit_code_counts_only_the_selected_kind() -> None:
    results = [_r("auto", "PASS"), _r("lab", "MISSING"), _r("waiver", "WAIVED")]
    assert rc.exit_code(results, "auto") == 0
    assert rc.exit_code(results, "lab") == 1
    assert rc.exit_code(results, None) == 1
    assert rc.exit_code([_r("auto", "PASS"), _r("waiver", "WAIVED")], None) == 0
    assert rc.exit_code([_r("auto", "FAIL"), _r("lab", "PASS")], "lab") == 0


def test_markdown_escapes_pipes_and_lists_every_row() -> None:
    results = [rc.Result("V0.1-c1", "auto", "FAIL", "a | b\nc")]
    md = rc.render_markdown({"SHA": SHA_A}, results)
    assert "| V0.1-c1 | auto | **FAIL** | a \\| b c |" in md
    assert "**Totals:** FAIL 1" in md


def test_an_invalid_manifest_or_unknown_row_is_a_usage_error(tmp_path: Path) -> None:
    bad = tmp_path / "m.yaml"
    bad.write_text("[]", encoding="utf-8")
    out = str(tmp_path / "r")
    assert rc.main(["--manifest", str(bad), "--out", out]) == 2
    assert rc.main(["--rows", "V0.99-c1", "--out", out]) == 2
    assert rc.main(["--sha", "no-such-ref", "--out", out]) == 2


def test_only_lab_writes_both_reports_and_never_runs_tests(tmp_path: Path) -> None:
    out = tmp_path / "report"
    code = rc.main(
        ["--only", "lab", "--lab-dir", str(tmp_path / "none"), "--out", str(out)]
    )
    assert code == 1  # no lab evidence exists yet
    data = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    statuses = {r["kind"]: set() for r in data["results"]}
    for r in data["results"]:
        statuses[r["kind"]].add(r["status"])
    assert statuses == {"auto": {"NOT_RUN"}, "lab": {"MISSING"}, "waiver": {"WAIVED"}}
    assert "release acceptance report" in out.with_suffix(".md").read_text(
        encoding="utf-8"
    )


# ------------------------------------------------------------------ plugin


def test_the_outcomes_plugin_folds_phases(tmp_path: Path) -> None:
    test_file = tmp_path / "test_sample.py"
    test_file.write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def broken():\n"
        "    yield\n"
        "    raise RuntimeError('teardown')\n"
        "def test_ok():\n    pass\n"
        "def test_bad():\n    assert False\n"
        "def test_skip():\n    pytest.skip('no gpu')\n"
        "@pytest.mark.xfail(reason='known', strict=True)\n"
        "def test_xf():\n    assert False\n"
        "def test_teardown(broken):\n    pass\n",
        encoding="utf-8",
    )
    out = tmp_path / "outcomes.json"
    ini = tmp_path / "pytest.ini"
    ini.write_text("[pytest]\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["PYTHONPATH"] = str(REPO / "scripts")
    env[rc.OUTCOMES_ENV] = str(out)
    subprocess.run(  # our own interpreter, fixed argv, an isolated rootdir
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            rc.PLUGIN,
            "-p",
            "no:cacheprovider",
            "-c",
            str(ini),
            "--rootdir",
            str(tmp_path),
            str(test_file),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
    )
    got = {
        k.split("::")[1]: v["outcome"] for k, v in json.loads(out.read_text()).items()
    }
    assert got == {
        "test_ok": "passed",
        "test_bad": "failed",
        "test_skip": "skipped",
        "test_xf": "skipped",
        "test_teardown": "error",
    }
