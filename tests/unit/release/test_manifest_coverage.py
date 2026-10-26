"""V1.0 G0 (roadmap 1.0.1, D407, ADR-0023): the acceptance manifest covers the roadmap.

* every success criterion of V0.1-V0.17 (deferred versions too) has exactly one
  row in ``release/acceptance.yaml`` whose text is the roadmap text;
* every ``auto`` (and supporting ``lab``) node id exists and **collects**;
* every waiver cites a decision that exists (an ADR file or a ``| Dnnn |`` row);
* the validation rules themselves, on small hand-made manifests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

REPO: Final = Path(__file__).resolve().parents[3]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import release_manifest as rm  # noqa: E402

DEFERRED: Final = ("V0.11", "V0.12", "V0.13", "V0.16")


@pytest.fixture(scope="module")
def criteria() -> list[rm.Criterion]:
    return rm.parse_roadmap(rm.ROADMAP.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> rm.Manifest:
    return rm.load_manifest()


# ------------------------------------------------------------ the real files


def test_roadmap_parse_finds_every_version_with_consecutive_numbers(
    criteria: list[rm.Criterion],
) -> None:
    by_version: dict[str, list[int]] = {}
    for c in criteria:
        by_version.setdefault(c.version, []).append(c.number)
    expected = {f"V0.{n}" for n in range(1, 18)} | {"V1.0"}
    assert set(by_version) == expected
    for version, numbers in by_version.items():
        assert numbers == list(range(1, len(numbers) + 1)), version
    # spot checks that pin the parser to the real text
    assert len(by_version["V0.4"]) == 6
    assert len(by_version["V0.16"]) == 2
    texts = {c.row_id: c.text for c in criteria}
    assert texts["V0.6-c2"].startswith("Controller `kill -9` with 20 running jobs")
    assert texts["V0.4-c1"].endswith(
        "(GPU kernels are not bit-reproducible across hardware — D76, 2026-09-29)."
    )


def test_v1_0_criteria_are_not_rows(criteria: list[rm.Criterion]) -> None:
    covered = rm.covered_criteria(criteria)
    assert {c.version for c in covered} == {f"V0.{n}" for n in range(1, 18)}
    assert all(c.version != "V1.0" for c in covered)


def test_the_manifest_is_valid(
    manifest: rm.Manifest, criteria: list[rm.Criterion]
) -> None:
    assert rm.validate(manifest, criteria) == []


def test_every_criterion_has_exactly_one_row(
    manifest: rm.Manifest, criteria: list[rm.Criterion]
) -> None:
    ids = [row.id for row in manifest.rows]
    assert len(ids) == len(set(ids))
    assert set(ids) == {c.row_id for c in rm.covered_criteria(criteria)}


def test_deferred_versions_are_waivers_citing_adr_0012(manifest: rm.Manifest) -> None:
    deferred = [row for row in manifest.rows if row.version in DEFERRED]
    assert len(deferred) == 15
    for row in deferred:
        assert row.kind == "waiver", row.id
        assert "ADR-0012" in row.decision, row.id
    # ...and nothing else is waived without the team (Q44): today no other waiver
    assert {row.id for row in manifest.rows if row.kind == "waiver"} == {
        row.id for row in deferred
    }


def test_every_waiver_cites_an_existing_decision(manifest: rm.Manifest) -> None:
    for row in manifest.rows:
        if row.kind == "waiver":
            assert row.decision, row.id
            assert all(rm.decision_exists(ref) for ref in row.decision), row.id


def test_every_lab_row_names_a_campaign_section_and_redo_globs(
    manifest: rm.Manifest,
) -> None:
    assert set(manifest.lab_sections) == {"L0", "L1", "L2", "L3", "L4", "L5"}
    for row in manifest.rows:
        if row.kind == "lab":
            assert row.section in manifest.lab_sections, row.id
            assert row.affected_by, row.id


def test_every_node_id_exists_statically(manifest: rm.Manifest) -> None:
    """Fast and on every OS: the file exists and the test/class is defined."""
    problems = {nid: rm.static_problem(nid) for nid in rm.all_test_ids(manifest)}
    assert {k: v for k, v in problems.items() if v} == {}


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="some named tests are POSIX-only modules (cluster/e2e) and are not collected",
)
@pytest.mark.extras("controller", "agent", "ml")
@pytest.mark.timeout(240)
def test_every_node_id_collects(manifest: rm.Manifest) -> None:
    counts = rm.collect(rm.all_test_ids(manifest))
    assert {nid: n for nid, n in counts.items() if n == 0} == {}


# ------------------------------------------------------------ parser rules

ROADMAP_SAMPLE: Final = """\
# M1

## V0.1 — One

**Success criteria**
1. First thing works.
2. Second thing
   continues here.

**Deliverable:** x.

---

## V0.2 — Two

**Testing:** none.
**Success criteria**
1. Only one.
3. Numbering is taken as written.
Not a criterion.
4. After the block ended: ignored.

## Cross-cutting tracks

**Success criteria**
1. Not a version: ignored.

