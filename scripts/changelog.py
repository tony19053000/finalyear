"""Changelog tooling (V1.0 G5, D421): draft from the roadmap, check the result.

    uv run python scripts/changelog.py draft [V0.10 ...]   > draft.md
        A Keep-a-Changelog draft per roadmap version, generated from
        docs/DEVELOPMENT_ROADMAP.md (each version's tasks and their states,
        its deferral banner, the ADRs it names) and the decision log of its
        brief (docs/briefs/V<version>-*.md, the D-numbers). The draft is raw
        material: docs/CHANGELOG.md is written from it **by hand** (D421).
    uv run python scripts/changelog.py check
        docs/CHANGELOG.md is well formed (``## [Unreleased]`` or
        ``## [x.y.z] - YYYY-MM-DD`` release headings, Keep-a-Changelog change
        types) and covers every roadmap version: each ``V0.x`` / ``V1.0`` is
        named in a ``#### V...`` heading, and a deferred version says so.

Exit codes: 0 ok; 1 the check found problems; 2 usage / a file is unreadable.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parent.parent
ROADMAP: Final = REPO / "docs" / "DEVELOPMENT_ROADMAP.md"
BRIEFS: Final = REPO / "docs" / "briefs"
CHANGELOG: Final = REPO / "docs" / "CHANGELOG.md"

CHANGE_TYPES: Final = (
    "Added",
    "Changed",
    "Deprecated",
    "Removed",
    "Fixed",
    "Security",
    # project additions, stated in the file's header (D421 release notes)
    "Deferred",
    "Known issues",
    "Accepted risks",
)
_VERSION: Final = re.compile(r"^## (V\d+\.\d+) — (.+)$", re.M)
_TASK: Final = re.compile(
    r"^- \[(?P<state>[ x~])\] (?P<strike>~~)?\*\*(?P<id>\d+\.\d+\.\d+)\*\*(?:~~)? (?P<text>.+)$"
)
_BANNER: Final = re.compile(r"^> \*\*DEFERRED\b", re.M)
_LEAD: Final = re.compile(r"^(?:\*\*DEFERRED[^*]*\*\*\s*)?(?:—\s*)?(?:\([^)]*\)\s*)?")
_DECISION: Final = re.compile(r"^\| \*{0,2}(D\d+)\b", re.M)
_ADR: Final = re.compile(r"ADR-(\d{4})")
_RELEASE: Final = re.compile(
    r"^## (\[Unreleased\]|\[\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?\] - \d{4}-\d{2}-\d{2})$"
)
STATE_WORD: Final = {"x": "done", "~": "in progress", " ": "not started"}


@dataclass
class Task:
    id: str
    state: str
    title: str


@dataclass
class Version:
    id: str
    title: str
    deferred: bool
    tasks: list[Task] = field(default_factory=list)
    deferred_tasks: list[Task] = field(default_factory=list)
    adrs: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)


def _title(text: str) -> str:
    """A task's own words: up to its evidence / status note, markup removed."""
    text = _LEAD.sub("", text, count=1)
    for cut in (
        " — *",
        " — **",
        " - *evidence",
        " *(",
        " — ",
        " Done 20",
        ". Same evidence",
    ):
        if cut in text:
            text = text.split(cut, 1)[0]
    text = text.replace("**", "").replace("🧑", "(lab)").strip()
    return text if len(text) <= 160 else text[:157].rstrip() + "..."


def parse_roadmap(text: str) -> list[Version]:
    """Every ``## V...`` section of the roadmap, in order."""
    heads = list(_VERSION.finditer(text))
    versions = []
    for i, head in enumerate(heads):
        end = (
            heads[i + 1].start()
            if i + 1 < len(heads)
            else text.find("\n## ", head.end())
        )
        body = text[head.end() : end if end != -1 else len(text)]
        version = Version(head[1], head[2].strip(), bool(_BANNER.search(body)))
        for line in body.splitlines():
            m = _TASK.match(line)
            if not m:
                continue
            task = Task(m["id"], m["state"], _title(m["text"]))
            if m["strike"] or m["text"].startswith("**DEFERRED"):
                version.deferred_tasks.append(task)
            else:
                version.tasks.append(task)
        version.adrs = sorted(set(_ADR.findall(body)))
        versions.append(version)
    return versions


