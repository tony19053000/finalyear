"""V0.7 G2 tester, checklist 21: "every security claim in a doc names its test".

Every ``tests/....py::test_name`` (and the ``::test_name`` shorthand that follows a
file name) cited by a THREAT_MODEL row that mentions G2 must exist; a ``*`` suffix
is a prefix match. A renamed or deleted test then breaks this, not silently the doc.
"""

# ruff: noqa: PIE810  (tester file: adversarial literals, blunt asserts)

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "THREAT_MODEL.md"
_FILE = re.compile(r"`?(tests/[\w/.\-]+\.py)`?")
_NAME = re.compile(r"::(test_\w+\*?)")


def _cited() -> list[tuple[str, str, str]]:
    cited: list[tuple[str, str, str]] = []
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or "G2" not in line:
            continue
        row = line.split("|")[1].strip()
        current: str | None = None
        for token in re.finditer(r"(tests/[\w/.\-]+\.py)|::(test_\w+\*?)", line):
            if token.group(1):
                current = token.group(1)
            elif current is not None:
                cited.append((row, current, token.group(2)))
    return cited


def test_threat_model_g2_rows_cite_tests_that_exist() -> None:
    cited = _cited()
    assert len(cited) >= 15, "expected the enrolment rows to cite many tests"
    missing = []
    for row, file, name in cited:
        path = ROOT / file
        if not path.is_file():
            missing.append((row, file, name, "no such file"))
            continue
        source = path.read_text(encoding="utf-8")
        pattern = (
            rf"def {re.escape(name[:-1])}\w*\("
            if name.endswith("*")
            else rf"def {name}\("
        )
        if not re.search(pattern, source):
            missing.append((row, file, name, "no such test"))
    assert not missing, missing


def test_every_cited_g2_row_is_mitigated_or_explicitly_accepted() -> None:
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if (
            line.startswith("| EN-")
            or line.startswith("| CG-S4")
            or line.startswith("| CG-S5")
        ):
            assert "mitigated" in line or "accepted" in line, line[:60]