## V1.0 — Release
**Success criteria**
1. Release criterion.
"""


def test_parse_roadmap_blocks_continuations_and_ends() -> None:
    got = [(c.row_id, c.text) for c in rm.parse_roadmap(ROADMAP_SAMPLE)]
    assert got == [
        ("V0.1-c1", "First thing works."),
        ("V0.1-c2", "Second thing continues here."),
        ("V0.2-c1", "Only one."),
        ("V0.2-c3", "Numbering is taken as written."),
        ("V1.0-c1", "Release criterion."),
    ]


def _write_manifest(tmp_path: Path, rows: list[dict[str, Any]]) -> rm.Manifest:
    path = tmp_path / "acceptance.yaml"
    doc = {"schema": rm.SCHEMA, "lab_sections": {"L2": "functional"}, "rows": rows}
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return rm.load_manifest(path)


def _crits() -> list[rm.Criterion]:
    return [
        rm.Criterion("V0.1", 1, "First thing works."),
        rm.Criterion("V0.1", 2, "Second."),
    ]


AUTO: Final = {
    "id": "V0.1-c1",
    "version": "V0.1",
    "criterion": 1,
    "text": "First   thing\nworks.",
    "kind": "auto",
    "evidence": {"tests": ["tests/unit/test_protos.py"]},
}
LAB: Final = {
    "id": "V0.1-c2",
    "version": "V0.1",
    "criterion": 2,
    "text": "Second.",
    "kind": "lab",
    "evidence": {"section": "L2", "analyzer": "lab/x.py"},
    "affected_by": ["src/*"],
}


def test_a_complete_manifest_is_valid_and_text_is_whitespace_insensitive(
    tmp_path: Path,
) -> None:
    assert rm.validate(_write_manifest(tmp_path, [AUTO, LAB]), _crits()) == []


def test_a_criterion_without_a_row_is_reported(tmp_path: Path) -> None:
    problems = rm.validate(_write_manifest(tmp_path, [AUTO]), _crits())
    assert problems == ["V0.1-c2: criterion has no manifest row"]


def test_a_row_without_a_criterion_and_a_duplicate_are_reported(tmp_path: Path) -> None:
    ghost = {**AUTO, "id": "V0.1-c9", "criterion": 9}
    problems = rm.validate(
        _write_manifest(tmp_path, [AUTO, AUTO, LAB, ghost]), _crits()
    )
    assert "V0.1-c1: duplicate row id" in problems
    assert "V0.1-c9: no such success criterion in the roadmap" in problems


def test_changed_roadmap_text_forces_a_re_review(tmp_path: Path) -> None:
    drifted = {**AUTO, "text": "First thing mostly works."}
    problems = rm.validate(_write_manifest(tmp_path, [drifted, LAB]), _crits())
    assert problems == [
        "V0.1-c1: text differs from the roadmap criterion (re-review the row)"
    ]


def test_id_must_match_version_and_number(tmp_path: Path) -> None:
    wrong = {**AUTO, "criterion": 2}
    problems = rm.validate(_write_manifest(tmp_path, [wrong, LAB]), _crits())
    assert (
        "V0.1-c1: id must be '<version>-c<criterion>' matching its fields" in problems
    )


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({**AUTO, "evidence": {}}, "V0.1-c1: auto row needs tests or commands"),
        (
            {**AUTO, "evidence": {"tests": ["unit/test_x.py"]}},
            "V0.1-c1: test node id must start with tests/: unit/test_x.py",
        ),
        (
            {
                **AUTO,
                "evidence": {
                    "tests": ["tests/unit/test_protos.py"],
                    "flags": ["--run-gpu"],
                },
            },
            "V0.1-c1: flags not allowed: ['--run-gpu']",
        ),
        (
            {**AUTO, "evidence": {"commands": [["scripts/nope.py"]]}},
            "V0.1-c1: command must start with a repo script: ['scripts/nope.py']",
        ),
        (
            {**AUTO, "kind": "manual"},
            "V0.1-c1: kind must be one of ('auto', 'lab', 'waiver')",
        ),
    ],
)
def test_auto_and_kind_rules(
    tmp_path: Path, row: dict[str, Any], expected: str
) -> None:
    assert expected in rm.validate(_write_manifest(tmp_path, [row, LAB]), _crits())


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (
            {**LAB, "affected_by": []},
            "V0.1-c2: lab row needs affected_by globs (D406 redo rule)",
        ),
        (
            {**LAB, "evidence": {"section": "L9", "analyzer": "x"}},
            "V0.1-c2: lab section 'L9' is not in lab_sections",
        ),
        (
            {**LAB, "evidence": {"section": "L2"}},
            "V0.1-c2: lab row needs a checklist or analyzer command",
        ),
        (
            {
                **LAB,
                "evidence": {"section": "L2", "checklist": "lab/checklists/nope.md#a"},
            },
            "V0.1-c2: checklist file missing: lab/checklists/nope.md#a",
        ),
    ],
)
def test_lab_rules(tmp_path: Path, row: dict[str, Any], expected: str) -> None:
    assert expected in rm.validate(_write_manifest(tmp_path, [AUTO, row]), _crits())


WAIVER: Final = {
    "id": "V0.1-c2",
    "version": "V0.1",
    "criterion": 2,
    "text": "Second.",
    "kind": "waiver",
    "reason": "Deferred by the scope decision for this release.",
    "decision": ["ADR-0012"],
}


def test_a_waiver_citing_a_real_adr_or_d_number_is_valid(tmp_path: Path) -> None:
    for decision in (["ADR-0012"], ["D406"], ["ADR-0012", "D357"]):
        row = {**WAIVER, "decision": decision}
        assert rm.validate(_write_manifest(tmp_path, [AUTO, row]), _crits()) == [], (
            decision
        )


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"decision": []}, "V0.1-c2: waiver must cite a decision (ADR-NNNN or DNNN)"),
        ({"decision": ["ADR-9999"]}, "V0.1-c2: cited decision not found: ADR-9999"),
        ({"decision": ["D99999"]}, "V0.1-c2: cited decision not found: D99999"),
        ({"decision": ["because"]}, "V0.1-c2: cited decision not found: because"),
        ({"reason": "deferred"}, "V0.1-c2: waiver needs a reason (a sentence)"),
        (
            {"evidence": {"tests": ["tests/unit/test_protos.py"]}},
            "V0.1-c2: a waiver carries no tests or commands",
        ),
    ],
)
def test_waiver_rules(tmp_path: Path, change: dict[str, Any], expected: str) -> None:
    row = {**WAIVER, **change}
    assert expected in rm.validate(_write_manifest(tmp_path, [AUTO, row]), _crits())


def test_a_d_number_must_be_a_decision_table_row_not_a_mention() -> None:
    # D76 is mentioned in the roadmap text but D-rows live in the briefs' tables
    assert rm.decision_exists("D406")
    assert not rm.decision_exists("D0")


@pytest.mark.parametrize(
    "doc",
    [
        "[]",
        "schema: other/1\nlab_sections: {}\nrows: []\n",
        f"schema: {rm.SCHEMA}\nlab_sections: []\nrows: []\n",
        f"schema: {rm.SCHEMA}\nlab_sections: {{}}\nrows: {{}}\n",
        f"schema: {rm.SCHEMA}\nlab_sections: {{}}\nrows: [{{id: x}}]\n",
        f"schema: {rm.SCHEMA}\nlab_sections: {{}}\nrows: [{{id: x, criterion: 1, bogus: 1}}]\n",
        "schema: [unclosed\n",
    ],
)
def test_malformed_manifests_are_a_typed_error(tmp_path: Path, doc: str) -> None:
    path = tmp_path / "m.yaml"
    path.write_text(doc, encoding="utf-8")
    with pytest.raises(rm.ManifestError):
        rm.load_manifest(path)


def test_a_missing_manifest_file_is_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(rm.ManifestError):
        rm.load_manifest(tmp_path / "absent.yaml")


@pytest.mark.parametrize(
    ("node_id", "problem"),
    [
        ("tests/unit/test_protos.py", None),
        ("tests/unit/test_protos.py::test_regeneration_is_byte_identical", None),
        ("tests/unit/test_protos.py::test_regeneration_is_byte_identical[x]", None),
        ("tests/e2e", None),
        (
            "tests/unit/test_protos.py::test_nope",
            "test_nope is not defined in tests/unit/test_protos.py",
        ),
        ("tests/unit/absent.py", "path does not exist: tests/unit/absent.py"),
        ("tests/e2e::x", "not a test module: tests/e2e"),
    ],
)
def test_static_node_id_check(node_id: str, problem: str | None) -> None:
    assert rm.static_problem(node_id) == problem


def test_static_check_resolves_class_methods(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_c.py").write_text(
        "class TestA:\n    def test_b(self):\n        pass\n", encoding="utf-8"
    )
    assert rm.static_problem("tests/test_c.py::TestA::test_b", tmp_path) is None
    assert rm.static_problem("tests/test_c.py::TestA::test_x", tmp_path) is not None


@pytest.mark.parametrize(
    ("node_id", "item", "hit"),
    [
        ("tests/unit/a.py", "tests/unit/a.py::test_x", True),
        ("tests/unit/a.py::test_x", "tests/unit/a.py::test_x[1-2]", True),
        ("tests/unit/a.py::test_x", "tests/unit/a.py::test_x", True),
        ("tests/unit/a.py::test_x", "tests/unit/a.py::test_xy", False),
        ("tests/unit/a.py", "tests/unit/a.pyc::t", False),
        ("tests/e2e", "tests/e2e/test_a.py::t", True),
        ("tests/e2e", "tests/e2e_other/test_a.py::t", False),
    ],
)
def test_collected_items_match_their_node_id(
    node_id: str, item: str, hit: bool
) -> None:
    assert rm.matches(node_id, item) is hit


@pytest.mark.parametrize(
    ("node_id", "tier"),
    [
        ("tests/unit/x.py::t", "unit"),
        ("tests/cluster/test_a.py", "cluster"),
        ("tests/cluster/chaos/test_v06_chaos.py", "chaos"),
        ("tests/e2e", "e2e"),
    ],
)
def test_tier_of(node_id: str, tier: str) -> None:
    assert rm.tier_of(node_id) == tier
