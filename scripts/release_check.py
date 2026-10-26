#!/usr/bin/env python3
"""V1.0 release acceptance check (D408, ADR-0023).

Reads ``release/acceptance.yaml`` (D407) and gives every row a status:

* ``auto`` rows - their pytest node ids run (one pytest process per tier and
  flag set, through ``scripts/pytest_exit_guard.py``) and their commands run;
  PASS needs at least one *passed* test per node id and no failure. A node id
  whose tests were all skipped is MISSING (a skip is not evidence).
* ``lab`` rows - the newest evidence entry for the row in ``lab/reports/raw/``
  (``*.json`` files with ``"schema": "meshforge.acceptance-evidence/1"``). PASS
  needs verdict PASS from a release build (a package version, not ``dev``) at
  the target SHA - or at an older SHA when no file matching the row's
  ``affected_by`` globs changed since (D406). Otherwise MISSING (or FAIL).
* ``waiver`` rows - WAIVED when the reason and the cited decision are valid.

Writes ``<out>.md`` and ``<out>.json`` (default
``docs/reports/release-v1.0-acceptance``) and prints the table.

Usage::

    uv run python scripts/release_check.py                     # everything
    uv run python scripts/release_check.py --only auto         # run the tests only
    uv run python scripts/release_check.py --only lab --sha <rc sha> --package-version 1.0.0-rc.2
    uv run python scripts/release_check.py --rows V0.6-c2 V0.4-c3 --out /tmp/x
    uv run python scripts/release_check.py record-lab V0.3-c2 --verdict PASS \\
        --package-version 1.0.0-rc.1 --sha <rc sha> --evidence lab/reports/raw/<file> \\
        --detail "OFFLINE after 6.1 s, ONLINE after 3.4 s (Ethernet)"

Exit status: 0 when no evaluated row is FAIL or MISSING (with ``--only K`` only
rows of kind K count; the other rows are still listed), 1 otherwise, 2 for a
usage error or an invalid manifest.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_manifest as rm

EVIDENCE_SCHEMA: Final = "meshforge.acceptance-evidence/1"
DEFAULT_OUT: Final = rm.REPO / "docs" / "reports" / "release-v1.0-acceptance"
DEFAULT_LAB_DIR: Final = rm.REPO / "lab" / "reports" / "raw"
GUARD: Final = rm.REPO / "scripts" / "pytest_exit_guard.py"
PLUGIN: Final = "release_outcomes_plugin"
OUTCOMES_ENV: Final = "MESHFORGE_RELEASE_OUTCOMES"
"""Read by ``scripts/release_outcomes_plugin.py`` (same name there)."""
OUTCOMES: Final = ("passed", "skipped", "failed", "error")
STATUSES: Final = ("PASS", "FAIL", "MISSING", "WAIVED", "NOT_RUN")
VERDICTS: Final = ("PASS", "FAIL")
#: pytest exit codes that still mean "the run finished": 0 all passed, 1 some
#: failed (the per-item outcomes say which), 5 nothing collected.
_PYTEST_FINISHED: Final = frozenset({0, 1, 5})


@dataclass
class Result:
    id: str
    kind: str
    status: str
    detail: str
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LabEntry:
    row: str
    verdict: str
    sha: str
    package_version: str
    recorded_at: str
    detail: str
    source: str


# --------------------------------------------------------------------- git


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # fixed git argv in the repo
        ["git", *args],  # noqa: S607 - git from PATH, like every dev tool here
        cwd=rm.REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def resolve_sha(ref: str) -> str | None:
    result = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return result.stdout.strip() if result.returncode == 0 else None


def tree_is_dirty() -> bool:
    return bool(_git("status", "--porcelain", "--untracked-files=no").stdout.strip())


def changed_files(old: str, new: str) -> list[str] | None:
    result = _git("diff", "--name-only", old, new)
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


def affected(files: Iterable[str], globs: Sequence[str]) -> list[str]:
    """Files matching any glob (fnmatch: ``*`` also crosses ``/``)."""
    return sorted(f for f in files if any(fnmatch.fnmatchcase(f, g) for g in globs))


# --------------------------------------------------------------------- auto


def run_pytest(
    node_ids: Sequence[str], flags: Sequence[str], outcomes_path: Path
) -> tuple[int, dict[str, dict[str, str]]]:
    """Run pytest on ``node_ids`` and return (exit code, per-item outcomes)."""
    env = dict(os.environ)
    env[OUTCOMES_ENV] = str(outcomes_path)
    scripts_dir = str(rm.REPO / "scripts")
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (scripts_dir, env.get("PYTHONPATH")) if p
    )
    argv = [
        sys.executable,
        str(GUARD),
        "-q",
        "-p",
        PLUGIN,
        "-p",
        "no:cacheprovider",
        *flags,
        *node_ids,
    ]
    print(f"$ pytest {' '.join([*flags, *node_ids])}", flush=True)
    code = subprocess.run(argv, cwd=rm.REPO, env=env, check=False).returncode
    try:
        outcomes = json.loads(outcomes_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        outcomes = {}
    return code, outcomes


def run_command(cmd: Sequence[str]) -> tuple[int, str]:
    print(f"$ python {' '.join(cmd)}", flush=True)
    result = subprocess.run(
        [sys.executable, *cmd],
        cwd=rm.REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    tail = (result.stdout + result.stderr).strip().splitlines()[-3:]
    return result.returncode, " | ".join(tail)


def judge_auto(
    row: rm.Row,
    outcomes: Mapping[str, Mapping[str, str]],
    crashed: Mapping[str, int],
    command_results: Mapping[tuple[str, ...], tuple[int, str]],
) -> Result:
    """Status of one auto row from the item outcomes of its node ids."""
    problems: list[str] = []
    missing: list[str] = []
    summary: list[str] = []
    for node_id in row.tests:
        mine = {k: v for k, v in outcomes.items() if rm.matches(node_id, k)}
        counts = {
            o: sum(1 for v in mine.values() if v["outcome"] == o) for o in OUTCOMES
        }
        if code := crashed.get(node_id):
            problems.append(f"{node_id}: pytest exited {code}")
        if not mine:
            problems.append(f"{node_id}: no test ran")
            continue
        bad = [k for k, v in mine.items() if v["outcome"] in ("failed", "error")]
        if bad:
            problems.append(f"{len(bad)} failed: {', '.join(sorted(bad)[:3])}")
        elif counts["passed"] == 0:
            reasons = sorted({v["reason"] for v in mine.values()})
            missing.append(f"{node_id}: all skipped ({'; '.join(reasons)[:200]})")
        summary.append(
            f"{counts['passed']}p/{counts['skipped']}s"
            + (f"/{len(bad)}f" if bad else "")
        )
    for cmd in row.commands:
        code, tail = command_results[cmd]
        if code != 0:
            problems.append(f"`{' '.join(cmd)}` exited {code}: {tail}")
        else:
            summary.append(f"`{cmd[0]}` ok")
    if problems:
        return Result(row.id, row.kind, "FAIL", "; ".join(problems), list(row.tests))
    if missing:
        return Result(row.id, row.kind, "MISSING", "; ".join(missing), list(row.tests))
    return Result(row.id, row.kind, "PASS", " ".join(summary), list(row.tests))


def evaluate_auto(rows: Sequence[rm.Row], workdir: Path) -> list[Result]:
    groups: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for row in rows:
        for node_id in row.tests:
            key = (rm.tier_of(node_id), tuple(sorted(row.flags)))
            if node_id not in groups.setdefault(key, []):
                groups[key].append(node_id)
    outcomes: dict[str, dict[str, str]] = {}
    crashed: dict[str, int] = {}
    order = ["unit", "component", "cluster", "chaos", "e2e"]
    ranked = sorted(
        groups.items(),
        key=lambda kv: (order.index(kv[0][0]) if kv[0][0] in order else 9, kv[0]),
    )
    for index, ((tier, flags), node_ids) in enumerate(ranked):
        print(f"\n== {tier} {' '.join(flags)} ({len(node_ids)} node ids)", flush=True)
        code, got = run_pytest(node_ids, flags, workdir / f"outcomes-{index}.json")
        outcomes.update(got)
        if code not in _PYTEST_FINISHED:
            crashed.update(dict.fromkeys(node_ids, code))
    command_results: dict[tuple[str, ...], tuple[int, str]] = {}
    for row in rows:
        for cmd in row.commands:
            if cmd not in command_results:
                command_results[cmd] = run_command(cmd)
    return [judge_auto(row, outcomes, crashed, command_results) for row in rows]


# --------------------------------------------------------------------- lab


def load_lab_evidence(lab_dir: Path) -> tuple[dict[str, list[LabEntry]], list[str]]:
    """Every evidence entry under ``lab_dir`` by row id, plus warnings for files
    that claim the schema but are malformed (other JSON files are ignored)."""
    entries: dict[str, list[LabEntry]] = {}
    warnings: list[str] = []
    if not lab_dir.is_dir():
        return entries, warnings
    for path in sorted(lab_dir.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("schema") != EVIDENCE_SCHEMA:
            continue
        rel = (
            path.relative_to(rm.REPO).as_posix()
            if path.is_relative_to(rm.REPO)
            else str(path)
        )
        results = data.get("results")
        if not isinstance(results, list):
            warnings.append(f"{rel}: 'results' must be a list")
            continue
        for i, raw in enumerate(results):
            entry = _lab_entry(raw, rel)
            if isinstance(entry, str):
                warnings.append(f"{rel} results[{i}]: {entry}")
            else:
                entries.setdefault(entry.row, []).append(entry)
    return entries, warnings


def _lab_entry(raw: object, source: str) -> LabEntry | str:
    if not isinstance(raw, dict):
        return "not a mapping"
    fields = ("row", "verdict", "sha", "package_version", "recorded_at")
    for name in fields:
        if not isinstance(raw.get(name), str) or not raw[name].strip():
            return f"missing {name}"
    if raw["verdict"] not in VERDICTS:
        return f"verdict must be one of {VERDICTS}"
    try:
        dt.datetime.fromisoformat(raw["recorded_at"])
    except ValueError:
        return "recorded_at is not ISO 8601"
    detail = raw.get("detail", "")
    return LabEntry(
        row=raw["row"],
        verdict=raw["verdict"],
        sha=raw["sha"],
        package_version=raw["package_version"],
        recorded_at=raw["recorded_at"],
        detail=detail if isinstance(detail, str) else "",
        source=source,
    )


def _recorded(entry: LabEntry) -> dt.datetime:
    when = dt.datetime.fromisoformat(entry.recorded_at)
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


def judge_lab(row: rm.Row, entries: Sequence[LabEntry], target_sha: str) -> Result:
    if not entries:
        return Result(
            row.id, row.kind, "MISSING", f"no evidence yet (lab section {row.section})"
        )
    entry = max(entries, key=_recorded)
    ev = [entry.source]
    where = f"{entry.package_version} @ {entry.sha[:12]}, {entry.recorded_at}"
    if entry.package_version.strip().lower() in ("dev", "unknown", "none"):
        return Result(
            row.id, row.kind, "MISSING", f"not from a release build ({where}; D406)", ev
        )
    if entry.verdict == "FAIL":
        return Result(row.id, row.kind, "FAIL", f"{where}: {entry.detail}", ev)
    evidence_sha = resolve_sha(entry.sha)
    if evidence_sha is None:
        return Result(
            row.id, row.kind, "MISSING", f"evidence SHA unknown to git ({where})", ev
        )
    if evidence_sha != target_sha:
        changed = changed_files(evidence_sha, target_sha)
        if changed is None:
            return Result(row.id, row.kind, "MISSING", f"cannot diff {where}", ev)
        if hits := affected(changed, row.affected_by):
            shown = ", ".join(hits[:3]) + (" ..." if len(hits) > 3 else "")
            return Result(
                row.id,
                row.kind,
                "MISSING",
                f"stale ({where}): changed since: {shown}",
                ev,
            )
        where += " (no affected file changed since)"
    return Result(row.id, row.kind, "PASS", f"{where}: {entry.detail}".rstrip(": "), ev)


# --------------------------------------------------------------------- report


def judge_waiver(row: rm.Row) -> Result:
    return Result(
        row.id, row.kind, "WAIVED", f"{', '.join(row.decision)}: {row.reason}"
    )


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(header: Mapping[str, object], results: Sequence[Result]) -> str:
    counts = {s: sum(1 for r in results if r.status == s) for s in STATUSES}
    lines = [
        "# MeshForge v1.0 — release acceptance report",
        "",
        "Generated by `scripts/release_check.py` from `release/acceptance.yaml` (D407/D408,"
        " ADR-0023). Do not edit by hand.",
        "",
        "| | |",
        "|---|---|",
    ]
    lines += [f"| {k} | {_cell(str(v))} |" for k, v in header.items()]
    lines += [
        "",
        "**Totals:** " + " · ".join(f"{s} {counts[s]}" for s in STATUSES if counts[s]),
        "",
        "| Row | Kind | Status | Detail |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {r.id} | {r.kind} | **{r.status}** | {_cell(r.detail)} |" for r in results
    ]
    return "\n".join(lines) + "\n"


def render_table(results: Sequence[Result]) -> str:
    width = max(len(r.id) for r in results)
    out = [f"{'ROW':<{width}}  {'KIND':<6}  {'STATUS':<7}  DETAIL"]
    out += [
        f"{r.id:<{width}}  {r.kind:<6}  {r.status:<7}  {r.detail[:110]}"
        for r in results
    ]
    return "\n".join(out)


def exit_code(results: Sequence[Result], only: str | None) -> int:
    counted = [r for r in results if only is None or r.kind == only]
    return 1 if any(r.status in ("FAIL", "MISSING") for r in counted) else 0


# --------------------------------------------------------------------- CLI


def check(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="release_check.py", description=__doc__.split("\n")[0]
    )
    parser.add_argument(
        "--only", choices=("auto", "lab"), help="evaluate one kind only"
    )
    parser.add_argument(
        "--sha", help="the release SHA the report is for (default HEAD)"
    )
    parser.add_argument("--package-version", default="", help="e.g. 1.0.0-rc.2 (D406)")
    parser.add_argument("--rows", nargs="+", metavar="ID", help="only these row ids")
    parser.add_argument("--manifest", type=Path, default=rm.MANIFEST)
    parser.add_argument("--lab-dir", type=Path, default=DEFAULT_LAB_DIR)
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="report path prefix"
    )
    args = parser.parse_args(argv)

    try:
        manifest = rm.load_manifest(args.manifest)
    except rm.ManifestError as exc:
        print(f"release_check: {exc}", file=sys.stderr)
        return 2
    criteria = rm.parse_roadmap(rm.ROADMAP.read_text(encoding="utf-8"))
    if problems := rm.validate(manifest, criteria):
        print(
            "release_check: invalid manifest:\n  " + "\n  ".join(problems),
            file=sys.stderr,
        )
        return 2
    head = resolve_sha("HEAD")
    target = resolve_sha(args.sha) if args.sha else head
    if target is None or head is None:
        print(
            f"release_check: cannot resolve SHA {args.sha or 'HEAD'}", file=sys.stderr
        )
        return 2
    if target != head and args.only != "lab":
        print(
            f"release_check: --sha {target[:12]} is not the checked-out HEAD {head[:12]}; "
            "check it out to run the auto rows (or use --only lab)",
            file=sys.stderr,
        )
        return 2
    rows = list(manifest.rows)
    if args.rows:
        unknown = sorted(set(args.rows) - {r.id for r in rows})
        if unknown:
            print(f"release_check: unknown rows {unknown}", file=sys.stderr)
            return 2
        rows = [r for r in rows if r.id in set(args.rows)]

    results: dict[str, Result] = {}
    auto_rows = [r for r in rows if r.kind == "auto"]
    if args.only in (None, "auto") and auto_rows:
        with tempfile.TemporaryDirectory(prefix="mf-release-") as tmp:
            for res in evaluate_auto(auto_rows, Path(tmp)):
                results[res.id] = res
    else:
        for row in auto_rows:
            results[row.id] = Result(row.id, row.kind, "NOT_RUN", "--only lab")
    entries, warnings = load_lab_evidence(args.lab_dir)
    for row in rows:
        if row.kind == "lab":
            results[row.id] = judge_lab(row, entries.get(row.id, []), target)
        elif row.kind == "waiver":
            results[row.id] = judge_waiver(row)
    ordered = [results[r.id] for r in rows]

    dirty = tree_is_dirty()
    code = exit_code(ordered, args.only)
    header: dict[str, object] = {
        "SHA": target,
        "Package version": args.package_version or "(not given: source checkout)",
        "Working tree": "DIRTY - not release evidence (D406)" if dirty else "clean",
        "Mode": f"--only {args.only}" if args.only else "all kinds",
        "Rows": f"{len(ordered)}" + (" (subset)" if args.rows else ""),
        "Generated": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "Exit": code,
    }
    if warnings:
        header["Evidence warnings"] = "; ".join(warnings)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    md = args.out.with_suffix(".md")
    js = args.out.with_suffix(".json")
    md.write_text(render_markdown(header, ordered), encoding="utf-8")
    js.write_text(
        json.dumps(
            {
                "schema": "meshforge.acceptance-report/1",
                "sha": target,
                "package_version": args.package_version or None,
                "dirty": dirty,
                "only": args.only,
                "exit": code,
                "warnings": warnings,
                "results": [asdict(r) for r in ordered],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print()
    print(render_table(ordered))
    counts = {s: sum(1 for r in ordered if r.status == s) for s in STATUSES}
    print("\n" + " · ".join(f"{s} {n}" for s, n in counts.items() if n))
    for warning in warnings:
        print(f"warning: {warning}")
    print(f"report: {md} / {js.name}; exit {code}")
    return code


def record_lab(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="release_check.py record-lab",
        description="Write a lab evidence file for one manifest row (D406/D408).",
    )
    parser.add_argument("row")
    parser.add_argument("--verdict", required=True, choices=VERDICTS)
    parser.add_argument(
        "--package-version", required=True, help="installed rc, e.g. 1.0.0-rc.1"
    )
    parser.add_argument("--sha", default="HEAD", help="git SHA the rc was built from")
    parser.add_argument(
        "--evidence", nargs="*", default=[], help="files backing the verdict"
    )
    parser.add_argument("--detail", default="", help="one line: the measured numbers")
    parser.add_argument("--manifest", type=Path, default=rm.MANIFEST)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_LAB_DIR / "acceptance")
    args = parser.parse_args(argv)
    try:
        rows = rm.load_manifest(args.manifest).by_id()
    except rm.ManifestError as exc:
        print(f"record-lab: {exc}", file=sys.stderr)
        return 2
    row = rows.get(args.row)
    if row is None or row.kind != "lab":
        print(
            f"record-lab: {args.row} is not a lab row of the manifest", file=sys.stderr
        )
        return 2
    sha = resolve_sha(args.sha)
    if sha is None:
        print(f"record-lab: unknown SHA {args.sha}", file=sys.stderr)
        return 2
    if args.package_version.strip().lower() in ("dev", "unknown", "none"):
        print(
            "record-lab: evidence must come from a release package (D406)",
            file=sys.stderr,
        )
        return 2
    missing = [p for p in args.evidence if not Path(p).exists()]
    if missing:
        print(f"record-lab: evidence files not found: {missing}", file=sys.stderr)
        return 2
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    entry = {
        "row": row.id,
        "verdict": args.verdict,
        "sha": sha,
        "package_version": args.package_version,
        "recorded_at": now.isoformat(),
        "detail": args.detail,
        "evidence": args.evidence,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%dT%H%M%SZ")
    path = args.out_dir / f"{stamp}-V1.0-{row.id}-{args.verdict}.json"
    path.write_text(
        json.dumps({"schema": EVIDENCE_SCHEMA, "results": [entry]}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "record-lab":
        return record_lab(args[1:])
    return check(args)


if __name__ == "__main__":
    sys.exit(main())
