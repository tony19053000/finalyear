"""The V1.0 acceptance manifest (D407, ADR-0023): load, validate, check coverage.

``release/acceptance.yaml`` has one row per success criterion of every roadmap
version below V1.0 (V0.1-V0.17, deferred versions included). Each row says how
the criterion is proven on the release build:

* ``auto`` - pytest node ids (and optional fixed commands) that
  ``scripts/release_check.py`` runs;
* ``lab`` - a section of the combined V1.0 lab campaign (L0-L5, brief §4) whose
  verdict arrives as an evidence JSON file in ``lab/reports/raw/``;
* ``waiver`` - not proven in v1.0, with a reason and the decision (ADR or
  D-number) that allows it.

This module is pure apart from file reads and the one ``pytest --collect-only``
subprocess in :func:`collect`; ``scripts/release_check.py`` (the runner) and
``tests/unit/release/test_manifest_coverage.py`` (the coverage gate) import it.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import yaml

REPO: Final = Path(__file__).resolve().parent.parent
ROADMAP: Final = REPO / "docs" / "DEVELOPMENT_ROADMAP.md"
MANIFEST: Final = REPO / "release" / "acceptance.yaml"
SCHEMA: Final = "meshforge.acceptance/1"

KINDS: Final = ("auto", "lab", "waiver")
#: Opt-in flags an auto row may need (tests/conftest.py tiers/capabilities).
#: Never ``--run-lab``/``--run-gpu``/``--run-soak``: those need hardware or days,
#: so such a criterion is a ``lab`` row.
ALLOWED_FLAGS: Final = frozenset({"--run-slow", "--run-chaos", "--run-perf"})
#: The release whose criteria are *not* rows: V1.0's own criteria are what the
#: acceptance report, the soak and the evaluation report prove.
RELEASE_VERSION: Final = (1, 0)

_VERSION_HEADING = re.compile(r"^## (V(\d+)\.(\d+))\b")
_CRITERION = re.compile(r"^(\d+)\.\s+(\S.*)$")
_ROW_ID = re.compile(r"^(V\d+\.\d+)-c(\d+)$")
_ADR_REF = re.compile(r"^ADR-(\d{4})$")
_D_REF = re.compile(r"^D(\d+)$")


class ManifestError(Exception):
    """The manifest file cannot be read or has the wrong shape."""


@dataclass(frozen=True)
class Criterion:
    """One numbered line of a roadmap version's **Success criteria** block."""

    version: str
    number: int
    text: str

    @property
    def row_id(self) -> str:
        return f"{self.version}-c{self.number}"


@dataclass(frozen=True)
class Row:
    """One manifest row (fields not used by a kind stay empty)."""

    id: str
    version: str
    criterion: int
    text: str
    kind: str
    tests: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    commands: tuple[tuple[str, ...], ...] = ()
    section: str = ""
    checklist: str = ""
    analyzer: str = ""
    affected_by: tuple[str, ...] = ()
    reason: str = ""
    decision: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Manifest:
    rows: tuple[Row, ...]
    lab_sections: Mapping[str, str]
    """Section id (``L0``…) -> title."""

    def by_id(self) -> dict[str, Row]:
        return {row.id: row for row in self.rows}


@dataclass
class Problems:
    """Validation findings, one human-readable line each."""

    items: list[str] = field(default_factory=list)

    def add(self, row_id: str, message: str) -> None:
        self.items.append(f"{row_id}: {message}")

    def __bool__(self) -> bool:
        return bool(self.items)


def normalise(text: str) -> str:
    """Whitespace-insensitive form used to compare row text with the roadmap."""
    return " ".join(text.split())


def version_key(version: str) -> tuple[int, int]:
    major, minor = version.removeprefix("V").split(".")
    return int(major), int(minor)


def parse_roadmap(text: str) -> list[Criterion]:
    """Every success criterion of every ``## Vx.y`` section, in file order.

    A criterion is a ``N. text`` line after the ``**Success criteria**`` line;
    an indented line continues the previous criterion. The block ends at the
    first line that is neither (blank lines inside it are skipped).
    """
    criteria: list[Criterion] = []
    version: str | None = None
    in_block = False
    for line in text.splitlines():
        if m := _VERSION_HEADING.match(line):
            version, in_block = m.group(1), False
            continue
        if line.startswith(("## ", "# ")):
            version, in_block = None, False
            continue
        if version is None:
            continue
        if line.strip() == "**Success criteria**":
            in_block = True
            continue
        if not in_block:
            continue
        if not line.strip():
            continue
        if m := _CRITERION.match(line):
            criteria.append(Criterion(version, int(m.group(1)), m.group(2).strip()))
        elif (
            line.startswith((" ", "\t"))
            and criteria
            and criteria[-1].version == version
        ):
            last = criteria.pop()
            criteria.append(
                Criterion(last.version, last.number, f"{last.text} {line.strip()}")
            )
        else:
            in_block = False
    return criteria


