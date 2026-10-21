# ruff: noqa: T201 - a lab tool: its output is the point
"""V0.10 lab kit (roadmap 0.10.10): build a Mode 3 job folder of any size,
check a split job's output, and compute criteria 2-3. The procedure is
``lab/checklists/V0.10-mode3.md``.

    uv run python lab/mode3_check.py build FAMILY DEST [--param K=V ...] [--seed N]
        The benchmark library's job folder for FAMILY (e.g.
        ``split.image_classify``) with these params - **without** the campaign's
        5 s - 3 min runtime window (a lab job may be longer or shorter; the
        params model, input caps and seed range still apply). Submit it with
        ``compute run DEST``. Prints the items, the split spec and the
        single-thread time hint of the whole job on the reference laptop.
    uv run python lab/mode3_check.py verify FAMILY OUTDIR [--param K=V ...] --seed N
        The family's whole-output check of a job's output folder (``compute get
        JOB --dest OUTDIR``): every item once, in order, each value recomputed
        here. Prints ``OK`` or the problems, and the output's content digest
        (equal digests = byte-identical outputs).
    uv run python lab/mode3_check.py report RUNS.parquet CHUNKS.parquet
            --split JOB --single JOB [--single JOB ...] [--out REPORT.json]
        Criteria 2-3 (D274) from the two exports (``compute telemetry export
        [--table chunks]``): per-node items/s, ideal bound, makespan ratio,
        speedup and the verdicts (``lab/_mode3/analysis.py``).

Exit codes: 0 done (verify: OK; report: no criterion FAIL); 2 usage error;
3 verify found problems / report has a FAIL; 4 report INCONCLUSIVE (an input
is missing - never a pass).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

_LAB = Path(__file__).resolve().parent
if str(_LAB) not in sys.path:  # pragma: no cover - direct invocation
    sys.path.insert(0, str(_LAB))

from _mode3.analysis import FAIL, INCONCLUSIVE, evaluate  # noqa: E402

EXIT_OK: Final = 0
EXIT_USAGE: Final = 2
EXIT_PROBLEMS: Final = 3
EXIT_INCONCLUSIVE: Final = 4


def _params(pairs: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"--param wants KEY=VALUE, got {pair!r}")
        try:
            out[key] = json.loads(raw)
        except ValueError:
            out[key] = raw
    return out


def files_digest(files: Mapping[str, bytes]) -> str:
    """sha256 over every (relative path, bytes) in path order."""
    hasher = hashlib.sha256()
    for name in sorted(files):
        for part in (name.encode(), files[name]):
            hasher.update(len(part).to_bytes(8, "little"))
            hasher.update(part)
    return "sha256:" + hasher.hexdigest()


def build(family: str, dest: Path, params: dict[str, Any], seed: int) -> int:
    from meshforge.bench.workloads import base, build_job, get_family

    try:
        fam = get_family(family)
    except LookupError as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_USAGE
    if not getattr(fam, "split", False):
        print(f"{family} is not a split (Mode 3) family", file=sys.stderr)
        return EXIT_USAGE
    saved = base.MIN_RUNTIME_S, base.MAX_RUNTIME_S
    base.MIN_RUNTIME_S, base.MAX_RUNTIME_S = 0.0, float("inf")  # type: ignore[misc]
    try:
        built = build_job(fam, params, seed, dest)
    except ValueError as exc:  # ParamsError / MaterialiseError
        print(f"cannot build: {exc}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        base.MIN_RUNTIME_S, base.MAX_RUNTIME_S = saved  # type: ignore[misc]
    hint = fam.expected_runtime_s(built.params)
    print(f"job folder: {built.path}")
    print(f"family: {fam.name}  params: {json.dumps(built.params, sort_keys=True)}")
    print(f"seed: {seed}  content: {built.content_digest}")
    print(f"split: {json.dumps(built.manifest.model_dump(mode='json').get('split'))}")
    print(f"whole job on the reference laptop, one core: ~{hint:.1f} s")
    return EXIT_OK


def verify(family: str, outdir: Path, params: dict[str, Any], seed: int) -> int:
    from meshforge.bench.workloads import get_family

    try:
        fam = get_family(family)
    except LookupError as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_USAGE
    if not getattr(fam, "split", False):
        print(f"{family} is not a split (Mode 3) family", file=sys.stderr)
        return EXIT_USAGE
    if not outdir.is_dir():
        print(
            f"{outdir} is not a folder (compute get JOB --dest {outdir})",
            file=sys.stderr,
        )
        return EXIT_USAGE
    files = {
        p.relative_to(outdir).as_posix(): p.read_bytes()
        for p in sorted(outdir.rglob("*"))
        if p.is_file()
    }
    verdict = fam.verify_output(files, params, seed)  # type: ignore[attr-defined]
    print(f"output digest: {files_digest(files)}  ({len(files)} file(s))")
    if verdict.ok:
        print("OK - every item once, in order, each value recomputed")
        return EXIT_OK
    for problem in verdict.problems:
        print(f"PROBLEM: {problem}")
    return EXIT_PROBLEMS


def report(
    runs_file: Path,
    chunks_file: Path,
    split: str,
    singles: Sequence[str],
    out: Path | None,
) -> int:
    import pyarrow.parquet as pq

    runs = pq.read_table(runs_file).to_pylist()
    chunks = pq.read_table(chunks_file).to_pylist()
    numbers = evaluate(runs, chunks, split_job_id=split, single_job_ids=singles)
    doc = numbers.as_json()
    for key, value in doc.items():
        if isinstance(value, float):
            print(f"{key}: {value:.3f}")
        else:
            print(f"{key}: {value}")
    if out is not None:
        out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    if FAIL in (numbers.c2, numbers.c3):
        return EXIT_PROBLEMS
    if INCONCLUSIVE in (numbers.c2, numbers.c3):
        return EXIT_INCONCLUSIVE
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mode3_check.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("family")
    b.add_argument("dest", type=Path)
    b.add_argument("--param", action="append", default=[])
    b.add_argument("--seed", type=int, default=1)
    v = sub.add_parser("verify")
    v.add_argument("family")
    v.add_argument("outdir", type=Path)
    v.add_argument("--param", action="append", default=[])
    v.add_argument("--seed", type=int, required=True)
    r = sub.add_parser("report")
    r.add_argument("runs", type=Path)
    r.add_argument("chunks", type=Path)
    r.add_argument("--split", required=True)
    r.add_argument("--single", action="append", required=True)
    r.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.command == "build":
        return build(args.family, args.dest, _params(args.param), args.seed)
    if args.command == "verify":
        return verify(args.family, args.outdir, _params(args.param), args.seed)
    return report(args.runs, args.chunks, args.split, args.single, args.out)


if __name__ == "__main__":
    sys.exit(main())
