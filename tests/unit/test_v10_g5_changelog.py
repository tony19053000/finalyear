"""V1.0 G5 (D421): changelog tooling - ``scripts/changelog.py``.

* ``docs/CHANGELOG.md`` passes ``check``: Keep-a-Changelog headings, an entry
  for every roadmap version V0.1-V1.0, deferred versions say so;
* the roadmap parser finds every version, its tasks with their states, the
  deferral banner (and only the banner: V0.2's struck-through deferred spikes
  do not make V0.2 deferred) and the brief's decision numbers;
* ``check`` reports each kind of problem; ``draft`` is deterministic.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "changelog.py"


@pytest.fixture(scope="module")
def cl() -> ModuleType:
    spec = importlib.util.spec_from_file_location("changelog_tool", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["changelog_tool"] = module
    spec.loader.exec_module(module)
    return module


def test_the_committed_changelog_passes_the_check(cl: ModuleType) -> None:
    versions = cl.load_versions()
    text = cl.CHANGELOG.read_text(encoding="utf-8")
    assert cl.check(text, versions) == []
    assert cl.main(["check"]) == 0
    assert "## [Unreleased]" in text and "## [0.17.0] - " in text


def test_the_roadmap_parses_into_every_version(cl: ModuleType) -> None:
    versions = {v.id: v for v in cl.load_versions()}
    assert "V0.1" in versions and "V1.0" in versions and len(versions) >= 18
    assert {v for v, ver in versions.items() if ver.deferred} == {
        "V0.11",
        "V0.12",
        "V0.13",
        "V0.16",
    }
    v02 = versions["V0.2"]
    assert {t.id for t in v02.deferred_tasks} == {"0.2.8", "0.2.9"}
    v10 = versions["V0.10"]
    assert {t.id for t in v10.tasks} >= {f"0.10.{i}" for i in range(1, 11)}
    assert all(t.state in "x~ " for v in versions.values() for t in v.tasks)
    assert "D259" in v10.decisions and "D1" in versions["V0.3"].decisions
    assert versions["V1.0"].decisions[0] == "D406"


def test_task_titles_drop_evidence_and_markup(cl: ModuleType) -> None:
    roadmap = (
        "## V9.1 — Test version\n\n"
        "- [x] **9.1.1** Do the thing — *evidence: commit abc, tester PASS*\n"
        "- [~] **9.1.2** (G5 lane A, D1) Labels per group\n"
        "- [ ] ~~**9.1.3**~~ **DEFERRED (ADR-0012, with Mode 2)** — S4 export\n"
        "- [ ] **9.1.4** 🧑 Lab run on **four** laptops\n"
    )
    (v,) = cl.parse_roadmap(roadmap)
    assert [(t.id, t.state, t.title) for t in v.tasks] == [
        ("9.1.1", "x", "Do the thing"),
        ("9.1.2", "~", "Labels per group"),
        ("9.1.4", " ", "(lab) Lab run on four laptops"),
    ]
    assert [t.title for t in v.deferred_tasks] == ["S4 export"]
    assert not v.deferred


def test_check_reports_each_problem(cl: ModuleType) -> None:
    versions = cl.parse_roadmap(
        "## V9.1 — Shipped\n\n- [x] **9.1.1** A\n\n"
        "## V9.2 — Later\n\n> **DEFERRED — ADR-0012.**\n\n- [ ] **9.2.1** B\n"
    )
    good = (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n\n#### V9.1 - Shipped\n\n- A\n\n"
        "#### V9.2 - Later\n\n- Deferred (ADR-0012).\n"
    )
    assert cl.check(good, versions) == []
    bad = (
        "# Changelog\n\n## 1.0 - someday\n\n### Things\n\n"
        "#### V9.2 - Later\n\n- built it\n"
    )
    problems = cl.check(bad, versions)
    assert any("bad release heading" in p for p in problems)
    assert any("unknown change type '### Things'" in p for p in problems)
    assert any(p.startswith("V9.1 (Shipped) has no") for p in problems)
    assert any("V9.2 is deferred" in p for p in problems)
    assert cl.check("# Changelog\n", versions)[0].startswith("no release heading")


def test_draft_is_deterministic_and_marks_deferred_versions(cl: ModuleType) -> None:
    versions = cl.load_versions()
    first = cl.draft(versions)
    assert first == cl.draft(cl.load_versions())
    v11 = first.split("#### V0.11", 1)[1].split("#### ", 1)[0]
    assert "Deferred (ADR-0012)" in v11
    assert "Decisions: D406-D421" in first


def test_decision_ranges_are_compact(cl: ModuleType) -> None:
    assert cl._ranges(["D3", "D1", "D2", "D7", "D9", "D8"]) == "D1-D3, D7-D9"
    assert cl._ranges(["D5"]) == "D5"
    assert cl._ranges([]) == "-"


def test_unknown_draft_version_is_a_usage_error(
    cl: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cl.main(["draft", "V9.9"]) == 2
    assert "unknown version" in capsys.readouterr().err
