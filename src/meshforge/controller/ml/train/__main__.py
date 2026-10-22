"""``python -m meshforge.controller.ml.train compare|fit ...`` - the processes
``compute planner ml compare`` (V0.15 G2, D353) and the controller's retrain
task (V0.15 G3, D361) run.

    compare --dataset FILE --out DIR [--terms a,b] [--algorithms a,b]
            [--with-family] [--min-rows N] [--min-families N]
            [--min-effect X]
    fit     --dataset FILE --out DIR [--reference DIR] [--terms a,b]
            [--algorithms a,b] [--min-rows N] [--min-families N]
            [--min-effect X] [--max-interval-log X]
            [--family-tolerance-log X]

``fit`` (:mod:`.fit_main`): train the model set on the dataset, run the
activation gate against the ``--reference`` set (or the calibration) and
write ``DIR/set.json`` + one ``DIR/<sha256 hex>.art`` per learned member.

Reads the G1 dataset ``FILE`` (and its sidecar ``FILE.json`` when present),
runs :func:`~.compare.compare` in this process (no server) and writes
``DIR/report.json`` (canonical, D352), ``DIR/report.md`` and
``DIR/timings.json`` (wall times - kept out of the report so the report stays
byte-identical). Prints one JSON line on stdout: ``{"report": ..., "markdown":
..., "seconds": ...}``.

Exit codes: 0 done; **2** = the ``ml`` extra is missing, the dataset's
``dataset_version`` / ``feature_version`` is unknown, or bad arguments;
1 = the file cannot be read or written. Messages go to stderr.

The ML libraries are imported only after the check, so a controller without
the extra gets the clear message, never a traceback.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Final

from meshforge.common.exit_codes import ExitCode
from meshforge.controller.ml.train import EXTRA_HINT, missing_libraries

if TYPE_CHECKING:
    from meshforge.controller.ml.train.compare import CompareSettings

__all__ = ["main"]

_PROG: Final = "python -m meshforge.controller.ml.train"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=_PROG, description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cmp = sub.add_parser("compare", help="compare models per term on a dataset")
    _common(cmp)
    cmp.add_argument("--with-family", action="store_true")
    fit = sub.add_parser("fit", help="train, gate and write a model set (G3)")
    _common(fit)
    fit.add_argument("--reference", type=Path, default=None)
    fit.add_argument("--max-interval-log", type=float, default=None)
    fit.add_argument("--family-tolerance-log", type=float, default=None)
    return parser


def _common(cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument("--dataset", required=True, type=Path)
    cmd.add_argument("--out", required=True, type=Path)
    cmd.add_argument("--terms", default=None)
    cmd.add_argument("--algorithms", default=None)
    cmd.add_argument("--min-rows", type=int, default=None)
    cmd.add_argument("--min-families", type=int, default=None)
    cmd.add_argument(
        "--min-effect",
        type=float,
        default=None,
        help="D357 ml_min_effect_log: `better` needs the CI's upper end below "
        "minus this (default 0.02)",
    )


def _err(message: str) -> None:
    sys.stderr.write(message.rstrip("\n") + "\n")


def _split(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _sidecar(path: Path, digest: str) -> tuple[int | None, str]:
    """``(node_failures, state)`` from ``FILE.json`` (report docstring)."""
    side = path.with_name(path.name + ".json")
    if not side.exists():
        return None, "missing"
    try:
        doc = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "unreadable"
    if not isinstance(doc, dict) or doc.get("digest") != digest:
        return None, "digest_mismatch"
    counts = doc.get("counts")
    value = counts.get("node_failures") if isinstance(counts, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return None, "matched"
    return value, "matched"


def _settings(args: argparse.Namespace) -> CompareSettings:
    """The comparison settings from the arguments; ``ValueError`` for an
    unknown term or algorithm."""
    from meshforge.controller.ml.schema import TERM_NAMES
    from meshforge.controller.ml.train.algorithms import configs_for
    from meshforge.controller.ml.train.compare import CompareSettings

    settings = CompareSettings(with_family=bool(getattr(args, "with_family", False)))
    terms, algorithms = _split(args.terms), _split(args.algorithms)
    if terms is not None:
        unknown = sorted(set(terms) - set(TERM_NAMES))
        if unknown:
            raise ValueError(f"unknown term(s): {', '.join(unknown)}")
        settings = replace(settings, terms=terms)
    if algorithms is not None:
        configs_for(algorithms)  # ValueError for an unknown one
        settings = replace(settings, algorithms=algorithms)
    if args.min_rows is not None:
        settings = replace(settings, min_rows=max(1, args.min_rows))
    if args.min_families is not None:
        settings = replace(settings, min_families=max(1, args.min_families))
    if args.min_effect is not None:
        if not args.min_effect >= 0.0:  # NaN too
            raise ValueError("--min-effect must be >= 0")
        settings = replace(settings, min_effect_log=args.min_effect)
    for name, field in (
        ("max_interval_log", "max_interval_log"),
        ("family_tolerance_log", "family_tolerance_log"),
    ):
        value = getattr(args, name, None)
        if value is not None:
            if not value > 0.0 or value == float("inf"):  # NaN too
                raise ValueError(f"--{name.replace('_', '-')} must be > 0")
            settings = replace(settings, **{field: value})
    return settings


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    missing = missing_libraries()
    if missing:
        _err(f"compare needs {', '.join(missing)} (not installed): {EXTRA_HINT}")
        return int(ExitCode.USAGE_ERROR)
    from meshforge.controller.ml.train.compare import compare
    from meshforge.controller.ml.train.data import (
        DatasetValueError,
        DatasetVersionError,
        load,
    )
    from meshforge.controller.ml.train.report import canonical_json, markdown, report

    try:
        settings = _settings(args)
    except ValueError as exc:
        _err(str(exc))
        return int(ExitCode.USAGE_ERROR)
    if args.command == "fit":
        from meshforge.controller.ml.train.fit_main import run_fit

        return run_fit(args.dataset, args.out, args.reference, settings)
    started = time.monotonic()
    try:
        table = load(args.dataset)
    except DatasetVersionError as exc:
        _err(f"{args.dataset}: {exc}")
        return int(ExitCode.USAGE_ERROR)
    except DatasetValueError as exc:
        _err(f"{args.dataset}: {exc}")
        return int(ExitCode.FAILURE)
    except Exception as exc:  # noqa: BLE001 - any unreadable file is one answer
        _err(f"{args.dataset}: cannot read the dataset ({type(exc).__name__})")
        return int(ExitCode.FAILURE)
    loaded = time.monotonic()
    node_failures, sidecar = _sidecar(args.dataset, table.digest)
    try:
        results = compare(table, settings)
        doc = report(
            table, settings, results, node_failures=node_failures, sidecar=sidecar
        )
    except Exception as exc:  # noqa: BLE001 - one line for the operator, no traceback
        detail = str(exc).splitlines()[0] if str(exc) else "no detail"
        _err(f"{args.dataset}: the comparison failed ({type(exc).__name__}: {detail})")
        return int(ExitCode.FAILURE)
    compared = time.monotonic()
    try:
        args.out.mkdir(parents=True, exist_ok=True)
        json_path = args.out / "report.json"
        md_path = args.out / "report.md"
        json_path.write_text(canonical_json(doc), encoding="utf-8")
        md_path.write_text(markdown(doc), encoding="utf-8")
        timings = {
            "load_s": round(loaded - started, 3),
            "compare_s": round(compared - loaded, 3),
            "total_s": round(time.monotonic() - started, 3),
            "rows": table.n,
        }
        (args.out / "timings.json").write_text(
            json.dumps(timings, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        _err(f"cannot write the report to {args.out}: {exc.strerror or exc}")
        return int(ExitCode.FAILURE)
    sys.stdout.write(
        json.dumps(
            {
                "report": str(json_path),
                "markdown": str(md_path),
                "seconds": timings["total_s"],
            }
        )
        + "\n"
    )
    return int(ExitCode.OK)


if __name__ == "__main__":
    raise SystemExit(main())
