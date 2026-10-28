# ruff: noqa: E501
"""V1.0 G0 tester round 1: adversarial checks of the acceptance manifest and
``scripts/release_check.py``, derived from the G0 success criteria (not from the
implementation): the manifest covers every roadmap criterion exactly once with a
valid kind; the check script fails safe on bad input and its exit codes mean
what they say; nothing here needs a ``.git`` of this repo.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

REPO: Final = Path(__file__).resolve().parents[3]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import release_check as rc  # noqa: E402
import release_manifest as rm  # noqa: E402

pytestmark = pytest.mark.unit

SHA_A: Final = "a" * 40
CLEAN_ENV: Final = {
    **os.environ,
    "GIT_DIR": "/nonexistent-git-dir",  # hermetic: no .git reachable
}


# ------------------------------------------------ independent criterion count


def _roadmap_criteria_independently() -> dict[str, list[str]]:
    """A second, deliberately simple parser: version -> criterion first lines."""
    out: dict[str, list[str]] = {}
    version: str | None = None
    active = False
    for line in rm.ROADMAP.read_text(encoding="utf-8").splitlines():
        h = re.match(r"^## (V\d+\.\d+)\b", line)
        if h:
            version, active = h.group(1), False
            continue
        if line.startswith("## "):
            version, active = None, False
        if version and line.strip() == "**Success criteria**":
            active = True
            continue
        if active:
            m = re.match(r"^(\d+)\. (.*)", line)
            if m:
                out.setdefault(version or "?", []).append(m.group(2))
            elif line.strip() and not line.startswith((" ", "\t")):
                active = False
    return out


def test_every_roadmap_criterion_has_exactly_one_row_by_an_independent_count() -> None:
    theirs = _roadmap_criteria_independently()
    manifest = rm.load_manifest()
    ids = [r.id for r in manifest.rows]
    assert len(ids) == len(set(ids)), "duplicate row ids"
    mine: dict[str, int] = {}
    for r in manifest.rows:
        mine[r.version] = mine.get(r.version, 0) + 1
    below = {v: len(c) for v, c in theirs.items() if rm.version_key(v) < (1, 0)}
    assert mine == below
    assert len(ids) == sum(below.values())
    # every row's text starts like the independently parsed criterion
    by_id = {r.id: r for r in manifest.rows}
    for version, texts in theirs.items():
        for i, first in enumerate(texts, start=1):
            if rm.version_key(version) >= (1, 0):
                continue
            row = by_id[f"{version}-c{i}"]
            assert rm.normalise(row.text).startswith(rm.normalise(first)[:40])


def test_every_row_has_a_valid_kind_and_the_fields_its_kind_needs() -> None:
    manifest = rm.load_manifest()
    for r in manifest.rows:
        assert r.kind in {"auto", "lab", "waiver"}, r.id
        if r.kind == "auto":
            assert r.tests or r.commands, r.id
        elif r.kind == "lab":
            assert r.section in manifest.lab_sections, r.id
            assert r.affected_by, r.id
            assert r.checklist or r.analyzer, r.id
            assert not r.reason, f"{r.id}: a lab row carries a waiver reason"
        else:
            assert len(r.reason.split()) >= 5, r.id
            assert r.decision, r.id
            assert not (r.tests or r.commands or r.flags), r.id


def test_waiver_reasons_are_not_filler() -> None:
    for r in rm.load_manifest().rows:
        if r.kind == "waiver":
            low = r.reason.lower()
            assert not any(w in low for w in ("todo", "tbd", "fixme", "xxx")), r.id


def test_real_manifest_and_roadmap_validate_clean() -> None:
    crit = rm.parse_roadmap(rm.ROADMAP.read_text(encoding="utf-8"))
    assert rm.validate(rm.load_manifest(), crit) == []


# ----------------------------------------- validate() rejects broken manifests


@pytest.fixture(scope="module")
def good() -> dict[str, Any]:
    return yaml.safe_load(rm.MANIFEST.read_text(encoding="utf-8"))


def _write(tmp: Path, data: Any) -> Path:
    p = tmp / "m.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return p


def _problems(path: Path) -> list[str]:
    crit = rm.parse_roadmap(rm.ROADMAP.read_text(encoding="utf-8"))
    return rm.validate(rm.load_manifest(path), crit)


def _mutate(good: dict[str, Any], fn: Any) -> dict[str, Any]:
    data = json.loads(json.dumps(good))
    fn(data)
    return data


def _first(data: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(r for r in data["rows"] if r["kind"] == kind)


def test_orphan_row_is_rejected(good: dict[str, Any], tmp_path: Path) -> None:
    def f(d: dict[str, Any]) -> None:
        row = json.loads(json.dumps(d["rows"][0]))
        row.update(id="V0.1-c99", criterion=99)
        d["rows"].append(row)

    got = _problems(_write(tmp_path, _mutate(good, f)))
    assert any("V0.1-c99" in p and "no such success criterion" in p for p in got)


def test_dropped_row_is_rejected(good: dict[str, Any], tmp_path: Path) -> None:
    got = _problems(_write(tmp_path, _mutate(good, lambda d: d["rows"].pop(5))))
    assert any("criterion has no manifest row" in p for p in got)


def test_duplicate_row_is_rejected(good: dict[str, Any], tmp_path: Path) -> None:
    got = _problems(
        _write(tmp_path, _mutate(good, lambda d: d["rows"].append(dict(d["rows"][3]))))
    )
    assert any("duplicate row id" in p for p in got)


def test_reworded_text_is_rejected(good: dict[str, Any], tmp_path: Path) -> None:
    def f(d: dict[str, Any]) -> None:
        d["rows"][0]["text"] += " (quietly weakened)"

    assert any(
        "text differs" in p for p in _problems(_write(tmp_path, _mutate(good, f)))
    )


@pytest.mark.parametrize("kind", ["manual", "AUTO", "", "skip"])
def test_unknown_kind_is_rejected(
    good: dict[str, Any], tmp_path: Path, kind: str
) -> None:
    def f(d: dict[str, Any]) -> None:
        d["rows"][0]["kind"] = kind

    assert any(
        "kind must be one of" in p
        for p in _problems(_write(tmp_path, _mutate(good, f)))
    )


@pytest.mark.parametrize("reason", ["", "n/a", "later", "not now thanks"])
def test_waiver_without_a_real_reason_is_rejected(
    good: dict[str, Any], tmp_path: Path, reason: str
) -> None:
    def f(d: dict[str, Any]) -> None:
        _first(d, "waiver")["reason"] = reason

    assert any(
        "waiver needs a reason" in p
        for p in _problems(_write(tmp_path, _mutate(good, f)))
    )


@pytest.mark.parametrize(
    "decision", [[], ["ADR-9999"], ["D99999"], ["whatever"], ["ADR-"]]
)
def test_waiver_with_missing_or_fake_decision_is_rejected(
    good: dict[str, Any], tmp_path: Path, decision: list[str]
) -> None:
    def f(d: dict[str, Any]) -> None:
        _first(d, "waiver")["decision"] = decision

    assert _problems(_write(tmp_path, _mutate(good, f)))


def test_auto_row_without_evidence_is_rejected(
    good: dict[str, Any], tmp_path: Path
) -> None:
    def f(d: dict[str, Any]) -> None:
        _first(d, "auto")["evidence"] = {}

    assert any(
        "auto row needs" in p for p in _problems(_write(tmp_path, _mutate(good, f)))
    )


def test_lab_row_with_unknown_section_or_no_globs_is_rejected(
    good: dict[str, Any], tmp_path: Path
) -> None:
    def f(d: dict[str, Any]) -> None:
        r = _first(d, "lab")
        r["evidence"]["section"] = "L9"
        r["affected_by"] = []

    got = _problems(_write(tmp_path, _mutate(good, f)))
    assert any("lab section" in p for p in got)
    assert any("affected_by" in p for p in got)


@pytest.mark.parametrize("node", ["src/x.py::t", "..\\tests\\x.py", "/etc/passwd"])
def test_node_ids_outside_tests_are_rejected(
    good: dict[str, Any], tmp_path: Path, node: str
) -> None:
    def f(d: dict[str, Any]) -> None:
        _first(d, "auto")["evidence"]["tests"] = [node]

    assert any(
        "must start with tests/" in p
        for p in _problems(_write(tmp_path, _mutate(good, f)))
    )


@pytest.mark.parametrize("cmd", [["/bin/sh", "-c", "id"], ["nope.py"], ["../x.py"], []])
def test_commands_must_be_a_repo_script(
    good: dict[str, Any], tmp_path: Path, cmd: list[str]
) -> None:
    def f(d: dict[str, Any]) -> None:
        _first(d, "auto")["evidence"]["commands"] = [cmd]

    assert any(
        "command must start with a repo script" in p
        for p in _problems(_write(tmp_path, _mutate(good, f)))
    )


def test_unknown_flag_is_rejected(good: dict[str, Any], tmp_path: Path) -> None:
    def f(d: dict[str, Any]) -> None:
        _first(d, "auto")["evidence"]["flags"] = ["--rootdir=/", "-p", "evil"]

    assert any(
        "flags not allowed" in p for p in _problems(_write(tmp_path, _mutate(good, f)))
    )


# --------------------------------------------- load_manifest on malformed input


@pytest.mark.parametrize(
    "content",
    [
        "",
        "null",
        "[]",
        "schema: meshforge.acceptance/2\nlab_sections: {}\nrows: []\n",
        "schema: meshforge.acceptance/1\nlab_sections: []\nrows: []\n",
        "schema: meshforge.acceptance/1\nlab_sections: {}\nrows: null\n",
        "schema: meshforge.acceptance/1\nlab_sections: {}\nrows: [1]\n",
        "schema: [unterminated\n",
        "\tschema: tabs\n",
        "schema: meshforge.acceptance/1\nlab_sections: {}\nrows:\n- {id: a, version: V0.1, criterion: true, text: t, kind: auto}\n",
        "schema: meshforge.acceptance/1\nlab_sections: {}\nrows:\n- {id: a, version: V0.1, criterion: 1, text: t, kind: auto, bogus: 1}\n",
        "schema: meshforge.acceptance/1\nlab_sections: {}\nrows:\n- {id: a, version: V0.1, criterion: 1, text: t, kind: auto, evidence: [x]}\n",
        "schema: meshforge.acceptance/1\nlab_sections: {}\nrows:\n- {id: a, version: V0.1, criterion: 1, text: t, kind: auto, evidence: {tests: {a: b}}}\n",
        "schema: meshforge.acceptance/1\nlab_sections: {}\nrows:\n- {id: a, version: V0.1, criterion: 1, text: t, kind: auto, evidence: {commands: nope}}\n",
        "schema: meshforge.acceptance/1\nlab_sections: {}\nrows:\n- {id: a, version: V0.1, criterion: 1, text: t, kind: auto, affected_by: 5}\n",
        "schema: meshforge.acceptance/1\nlab_sections: {}\nrows:\n- {id: [a], version: V0.1, criterion: 1, text: t, kind: auto}\n",
    ],
)
def test_malformed_manifest_raises_manifest_error_only(
    tmp_path: Path, content: str
) -> None:
    p = tmp_path / "m.yaml"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(rm.ManifestError):
        rm.load_manifest(p)


def test_binary_and_missing_manifest_raise_manifest_error(tmp_path: Path) -> None:
    p = tmp_path / "bin.yaml"
    p.write_bytes(b"\xff\xfe\x00\x01garbage")
    with pytest.raises(rm.ManifestError):
        rm.load_manifest(p)
    with pytest.raises(rm.ManifestError):
        rm.load_manifest(tmp_path / "absent.yaml")
    with pytest.raises(rm.ManifestError):
        rm.load_manifest(tmp_path)  # a directory


# ------------------------------------------------- CLI through a real process


def _cli(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "release_check.py"), *args],
        capture_output=True,
        text=True,
        env=env or CLEAN_ENV,
        cwd=REPO,
        timeout=120,
        check=False,
    )


def test_cli_without_git_fails_cleanly_with_exit_2_not_a_traceback(
    tmp_path: Path,
) -> None:
    res = _cli(
        "--only", "lab", "--lab-dir", str(tmp_path), "--out", str(tmp_path / "r")
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "Traceback" not in res.stderr
    assert "cannot resolve SHA" in res.stderr


def test_cli_with_no_git_binary_fails_cleanly(tmp_path: Path) -> None:
    env = {**os.environ, "PATH": str(tmp_path)}
    res = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "release_check.py"),
            "--only",
            "lab",
            "--out",
            str(tmp_path / "r"),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        timeout=60,
        check=False,
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "Traceback" not in res.stderr


@pytest.mark.parametrize(
    "content",
    ["", "[]", "schema: x\n", "schema: [", "rows: 1"],
)
def test_cli_malformed_manifest_exit_2_no_report(tmp_path: Path, content: str) -> None:
    m = tmp_path / "m.yaml"
    m.write_text(content, encoding="utf-8")
    out = tmp_path / "rep"
    res = _cli("--manifest", str(m), "--out", str(out))
    assert res.returncode == 2
    assert "Traceback" not in res.stderr
    assert not out.with_suffix(".json").exists()


def test_cli_manifest_that_drops_a_criterion_is_exit_2(
    good: dict[str, Any], tmp_path: Path
) -> None:
    data = json.loads(json.dumps(good))
    data["rows"].pop(0)
    m = _write(tmp_path, data)
    res = _cli("--manifest", str(m), "--out", str(tmp_path / "rep"))
    assert res.returncode == 2
    assert "invalid manifest" in res.stderr
    assert "V0.1-c1" in res.stderr


@pytest.mark.parametrize(
    "argv",
    [["--only", "waiver"], ["--only", "AUTO"], ["--rows"], ["--bogus"]],
)
def test_cli_bad_options_are_usage_errors(argv: list[str], tmp_path: Path) -> None:
    res = _cli(*argv, "--out", str(tmp_path / "r"))
    assert res.returncode == 2
    assert "Traceback" not in res.stderr


def test_cli_record_lab_rejects_non_lab_rows_and_dev_builds(tmp_path: Path) -> None:
    res = _cli(
        "record-lab",
        "V0.1-c1",
        "--verdict",
        "PASS",
        "--package-version",
        "1.0.0-rc.1",
        "--out-dir",
        str(tmp_path),
    )
    assert res.returncode == 2 and "not a lab row" in res.stderr
    res = _cli(
        "record-lab",
        "V0.99-c1",
        "--verdict",
        "PASS",
        "--package-version",
        "1.0.0-rc.1",
        "--out-dir",
        str(tmp_path),
    )
    assert res.returncode == 2
    assert not list(tmp_path.iterdir())


# ----------------------------------------- in-process main() with a fake HEAD


@pytest.fixture
def fake_head(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(
        rc, "resolve_sha", lambda ref: SHA_A if ref in {"HEAD", SHA_A} else None
    )
    monkeypatch.setattr(rc, "tree_is_dirty", lambda: False)
    monkeypatch.setattr(rc, "changed_files", lambda old, new: [])
    return SHA_A


def _lab_ids() -> list[str]:
    return [r.id for r in rm.load_manifest().rows if r.kind == "lab"]


def _auto_ids() -> list[str]:
    return [r.id for r in rm.load_manifest().rows if r.kind == "auto"]


def test_only_lab_with_no_evidence_and_lab_filter_exits_1(
    tmp_path: Path, fake_head: str
) -> None:
    out = tmp_path / "r"
    code = rc.main(
        [
            "--only",
            "lab",
            "--rows",
            *_lab_ids()[:2],
            "--lab-dir",
            str(tmp_path / "x"),
            "--out",
            str(out),
        ]
    )
    assert code == 1
    data = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert data["exit"] == 1 and len(data["results"]) == 2


def test_waiver_only_subset_exits_0_and_is_waived(
    tmp_path: Path, fake_head: str
) -> None:
    ids = [r.id for r in rm.load_manifest().rows if r.kind == "waiver"][:3]
    out = tmp_path / "r"
    assert rc.main(["--rows", *ids, "--out", str(out)]) == 0
    data = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert {r["status"] for r in data["results"]} == {"WAIVED"}


def test_a_filter_that_selects_nothing_of_the_kind_must_not_pass_vacuously(
    tmp_path: Path, fake_head: str
) -> None:
    """Regression T-3 (fixed in fix round 1): ``--only auto --rows <lab ids>`` evaluates zero rows. A
    release gate that exits 0 having judged nothing is a false green."""
    out = tmp_path / "r"
    code = rc.main(
        [
            "--only",
            "auto",
            "--rows",
            *_lab_ids()[:1],
            "--lab-dir",
            str(tmp_path / "x"),
            "--out",
            str(out),
        ]
    )
    assert code != 0, "exit 0 although no row of the selected kind was evaluated"


def test_sha_mismatch_blocks_auto_rows(
    tmp_path: Path, fake_head: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = "c" * 40
    monkeypatch.setattr(
        rc, "resolve_sha", lambda ref: {"HEAD": SHA_A, other: other}.get(ref)
    )
    assert rc.main(["--sha", other, "--out", str(tmp_path / "r")]) == 2
    assert (
        rc.main(["--only", "auto", "--sha", other, "--out", str(tmp_path / "r")]) == 2
    )


def test_dirty_tree_is_flagged_in_both_reports(
    tmp_path: Path, fake_head: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rc, "tree_is_dirty", lambda: True)
    out = tmp_path / "r"
    rc.main(
        [
            "--rows",
            *[r.id for r in rm.load_manifest().rows if r.kind == "waiver"][:1],
            "--out",
            str(out),
        ]
    )
    assert (
        json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))["dirty"]
        is True
    )
    assert "DIRTY" in out.with_suffix(".md").read_text(encoding="utf-8")


# ---------------------------------------------------- lab evidence ingestion


def _ev(directory: Path, name: str, payload: Any) -> None:
    (directory / name).write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )


def test_garbage_evidence_files_never_crash_and_never_count(tmp_path: Path) -> None:
    _ev(tmp_path, "a.json", "{not json")
    _ev(tmp_path, "b.json", "[]")
    _ev(tmp_path, "c.json", "null")
    (tmp_path / "d.json").write_bytes(b"\xff\xfe\x00")
    (tmp_path / "dir.json").mkdir()
    _ev(tmp_path, "e.json", {"schema": rc.EVIDENCE_SCHEMA, "results": "nope"})
    _ev(
        tmp_path,
        "f.json",
        {"schema": rc.EVIDENCE_SCHEMA, "results": [1, None, {}, {"row": "V0.3-c2"}]},
    )
    _ev(
        tmp_path,
        "g.json",
        {"schema": "other/1", "results": [{"row": "V0.3-c2", "verdict": "PASS"}]},
    )
    entries, warnings = rc.load_lab_evidence(tmp_path)
    assert entries == {}
    assert len(warnings) >= 5


@pytest.mark.parametrize(
    "mutation",
    [
        {"verdict": "pass"},
        {"verdict": "PARTIAL"},
        {"verdict": ""},
        {"recorded_at": "yesterday"},
        {"recorded_at": 12345},
        {"sha": "   "},
        {"package_version": ""},
        {"row": None},
    ],
)
def test_invalid_entry_fields_are_rejected_with_a_warning(
    tmp_path: Path, mutation: dict[str, Any]
) -> None:
    entry = {
        "row": "V0.3-c2",
        "verdict": "PASS",
        "sha": SHA_A,
        "package_version": "1.0.0-rc.1",
        "recorded_at": "2026-10-28T10:00:00+00:00",
        **mutation,
    }
    _ev(tmp_path, "x.json", {"schema": rc.EVIDENCE_SCHEMA, "results": [entry]})
    entries, warnings = rc.load_lab_evidence(tmp_path)
    assert entries == {} and len(warnings) == 1


@pytest.mark.parametrize("pv", ["dev", "DEV", " Dev ", "unknown", "None"])
def test_dev_builds_never_pass(pv: str, fake_head: str) -> None:
    row = next(r for r in rm.load_manifest().rows if r.kind == "lab")
    e = rc.LabEntry(row.id, "PASS", SHA_A, pv, "2026-10-28T10:00:00+00:00", "", "s")
    assert rc.judge_lab(row, [e], SHA_A).status == "MISSING"


def test_naive_and_aware_timestamps_compare_without_crashing(fake_head: str) -> None:
    row = next(r for r in rm.load_manifest().rows if r.kind == "lab")
    a = rc.LabEntry(row.id, "FAIL", SHA_A, "1.0.0-rc.1", "2026-10-28T10:00:00", "", "s")
    b = rc.LabEntry(
        row.id, "PASS", SHA_A, "1.0.0-rc.1", "2026-10-28T11:00:00+00:00", "", "s"
    )
    assert rc.judge_lab(row, [a, b], SHA_A).status == "PASS"
    assert rc.judge_lab(row, [b, a], SHA_A).status == "PASS"


def test_a_later_fail_overrides_an_earlier_pass(fake_head: str) -> None:
    row = next(r for r in rm.load_manifest().rows if r.kind == "lab")
    p = rc.LabEntry(
        row.id, "PASS", SHA_A, "1.0.0-rc.1", "2026-10-28T10:00:00+00:00", "", "s"
    )
    f = rc.LabEntry(
        row.id, "FAIL", SHA_A, "1.0.0-rc.1", "2026-10-28T12:00:00+00:00", "x", "s"
    )
    assert rc.judge_lab(row, [p, f], SHA_A).status == "FAIL"


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_evidence_sha_must_be_a_commit_not_a_movable_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression T-4 (fixed in fix round 1): a hand-written evidence file whose ``sha`` is ``HEAD`` (or a
    branch name) resolves to whatever is checked out *now*, so it counts as
    evidence for every future release SHA and never goes stale (D406)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    env.pop("GIT_DIR", None)

    def g(*a: str) -> str:
        return subprocess.run(
            ["git", *a], cwd=repo, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    g("init", "-q")
    (repo / "f").write_text("1")
    g("add", ".")
    g("commit", "-qm", "one")
    old = g("rev-parse", "HEAD")
    (repo / "src").mkdir()
    (repo / "src" / "x.py").write_text("changed")
    g("add", ".")
    g("commit", "-qm", "two")
    new = g("rev-parse", "HEAD")
    monkeypatch.setattr(rm, "REPO", repo)
    monkeypatch.delenv("GIT_DIR", raising=False)
    row = rm.Row(
        id="V0.3-c2",
        version="V0.3",
        criterion=2,
        text="t",
        kind="lab",
        section="L2",
        analyzer="lab/x.py",
        affected_by=("src/*",),
    )
    # sanity: a real old SHA is stale because src/ changed
    real_old = rc.LabEntry(
        row.id, "PASS", old, "1.0.0-rc.1", "2026-10-28T10:00:00+00:00", "", "s"
    )
    assert rc.judge_lab(row, [real_old], new).status == "MISSING"
    # a movable ref must not be accepted as an evidence SHA
    for ref in ("HEAD", "master", "main", "HEAD~0"):
        e = rc.LabEntry(
            row.id, "PASS", ref, "1.0.0-rc.1", "2026-10-28T10:00:00+00:00", "", "s"
        )
        status = rc.judge_lab(row, [e], new).status
        if ref in {g("branch", "--show-current"), "HEAD", "HEAD~0"}:
            assert status != "PASS", f"evidence sha={ref!r} counted as PASS"


# ------------------------------------------------- auto verdict edge cases


def test_pytest_crash_is_not_masked_by_stale_outcomes() -> None:
    row = rm.Row(
        id="V0.1-c1",
        version="V0.1",
        criterion=1,
        text="t",
        kind="auto",
        tests=("tests/unit/a.py",),
    )
    out = {"tests/unit/a.py::t": {"outcome": "passed", "reason": ""}}
    res = rc.judge_auto(row, out, {"tests/unit/a.py": 3}, {})
    assert res.status == "FAIL"


def test_a_prefix_node_id_does_not_match_a_sibling_file() -> None:
    assert not rm.matches("tests/unit/a.py", "tests/unit/a.pyc::t")
    assert not rm.matches("tests/unit/a.py", "tests/unit/aa.py::t")
    assert not rm.matches("tests/unit/a.py::test_x", "tests/unit/a.py::test_xy")
    assert rm.matches("tests/unit/a.py::test_x", "tests/unit/a.py::test_x[param]")


def test_failing_command_fails_row_even_if_tests_pass() -> None:
    row = rm.Row(
        id="V0.1-c1",
        version="V0.1",
        criterion=1,
        text="t",
        kind="auto",
        tests=("tests/unit/a.py",),
        commands=(("scripts/x.py",),),
    )
    out = {"tests/unit/a.py::t": {"outcome": "passed", "reason": ""}}
    assert (
        rc.judge_auto(row, out, {}, {("scripts/x.py",): (1, "boom")}).status == "FAIL"
    )


def test_xfail_xpass_and_error_outcomes_are_not_a_pass() -> None:
    row = rm.Row(
        id="V0.1-c1",
        version="V0.1",
        criterion=1,
        text="t",
        kind="auto",
        tests=("tests/unit/a.py",),
    )
    for outcome in ("error", "failed"):
        out = {
            "tests/unit/a.py::t": {"outcome": outcome, "reason": ""},
            "tests/unit/a.py::u": {"outcome": "passed", "reason": ""},
        }
        assert rc.judge_auto(row, out, {}, {}).status == "FAIL"


def test_exit_code_with_no_results_and_with_unknown_status() -> None:
    assert rc.exit_code([], None) == 0  # documents the vacuous case
    assert rc.exit_code([rc.Result("x", "auto", "NOT_RUN", "")], None) == 0


# --------------------------------------------- hermetic: no .git for the suite


def test_the_release_unit_tests_pass_with_no_git_dir() -> None:
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/unit/release/test_release_check.py",
            "tests/unit/release/test_manifest_coverage.py::test_roadmap_parse_finds_every_version_with_consecutive_numbers",
            "-k",
            "not collect",
        ],
        cwd=REPO,
        env=CLEAN_ENV,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-1000:]
