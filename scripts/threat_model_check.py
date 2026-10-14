"""Check that every THREAT_MODEL row is closed (V0.7 G7, D166 (2); stdlib only).

Criterion 5 of V0.7 ("every threat-model row is mitigated by a test or
accepted with a reason") made mechanical. For every row of
``docs/THREAT_MODEL.md`` §5 (an ``| XX-Yn |`` line), the status cell must be:

* ``mitigated`` - with at least one test reference in the Test or Status cell,
  and **every** test reference must match a test that
  ``pytest --collect-only -q -m security`` collects (so it exists and carries
  the ``security`` capability marker); or
* ``accepted`` (``**accepted**``) - with a non-empty reason in the row, and the
  row ID named in the §7 "Accepted risks" table. Test references in an
  accepted row are optional but, if given, must be collected the same way.

A status may combine both (``mitigated; **accepted**: residual ...``): then
both rules apply. ``open``, ``decision (Gn)`` or ``to be written`` anywhere in
a row fails it.

Test references (inside backticks): ``tests/<tier>/.../test_x.py`` (a whole
file: at least one collected test in it), ``tests/...py::test_name`` (that
test, any parametrisation), ``::test_name`` (same file as the previous
reference in the cell), ``[param]`` suffixes, and ``*`` wildcards in the name.

Usage::

    uv run python scripts/threat_model_check.py [--threat-model PATH]

Prints one line per row (ID, status, test refs, collected) and exits 0 only when
every row passes. The unit test ``tests/unit/test_threat_model_check.py`` runs
it, so a stale reference or a reopened row fails the unit tier.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
THREAT_MODEL = REPO / "docs" / "THREAT_MODEL.md"
TIER_DIRS = ("tests/unit", "tests/component", "tests/cluster")

_ROW_ID = re.compile(r"^[A-Z]{2}-[A-Z]\d+$")
_CODE = re.compile(r"`([^`]+)`")
_FILE_REF = re.compile(r"^(tests/[\w./-]+\.py)(::.+)?$")
_NAME_REF = re.compile(r"^::[\w*\[\]:.-]+$")
_FORBIDDEN = (
    re.compile(r"\bopen \(G\d"),
    re.compile(r"\bdecision \(G\d"),
    re.compile(r"to be written", re.IGNORECASE),
)
_ACCEPTED = re.compile(r"\*\*accepted\*\*|^accepted\b", re.IGNORECASE)
_MITIGATED = re.compile(r"^\s*(\*\*)?mitigated\b", re.IGNORECASE)
#: A test cell whose code spans look like tests but are not (bare file names).
_BARE_TEST_FILE = re.compile(r"^test_\w+\.py(::.*)?$")


@dataclass
class Row:
    """One §5 row."""

    row_id: str
    section: str
    status: str
    text: str
    mitigation: str = ""
    refs: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def split_cells(line: str) -> list[str]:
    """Split a Markdown table line on unescaped ``|``."""
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    in_code = False
    for char in line.strip():
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            continue
        if char == "`":
            in_code = not in_code
        if char == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    cells.append("".join(current).strip())
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


def test_refs(cell: str) -> tuple[list[str], list[str]]:
    """Test references in ``cell`` (``::name`` resolved against the previous
    file) and problems (bare file names that cannot be resolved)."""
    refs: list[str] = []
    problems: list[str] = []
    current_file: str | None = None
    for token in _CODE.findall(cell):
        token = token.strip()
        if match := _FILE_REF.match(token):
            current_file = match.group(1)
            refs.append(token)
        elif _NAME_REF.match(token):
            if current_file is None:
                problems.append(f"`{token}` names no file before it")
            else:
                refs.append(current_file + token)
        elif _BARE_TEST_FILE.match(token):
            problems.append(f"`{token}`: write the full path (tests/<tier>/...)")
    return refs, problems


def parse(text: str) -> tuple[list[Row], str]:
    """Rows of §5 and the text of §7."""
    rows: list[Row] = []
    section = ""
    accepted_section: list[str] = []
    in_section7 = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section7 = line.startswith("## 7.")
            section = line[3:].strip()
        elif line.startswith("### "):
            section = line[4:].strip()
        if in_section7:
            accepted_section.append(line)
            continue
        if not section.startswith("5.") or not line.startswith("|"):
            continue
        cells = split_cells(line)
        if not cells or not _ROW_ID.match(cells[0]):
            continue
        row = Row(row_id=cells[0], section=section, status="", text=line)
        if len(cells) != 8:
            row.problems.append(f"expected 8 cells, found {len(cells)}")
            rows.append(row)
            continue
        row.status = cells[7]
        row.mitigation = cells[4]
        for cell in (cells[6], cells[7]):
            refs, problems = test_refs(cell)
            row.refs.extend(refs)
            row.problems.extend(problems)
        rows.append(row)
    return rows, "\n".join(accepted_section)


def judge(row: Row, accepted_text: str) -> None:
    """Add the rule violations of ``row`` to ``row.problems`` (collection
    is checked separately)."""
    for pattern in _FORBIDDEN:
        if pattern.search(row.text):
            row.problems.append(f"contains '{pattern.pattern}'")
    status = row.status
    mitigated = bool(_MITIGATED.match(status))
    accepted = bool(_ACCEPTED.search(status))
    if not mitigated and not accepted:
        row.problems.append("status is neither `mitigated` nor `**accepted**`")
    if mitigated and not row.refs:
        row.problems.append("`mitigated` without a test reference")
    if accepted and not (_reason(status) or _reason(row.mitigation)):
        row.problems.append("`accepted` without a reason")
    if accepted and not re.search(rf"\b{re.escape(row.row_id)}\b", accepted_text):
        row.problems.append(f"`accepted` but {row.row_id} is not named in §7")


def _reason(cell: str) -> bool:
    """Whether ``cell`` gives a reason after the word ``accepted``: at least
    a few words of text after it (``accepted`` alone is no reason)."""
    match = re.search(r"accepted\**\s*(.*)", cell, flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        return False
    return len(re.sub(r"[\s*:;()\-]", "", match.group(1))) >= 10


def collect_security_ids(repo: Path = REPO) -> list[str]:
    """Node ids ``pytest --collect-only -q -m security`` selects (all tiers)."""
    result = subprocess.run(  # fixed argv, our own interpreter
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            "security",
            "-p",
            "no:cacheprovider",
            *TIER_DIRS,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    # 0 = collected, 5 = nothing selected (still a valid, empty answer).
    if result.returncode not in (0, 5):
        raise RuntimeError(
            "pytest collection failed "
            f"(exit {result.returncode}):\n{result.stdout[-4000:]}{result.stderr[-4000:]}"
        )
    return [line.strip() for line in result.stdout.splitlines() if "::" in line]


def _strip_params(node_id: str) -> str:
    return node_id.split("[", 1)[0]


def ref_matches(ref: str, collected: Iterable[str]) -> bool:
    """Whether a reference names at least one collected node id."""
    if "::" not in ref:
        prefix = ref + "::"
        return any(node.startswith(prefix) for node in collected)
    has_param = "[" in ref
    for node in collected:
        candidate = node if has_param else _strip_params(node)
        if candidate == ref or fnmatch.fnmatchcase(candidate, ref):
            return True
        # A class reference (file::Class) names every method in it.
        if node.startswith(ref + "::"):
            return True
    return False


def check(text: str, collected: Sequence[str]) -> tuple[list[Row], dict[str, bool]]:
    """Judge every row; returns the rows and, per reference, whether it is
    collected with the ``security`` marker."""
    rows, accepted_text = parse(text)
    found: dict[str, bool] = {}
    for row in rows:
        judge(row, accepted_text)
        for ref in row.refs:
            if ref not in found:
                found[ref] = ref_matches(ref, collected)
            if not found[ref]:
                row.problems.append(f"not collected with -m security: {ref}")
    if not rows:
        raise ValueError("no §5 rows found - is this the threat model?")
    return rows, found


def _status_word(status: str) -> str:
    words = []
    if _MITIGATED.match(status):
        words.append("mitigated")
    if _ACCEPTED.search(status):
        words.append("accepted")
    return "+".join(words) or "INVALID"


def report(rows: Sequence[Row], found: dict[str, bool]) -> str:
    lines = [f"{'ID':<7} {'status':<19} {'tests':>5} {'collected':>9}  problems"]
    for row in rows:
        collected = sum(1 for ref in row.refs if found.get(ref))
        lines.append(
            f"{row.row_id:<7} {_status_word(row.status):<19} {len(row.refs):>5} "
            f"{collected:>4}/{len(row.refs):<4}  "
            + ("; ".join(row.problems) if row.problems else "ok")
        )
    bad = [row for row in rows if row.problems]
    mitigated = sum(1 for row in rows if _MITIGATED.match(row.status))
    accepted_only = sum(
        1
        for row in rows
        if _ACCEPTED.search(row.status) and not _MITIGATED.match(row.status)
    )
    lines.append(
        f"{len(rows)} rows: {mitigated} mitigated (some with an accepted residual), "
        f"{accepted_only} accepted, {len(bad)} failing; "
        f"{len(found)} distinct test references"
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--threat-model", type=Path, default=THREAT_MODEL)
    args = parser.parse_args(argv)
    text = args.threat_model.read_text(encoding="utf-8")
    rows, found = check(text, collect_security_ids())
    print(report(rows, found))
    return 1 if any(row.problems for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