def brief_decisions(version: str, briefs: Path = BRIEFS) -> list[str]:
    """The D-numbers of the version's brief decision tables (sorted, unique)."""
    found: set[str] = set()
    for path in sorted(briefs.glob(f"{version}-*.md")):
        found.update(_DECISION.findall(path.read_text(encoding="utf-8")))
    return sorted(found, key=lambda d: int(d[1:]))


def _ranges(decisions: Sequence[str]) -> str:
    nums = sorted(int(d[1:]) for d in decisions)
    if not nums:
        return "-"
    parts, start, prev = [], nums[0], nums[0]
    for n in [*nums[1:], None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(f"D{start}" if start == prev else f"D{start}-D{prev}")
        if n is not None:
            start = prev = n
    return ", ".join(parts)


def draft(versions: Sequence[Version]) -> str:
    lines = [
        "<!-- Draft from scripts/changelog.py draft: edit by hand into docs/CHANGELOG.md -->",
        "",
    ]
    for v in versions:
        lines += [f"#### {v.id} - {v.title}", ""]
        if v.deferred:
            lines += ["Deferred (ADR-0012): designed, not built in this release.", ""]
        groups = {
            "x": "Added",
            "~": "In progress (open lab step or CI)",
            " ": "Not done",
        }
        for state, label in groups.items():
            tasks = [t for t in v.tasks if t.state == state]
            if tasks and not (v.deferred and state == " "):
                lines += [f"{label}:", ""]
                lines += [f"- {t.id} {t.title}" for t in tasks]
                lines.append("")
        if v.deferred_tasks:
            lines += ["Deferred tasks (ADR-0012):", ""]
            lines += [f"- {t.id} {t.title}" for t in v.deferred_tasks]
            lines.append("")
        lines.append(f"Decisions: {_ranges(v.decisions)}")
        if v.adrs:
            lines.append("ADRs: " + ", ".join(f"ADR-{a}" for a in v.adrs))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def check(changelog: str, versions: Sequence[Version]) -> list[str]:
    """What is wrong with ``changelog`` (empty = fine)."""
    problems: list[str] = []
    releases = [line for line in changelog.splitlines() if line.startswith("## ")]
    if not releases:
        problems.append("no release heading (## [Unreleased] or ## [x.y.z] - date)")
    problems += [
        f"bad release heading {line!r}" for line in releases if not _RELEASE.match(line)
    ]
    for line in changelog.splitlines():
        if line.startswith("### ") and line[4:].strip() not in CHANGE_TYPES:
            problems.append(
                f"unknown change type {line!r} (Keep a Changelog: {CHANGE_TYPES})"
            )
    headed = {
        m[1]: m.group(0)
        for m in re.finditer(r"^#### (V\d+\.\d+)\b.*$", changelog, re.M)
    }
    for v in versions:
        if v.id not in headed:
            problems.append(f"{v.id} ({v.title}) has no '#### {v.id}' entry")
        elif v.deferred:
            start = changelog.index(headed[v.id])
            nxt = changelog.find("\n#### ", start + 1)
            entry = changelog[start : nxt if nxt != -1 else len(changelog)]
            if "deferred" not in entry.lower():
                problems.append(
                    f"{v.id} is deferred in the roadmap; its entry must say so"
                )
    return problems


def load_versions(roadmap: Path = ROADMAP, briefs: Path = BRIEFS) -> list[Version]:
    versions = parse_roadmap(roadmap.read_text(encoding="utf-8"))
    for v in versions:
        v.decisions = brief_decisions(v.id, briefs)
    return versions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("draft", help="print a draft per roadmap version")
    d.add_argument("versions", nargs="*", help="e.g. V0.10 (default: every version)")
    c = sub.add_parser("check", help="check docs/CHANGELOG.md")
    c.add_argument("--changelog", type=Path, default=CHANGELOG)
    args = parser.parse_args(argv)
    try:
        versions = load_versions()
    except OSError as exc:
        print(f"changelog: {exc}", file=sys.stderr)
        return 2
    if args.command == "draft":
        wanted = set(args.versions)
        unknown = wanted - {v.id for v in versions}
        if unknown:
            print(f"changelog: unknown version(s) {sorted(unknown)}", file=sys.stderr)
            return 2
        sys.stdout.write(draft([v for v in versions if not wanted or v.id in wanted]))
        return 0
    try:
        text = args.changelog.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"changelog: {exc}", file=sys.stderr)
        return 2
    problems = check(text, versions)
    for problem in problems:
        print(f"changelog: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
