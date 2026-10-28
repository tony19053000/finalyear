"""Fail on any untriaged medium/high bandit finding in ``src`` (V1.0 G3, D413).

Runs ``bandit -r src -ll -f json`` (the lock-pinned bandit of the dev group) and
compares every finding with ``scripts/bandit_triage.toml``. A finding is triaged
when an entry has the same ``test_id``, the same file (repository-relative,
``/``-separated) and the same stripped text of the flagged source line. Each
entry must carry a ``reason`` (three or more words) and a ``ref``.

Exit codes: 0 every finding triaged · 1 untriaged findings (printed as
ready-to-paste ``[[finding]]`` blocks) or a malformed triage file · 2 bandit
could not run. Stale entries (no longer matching a finding) are reported and
also fail, so the allow-list never outlives the code it describes.

Usage::

    uv run python scripts/bandit_gate.py [--triage PATH] [--report PATH]

``--report`` reads an existing bandit JSON report instead of running bandit
(used by the unit tests).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

REPO: Final = Path(__file__).resolve().parent.parent
TRIAGE: Final = REPO / "scripts" / "bandit_triage.toml"
_WORD: Final = re.compile(r"[A-Za-z][A-Za-z'-]*[A-Za-z]")
MIN_REASON_WORDS: Final = 3


@dataclass(frozen=True)
class Finding:
    test_id: str
    file: str
    line: str
    severity: str
    line_number: int
    text: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.test_id, self.file, self.line)


class TriageError(Exception):
    """The triage file is malformed."""


def _relative(filename: str, repo: Path) -> str:
    path = Path(filename)
    if not path.is_absolute():
        path = repo / path
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return Path(filename).as_posix()


def _source_line(repo: Path, file: str, number: int) -> str:
    try:
        lines = (repo / file).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    return lines[number - 1].strip() if 0 < number <= len(lines) else ""


def findings_from_report(report: dict[str, Any], repo: Path) -> list[Finding]:
    """The medium/high findings of a bandit JSON report."""
    out: list[Finding] = []
    for result in report.get("results", []):
        severity = str(result.get("issue_severity", "")).upper()
        if severity not in ("MEDIUM", "HIGH"):
            continue
        file = _relative(str(result["filename"]), repo)
        number = int(result["line_number"])
        out.append(
            Finding(
                test_id=str(result["test_id"]),
                file=file,
                line=_source_line(repo, file, number),
                severity=severity,
                line_number=number,
                text=str(result.get("issue_text", "")),
            )
        )
    return out


def load_triage(path: Path) -> set[tuple[str, str, str]]:
    """The triaged keys; :class:`TriageError` for a malformed entry."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries = data.get("finding", [])
    if not isinstance(entries, list):
        raise TriageError("[[finding]] must be an array of tables")
    keys: set[tuple[str, str, str]] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise TriageError(f"entry {index}: not a table")
        missing = [
            name
            for name in ("test_id", "file", "line", "reason", "ref")
            if not isinstance(entry.get(name), str) or not entry[name].strip()
        ]
        if missing:
            raise TriageError(f"entry {index}: missing {', '.join(missing)}")
        words = {w.lower() for w in _WORD.findall(entry["reason"])}
        if len(words) < MIN_REASON_WORDS:
            raise TriageError(f"entry {index}: the reason is not a sentence")
        key = (entry["test_id"], entry["file"], entry["line"].strip())
        if key in keys:
            raise TriageError(f"entry {index}: duplicate of an earlier entry")
        keys.add(key)
    return keys


def run_bandit(repo: Path) -> dict[str, Any]:
    done = subprocess.run(
        [sys.executable, "-m", "bandit", "-r", "src", "-ll", "-q", "-f", "json"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    # bandit exits 1 when it has findings; only unparsable output is an error.
    try:
        report: dict[str, Any] = json.loads(done.stdout)
    except ValueError as exc:
        raise RuntimeError(
            f"bandit exited {done.returncode} without a JSON report: "
            f"{done.stderr.strip()[-2000:]}"
        ) from exc
    if report.get("errors"):
        raise RuntimeError(f"bandit reported errors: {report['errors']}")
    return report


def _block(finding: Finding) -> str:
    line = json.dumps(finding.line)
    return (
        "[[finding]]\n"
        f'test_id = "{finding.test_id}"\n'
        f'file = "{finding.file}"\n'
        f"line = {line}\n"
        'reason = "<why this is safe>"\n'
        'ref = "<decision / ADR / test>"\n'
    )


def gate(
    findings: Iterable[Finding], triaged: set[tuple[str, str, str]]
) -> tuple[list[Finding], list[tuple[str, str, str]]]:
    """(untriaged findings, stale triage keys)."""
    found = list(findings)
    untriaged = [f for f in found if f.key not in triaged]
    seen = {f.key for f in found}
    stale = sorted(k for k in triaged if k not in seen)
    return untriaged, stale


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--triage", type=Path, default=TRIAGE)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--repo", type=Path, default=REPO)
    args = parser.parse_args(argv)
    try:
        triaged = load_triage(args.triage)
    except (TriageError, tomllib.TOMLDecodeError, OSError) as exc:
        print(f"bandit_gate: bad triage file {args.triage}: {exc}", file=sys.stderr)
        return 1
    try:
        if args.report is not None:
            report = json.loads(args.report.read_text(encoding="utf-8"))
        else:
            report = run_bandit(args.repo)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"bandit_gate: {exc}", file=sys.stderr)
        return 2
    findings = findings_from_report(report, args.repo)
    untriaged, stale = gate(findings, triaged)
    for f in findings:
        mark = "UNTRIAGED" if f in untriaged else "triaged"
        print(f"{mark:9} {f.severity:6} {f.test_id} {f.file}:{f.line_number}")
    for key in stale:
        print(f"STALE     {key[0]} {key[1]}: {key[2]!r}")
    if untriaged:
        print("\nadd to scripts/bandit_triage.toml (with a real reason):\n")
        for f in untriaged:
            print(f"# {f.severity} {f.text}")
            print(_block(f))
    ok = not untriaged and not stale
    print(
        f"bandit_gate: {len(findings)} medium/high finding(s), "
        f"{len(untriaged)} untriaged, {len(stale)} stale -> {'OK' if ok else 'FAIL'}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