def covered_criteria(criteria: Iterable[Criterion]) -> list[Criterion]:
    """The criteria the manifest must cover: every version below V1.0."""
    return [c for c in criteria if version_key(c.version) < RELEASE_VERSION]


def _str_tuple(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    raise ManifestError(f"{where}: expected a string or a list of strings")


def _row(raw: object, index: int) -> Row:
    if not isinstance(raw, dict):
        raise ManifestError(f"rows[{index}]: expected a mapping")
    where = f"rows[{index}] ({raw.get('id', '?')})"
    known = {
        "id",
        "version",
        "criterion",
        "text",
        "kind",
        "evidence",
        "affected_by",
        "reason",
        "decision",
        "note",
    }
    if unknown := sorted(set(raw) - known):
        raise ManifestError(f"{where}: unknown keys {unknown}")
    evidence = raw.get("evidence") or {}
    if not isinstance(evidence, dict):
        raise ManifestError(f"{where}: evidence must be a mapping")
    ev_known = {"tests", "flags", "commands", "section", "checklist", "analyzer"}
    if unknown := sorted(set(evidence) - ev_known):
        raise ManifestError(f"{where}: unknown evidence keys {unknown}")
    commands_raw = evidence.get("commands") or []
    if not isinstance(commands_raw, list):
        raise ManifestError(f"{where}: evidence.commands must be a list of argv lists")
    commands = tuple(_str_tuple(c, f"{where} evidence.commands") for c in commands_raw)
    criterion = raw.get("criterion")
    if not isinstance(criterion, int) or isinstance(criterion, bool):
        raise ManifestError(f"{where}: criterion must be an integer")
    for key in ("id", "version", "text", "kind"):
        if not isinstance(raw.get(key), str):
            raise ManifestError(f"{where}: {key} must be a string")
    return Row(
        id=raw["id"],
        version=raw["version"],
        criterion=criterion,
        text=raw["text"],
        kind=raw["kind"],
        tests=_str_tuple(evidence.get("tests"), f"{where} evidence.tests"),
        flags=_str_tuple(evidence.get("flags"), f"{where} evidence.flags"),
        commands=commands,
        section=str(evidence.get("section") or ""),
        checklist=str(evidence.get("checklist") or ""),
        analyzer=str(evidence.get("analyzer") or ""),
        affected_by=_str_tuple(raw.get("affected_by"), f"{where} affected_by"),
        reason=str(raw.get("reason") or ""),
        decision=_str_tuple(raw.get("decision"), f"{where} decision"),
        note=str(raw.get("note") or ""),
    )


def load_manifest(path: Path = MANIFEST) -> Manifest:
    """Parse the YAML file into rows; shape errors raise :class:`ManifestError`."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ManifestError(f"{path}: not a {SCHEMA} document")
    sections = data.get("lab_sections")
    if not isinstance(sections, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in sections.items()
    ):
        raise ManifestError(f"{path}: lab_sections must map section ids to titles")
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ManifestError(f"{path}: rows must be a list")
    return Manifest(
        rows=tuple(_row(raw, i) for i, raw in enumerate(rows)),
        lab_sections=dict(sections),
    )


def decision_exists(ref: str, repo: Path = REPO) -> bool:
    """``ADR-NNNN`` -> an ADR file; ``DNNN`` -> a decision-table row ``| DNNN |``
    in a version brief or the project brief's decision log."""
    if m := _ADR_REF.match(ref):
        return any((repo / "docs" / "adr").glob(f"{m.group(1)}-*.md"))
    if _D_REF.match(ref):
        needle = re.compile(rf"^\|\s*{re.escape(ref)}\s*\|", re.MULTILINE)
        docs = [
            *sorted((repo / "docs" / "briefs").glob("*.md")),
            repo / "docs" / "PROJECT_BRIEF.md",
        ]
        return any(
            needle.search(doc.read_text(encoding="utf-8"))
            for doc in docs
            if doc.is_file()
        )
    return False


def _is_sentence(text: str) -> bool:
    return len(text.split()) >= 5


def validate(
    manifest: Manifest, criteria: Sequence[Criterion], repo: Path = REPO
) -> list[str]:
    """Every rule except "the node ids collect" (that needs pytest, see :func:`collect`)."""
    problems = Problems()
    expected = {c.row_id: c for c in covered_criteria(criteria)}
    seen: set[str] = set()
    for row in manifest.rows:
        if row.id in seen:
            problems.add(row.id, "duplicate row id")
        seen.add(row.id)
        m = _ROW_ID.match(row.id)
        if m is None or m.group(1) != row.version or int(m.group(2)) != row.criterion:
            problems.add(
                row.id, "id must be '<version>-c<criterion>' matching its fields"
            )
        crit = expected.get(row.id)
        if crit is None:
            problems.add(row.id, "no such success criterion in the roadmap")
        elif normalise(crit.text) != normalise(row.text):
            problems.add(
                row.id, "text differs from the roadmap criterion (re-review the row)"
            )
        if row.kind not in KINDS:
            problems.add(row.id, f"kind must be one of {KINDS}")
            continue
        for test in row.tests:
            if not test.startswith("tests/") or "\\" in test:
                problems.add(row.id, f"test node id must start with tests/: {test}")
        if bad := sorted(set(row.flags) - ALLOWED_FLAGS):
            problems.add(row.id, f"flags not allowed: {bad}")
        for cmd in row.commands:
            if not cmd or not cmd[0].endswith(".py") or not (repo / cmd[0]).is_file():
                problems.add(
                    row.id, f"command must start with a repo script: {list(cmd)}"
                )
        if row.kind == "auto":
            if not row.tests and not row.commands:
                problems.add(row.id, "auto row needs tests or commands")
        elif row.kind == "lab":
            if row.section not in manifest.lab_sections:
                problems.add(
                    row.id, f"lab section {row.section!r} is not in lab_sections"
                )
            if not row.affected_by:
                problems.add(row.id, "lab row needs affected_by globs (D406 redo rule)")
            if not (row.checklist or row.analyzer):
                problems.add(row.id, "lab row needs a checklist or analyzer command")
            if row.checklist and not (repo / row.checklist.split("#", 1)[0]).is_file():
                problems.add(row.id, f"checklist file missing: {row.checklist}")
        else:
            if not _is_sentence(row.reason):
                problems.add(row.id, "waiver needs a reason (a sentence)")
            if not row.decision:
                problems.add(row.id, "waiver must cite a decision (ADR-NNNN or DNNN)")
            for ref in row.decision:
                if not decision_exists(ref, repo):
                    problems.add(row.id, f"cited decision not found: {ref}")
            if row.tests or row.commands or row.flags:
                problems.add(row.id, "a waiver carries no tests or commands")
    for row_id in sorted(set(expected) - seen, key=_sort_key):
        problems.add(row_id, "criterion has no manifest row")
    return problems.items


def _sort_key(row_id: str) -> tuple[int, int, int]:
    version, _, crit = row_id.partition("-c")
    major, minor = version_key(version)
    return major, minor, int(crit) if crit.isdigit() else 0


def tier_of(node_id: str) -> str:
    """``tests/<tier>/...`` -> ``<tier>`` (the chaos suite lives in tests/cluster/chaos)."""
    parts = node_id.split("::", 1)[0].split("/")
    if len(parts) >= 3 and parts[1] == "cluster" and parts[2] == "chaos":
        return "chaos"
    return parts[1] if len(parts) > 1 else ""


def static_problem(node_id: str, repo: Path = REPO) -> str | None:
    """Cheap existence check of a node id: the path exists and, for
    ``file::name`` or ``file::Class::name``, the name is defined there."""
    path_part, _, rest = node_id.partition("::")
    path = repo / path_part
    if not path.exists():
        return f"path does not exist: {path_part}"
    if not rest:
        return None
    if not path.is_file() or path.suffix != ".py":
        return f"not a test module: {path_part}"
    names = rest.split("[", 1)[0].split("::")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scope: Sequence[ast.stmt] = tree.body
    for name in names:
        found = next(
            (
                node
                for node in scope
                if isinstance(
                    node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
                )
                and node.name == name
            ),
            None,
        )
        if found is None:
            return f"{name} is not defined in {path_part}"
        scope = found.body if isinstance(found, ast.ClassDef) else []
    return None


def matches(node_id: str, collected: str) -> bool:
    """Whether a collected item belongs to a manifest node id (file, dir, test, class)."""
    if collected == node_id:
        return True
    return collected.startswith(node_id) and collected[len(node_id)] in ":[/"


def collect(node_ids: Sequence[str], repo: Path = REPO) -> dict[str, int]:
    """Number of items ``pytest --collect-only`` collects for each node id."""
    result = subprocess.run(  # our own interpreter, fixed argv
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            *sorted(set(node_ids)),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 5):
        raise RuntimeError(
            f"pytest collection failed (exit {result.returncode}):\n"
            f"{result.stdout[-4000:]}{result.stderr[-4000:]}"
        )
    items = [line.strip() for line in result.stdout.splitlines() if "::" in line]
    return {nid: sum(1 for item in items if matches(nid, item)) for nid in node_ids}


def all_test_ids(manifest: Manifest) -> list[str]:
    """Every node id the manifest names (auto evidence and lab supporting tests)."""
    return sorted({test for row in manifest.rows for test in row.tests})
