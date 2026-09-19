"""Spike S7 (roadmap 0.2.11): hardware scores of one laptop - CPU GFLOPS,
memory bandwidth, GPU fp32/fp16/bf16 TFLOPS.

One command per laptop, then one command anywhere to build the comparison::

    # on every laptop (plugged in, performance mode):
    uv run python lab/spikes/s7_hardware.py run
    # smoke test only (small sizes; never a real measurement):
    uv run python lab/spikes/s7_hardware.py run --quick
    # after collecting every host's JSON in one folder:
    uv run python lab/spikes/s7_hardware.py report \\
        --results lab/reports/raw/2026-09-20-S7-hardware \\
        --out lab/reports/2026-09-20-S7-hardware.md --labels L1=host1,L2=host2

``run`` measures this machine with ``--warmup`` + ``--reps`` repetitions per
case and writes one result envelope to
``lab/reports/raw/<today>-S7-hardware/<host>.json``. Every benchmark runs in a
child process, so a broken CUDA stack is recorded as an error instead of
killing the run, and the whole run is bounded by ``--timeout``.

The scores are the hardware inputs of the planner cost model (ARCHITECTURE §7):
``extra.planner_fields`` in the JSON names the exact fields it reads.

Exit codes: ``0`` every case complete; ``1`` cannot write the output; ``2``
usage error; ``3`` results written but incomplete (a backend is missing, a case
failed, the budget ran out - see ``errors`` in the JSON); ``130`` interrupted.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

_LAB_DIR: Final = Path(__file__).resolve().parents[1]
if str(_LAB_DIR) not in sys.path:
    sys.path.insert(0, str(_LAB_DIR))

from spikes._harness import MIN_VALID_REPS  # noqa: E402
from spikes._harness.envelope import (  # noqa: E402
    LINKS,
    RAW_ROOT,
    ResultCollisionError,
    load_dir,
    raw_dir,
    raw_result_path,
    write_envelope,
)
from spikes._harness.report import (  # noqa: E402
    TEMPLATE_PATH,
    TemplateError,
    render_report,
)
from spikes._harness.reps import JSON  # noqa: E402
from spikes._s7 import SPIKE, TOPIC, sizing  # noqa: E402
from spikes._s7.report import is_score, render_scoreboard  # noqa: E402
from spikes._s7.run import RunConfig, run_scores  # noqa: E402

EXIT_OK: Final = 0
EXIT_IO: Final = 1
EXIT_USAGE: Final = 2
EXIT_INCOMPLETE: Final = 3
EXIT_INTERRUPTED: Final = 130
MIN_BUDGET_S: Final = 10.0
MAX_BUDGET_S: Final = 24 * 3600.0
_DAY_DIR_RE: Final = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
_NAME_RE: Final = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _say(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


# ------------------------------------------------------------ argument types
def _number(
    lo: float, hi: float, *, integer: bool = False
) -> Callable[[str], float | int]:
    def parse(text: str) -> float | int:
        try:
            value: float | int = int(text) if integer else float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
        if not lo <= value <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo:g} and {hi:g}")
        return value

    return parse


def _name(text: str) -> str:
    if not _NAME_RE.match(text):
        raise argparse.ArgumentTypeError("use 1-64 characters from A-Z a-z 0-9 . _ -")
    return text


def _labels(text: str) -> dict[str, str]:
    """``L1=host1,L2=host2`` -> ``{"host1": "L1", "host2": "L2"}``."""
    out: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        label, sep, host = part.partition("=")
        if not sep or not label.strip() or not host.strip():
            raise argparse.ArgumentTypeError(f"expected LABEL=HOST, got {part!r}")
        out[host.strip()] = label.strip()
    return out


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lab/spikes/s7_hardware.py",
        description="Spike S7: CPU / memory / GPU hardware scores of this laptop.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="measure this machine and write its result JSON")
    run.add_argument(
        "--quick",
        action="store_true",
        help="smoke run: smaller sizes and less warmup, marked quick=true in the "
        "JSON so it can never be mistaken for a real measurement",
    )
    run.add_argument(
        "--reps",
        type=_number(1, 1000, integer=True),
        default=0,
        help=f"measured repetitions per case (>= {MIN_VALID_REPS} to be valid; "
        "default: 5)",
    )
    run.add_argument(
        "--warmup",
        type=_number(0, 100, integer=True),
        default=-1,
        help="warmup repetitions per case, kept in the JSON but not summarised "
        "(default: 2, quick: 1)",
    )
    run.add_argument(
        "--timeout",
        type=_number(MIN_BUDGET_S, MAX_BUDGET_S),
        default=0.0,
        metavar="SECONDS",
        help="wall-clock budget for the whole run (default 1800, quick 600). "
        "Cases that no longer fit are recorded as budget_exhausted errors.",
    )
    run.add_argument(
        "--pause",
        type=_number(0, 60),
        default=0.2,
        metavar="SECONDS",
        help="pause between repetitions (default %(default)g)",
    )
    run.add_argument("--skip-gpu", action="store_true", help="do not touch the GPU")
    run.add_argument(
        "--device",
        type=_number(0, 15, integer=True),
        default=0,
        metavar="INDEX",
        help="CUDA device index to measure (default 0)",
    )
    run.add_argument(
        "--link",
        choices=LINKS,
        default="ethernet",
        help="not used by S7 (no network traffic); recorded only because the "
        "shared result envelope requires it",
    )
    run.add_argument(
        "--name", type=_name, help="identity shown in the report (default: hostname)"
    )
    run.add_argument(
        "--out-dir",
        type=Path,
        help=f"result folder (default {RAW_ROOT.name}/<today>-{TOPIC} under lab/reports)",
    )

    report = sub.add_parser(
        "report", help="build the comparison report from every laptop's result JSON"
    )
    report.add_argument(
        "--results",
        action="append",
        required=True,
        type=Path,
        metavar="DIR",
        help="folder with S7 result JSONs (repeatable)",
    )
    report.add_argument(
        "--out", required=True, type=Path, help="markdown report to write"
    )
    report.add_argument(
        "--labels",
        type=_labels,
        default={},
        help="short names, e.g. L1=hostA,L2=hostB; laptops named here but "
        "missing a result file show up as TBD",
    )
    report.add_argument(
        "--date",
        type=date.fromisoformat,
        help="report date YYYY-MM-DD (default: from the folder name, else today)",
    )
    report.add_argument(
        "--template", type=Path, default=TEMPLATE_PATH, help=argparse.SUPPRESS
    )
    return parser


# ------------------------------------------------------------------------- run
def _fmt(value: object, digits: int) -> str:
    return f"{value:.{digits}f}" if isinstance(value, int | float) else "—"


def print_summary(envelope: dict[str, JSON], reps: int) -> None:
    """Loud, human-readable summary on stderr (warnings last, so they are seen)."""
    cases = envelope["cases"]
    extra = envelope["extra"]
    _say(f"--- S7 hardware scores: {envelope['host']['label']} ---")
    for case_id in sorted(cases):
        case = cases[case_id]
        summary = case["summary"]
        units = case["metrics"]
        n_ok = len(case["samples"])
        flag = "" if n_ok >= max(reps, MIN_VALID_REPS) else f"  INVALID (n={n_ok})"
        parts = [
            f"{metric} {_fmt(summary[metric]['mean'], 2)} ± "
            f"{_fmt(summary[metric]['stdev'], 2)} {units.get(metric, '')}"
            for metric in sorted(summary)
            if is_score(metric)
        ]
        _say(f"{case_id}: n={n_ok}{flag}\n    " + "\n    ".join(parts or ["—"]))
    for err in envelope["errors"]:
        _say(f"not measured: {err['failure_type']}: {err['message']}")
    for warning in extra.get("warnings") or []:
        _say(f"!! {warning}")


def cmd_run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    profile = sizing.profile(quick=args.quick)
    if args.reps and args.reps < MIN_VALID_REPS:
        _say(
            f"warning: --reps {args.reps} < {MIN_VALID_REPS}: every score will be INVALID"
        )
    cfg = RunConfig(
        profile=profile,
        link=args.link,
        name=args.name,
        reps=args.reps,
        warmup=args.warmup,
        budget_s=args.timeout,
        skip_gpu=args.skip_gpu,
        device_index=args.device,
        pause_s=args.pause,
    )
    if profile.quick:
        _say("QUICK MODE: smoke run only, not a measurement of this machine.")
    _say(
        f"profile {profile.name}: {cfg.effective_warmup()} warmup + "
        f"{cfg.effective_reps()} measured repetitions per case, budget "
        f"{cfg.effective_budget():g} s"
    )
    run = run_scores(cfg, log=_say)
    today = datetime.now(UTC).date()
    out_dir: Path = args.out_dir or raw_dir(TOPIC, today)
    host = str(run.envelope["host"]["label"])
    path = out_dir / raw_result_path(TOPIC, host, today).name
    try:
        write_envelope(run.envelope, path)
    except ResultCollisionError as exc:
        _say(f"run: {exc}")
        return EXIT_IO
    except OSError as exc:
        _say(f"run: cannot write {path}: {exc}")
        return EXIT_IO
    print_summary(run.envelope, cfg.effective_reps())
    _say(f"written: {path}")
    return EXIT_OK if run.complete else EXIT_INCOMPLETE


# ---------------------------------------------------------------------- report
def _report_day(dirs: Sequence[Path], explicit: date | None) -> date:
    if explicit is not None:
        return explicit
    for d in dirs:
        match = _DAY_DIR_RE.match(d.name)
        if match:
            with contextlib.suppress(ValueError):
                return date.fromisoformat(match.group(1))
    return datetime.now(UTC).date()


def cmd_report(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    dirs: list[Path] = args.results
    missing = [str(d) for d in dirs if not d.is_dir()]
    if missing:
        parser.error(f"--results folder not found: {', '.join(missing)}")
    envelopes = []
    skipped: list[str] = []
    for directory in dirs:
        docs, bad = load_dir(directory)
        envelopes += [e for e in docs if e.get("spike") == SPIKE]
        skipped += bad
    for reason in skipped:
        _say(f"skipped invalid result file: {reason}")
    if not envelopes:
        _say("warning: no S7 result files found; every cell will be TBD")
    day = _report_day(dirs, args.date).isoformat()
    extra = render_scoreboard(envelopes, args.labels)
    if skipped:
        extra += (
            "\nSkipped result files:\n" + "\n".join(f"- {s}" for s in skipped) + "\n"
        )
    try:
        template = args.template.read_text(encoding="utf-8")
        text = render_report(
            template,
            envelopes,
            topic=TOPIC,
            title="S7 hardware scores",
            day=day,
            task="0.2.11",
            spike=SPIKE,
            extra_results=extra,
        )
    except (OSError, TemplateError) as exc:
        _say(f"report: cannot build the report: {exc}")
        return EXIT_IO
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        _say(f"report: cannot write {args.out}: {exc}")
        return EXIT_IO
    _say(f"report written: {args.out} ({len(envelopes)} result files)")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)  # exits with 2 on usage errors
    try:
        if args.command == "run":
            return cmd_run(args, parser)
        return cmd_report(args, parser)
    except KeyboardInterrupt:
        _say("interrupted (Ctrl-C)")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
