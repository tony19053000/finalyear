"""V0.3 lab kit (roadmap 0.3.15, success criterion 5 part 2): sample the resident
memory (RSS) of this laptop's running ``meshforge-agent`` during the 24 h soak and
report its growth (the bound is < 10 %).

Run it on **every** laptop that runs an agent, after the agent has started::

    uv run --extra agent python lab/agent_rss.py run --hours 24 --name L2
    # after the soak, on the controller laptop, with every laptop's JSON copied into
    # lab/reports/raw/<day>-V0.3-agent-rss/:
    uv run python lab/agent_rss.py report --day 2026-10-01

It needs psutil, which comes with the agent extra (every agent laptop has it).
How the agent process is found on Linux and Windows is described in
``lab/_fabric/rss.py``; ``--pid`` overrides it. One sample every ``--interval``
seconds (default 60); the checkpoint is rewritten after every sample, so a crash
loses at most one sample and re-running the same command resumes.

Exit codes: ``0`` done and growth < 10 % with no agent restart; ``1`` cannot
write the result / unreadable checkpoint; ``2`` usage error; ``3`` done but growth
>= 10 %, the agent restarted, or it was never found; ``4`` psutil missing;
``5`` no agent process found at the start (or several - pass ``--pid``);
``130`` interrupted (the checkpoint is kept; run again to resume).
"""

from __future__ import annotations

import argparse
import socket
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Final

_LAB_DIR: Final = Path(__file__).resolve().parent
if str(_LAB_DIR) not in sys.path:
    sys.path.insert(0, str(_LAB_DIR))

from _fabric import checkpoint, reporting  # noqa: E402
from _fabric.clock import Clock, ConsoleOperator, Operator, SystemClock  # noqa: E402
from _fabric.rss import (  # noqa: E402
    KIND,
    ProcessSource,
    PsutilSource,
    RssError,
    pick_agent,
    summarize,
)
from _fabric.rss_run import RssConfig, RssSampler, new_state  # noqa: E402
from spikes._harness.envelope import safe_name  # noqa: E402
from spikes._harness.report import ReportMergeError, TemplateError  # noqa: E402

EXIT_OK: Final = 0
EXIT_WRITE: Final = 1
EXIT_USAGE: Final = 2
EXIT_INCOMPLETE: Final = 3
EXIT_NO_PSUTIL: Final = 4
EXIT_NO_AGENT: Final = 5
EXIT_INTERRUPTED: Final = 130
GROWTH_BOUND_PCT: Final = 10.0
REPORTS_DIR: Final = _LAB_DIR / "reports"


def _say(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _positive(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {text}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent_rss", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--reports-dir", type=Path, default=REPORTS_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="sample this laptop's agent RSS")
    run.add_argument("--hours", type=_positive, default=24.0)
    run.add_argument(
        "--interval", type=_positive, default=60.0, help="seconds between samples"
    )
    run.add_argument(
        "--warmup-min",
        type=float,
        default=10.0,
        help="minutes of samples before the growth baseline (default %(default)s)",
    )
    run.add_argument(
        "--pid", type=int, default=None, help="the agent's pid (skip auto-detection)"
    )
    run.add_argument(
        "--name", default=None, help="this laptop's label, e.g. L2 (default hostname)"
    )
    run.add_argument("--checkpoint", type=Path, default=None)
    run.add_argument(
        "--fresh", action="store_true", help="ignore an existing checkpoint"
    )
    rep = sub.add_parser(
        "report", help="rebuild the report from every laptop's JSON of a day"
    )
    rep.add_argument("--day", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    return parser


def _doc(state: dict[str, Any], cfg: RssConfig, clock: Clock) -> dict[str, Any]:
    return reporting.envelope(
        "rss",
        link=None,
        params={
            "hours": cfg.hours,
            "interval_s": cfg.interval_s,
            "warmup_min": cfg.warmup_min,
        },
        cases=[],
        extra={"summary": summarize(state), "state": state},
        now=clock.wall(),
        host_label=state["label"],
    )


def cmd_run(
    args: argparse.Namespace,
    clock: Clock,
    operator: Operator,
    source: ProcessSource | None,
) -> int:
    try:
        cfg = RssConfig(
            hours=args.hours,
            interval_s=args.interval,
            warmup_min=args.warmup_min,
            pid=args.pid,
        )
    except ValueError as exc:
        _say(f"error: {exc}")
        return EXIT_USAGE
    label = args.name or socket.gethostname()
    path: Path = args.checkpoint or (
        args.reports_dir / "raw" / f"V0.3-agent-rss.{safe_name(label)}.checkpoint.json"
    )
    real: PsutilSource | None = None
    if source is None:
        real = PsutilSource()  # raises PsutilMissing
        source = real
    try:
        state = None if args.fresh else checkpoint.load(path, KIND)
    except checkpoint.CheckpointError as exc:
        _say(f"error: {exc}. Move it away or pass --fresh.")
        return EXIT_WRITE
    if state is None:
        try:
            first = (
                real.by_pid(cfg.pid)
                if (real and cfg.pid is not None)
                else pick_agent(source.processes())
            )
        except RssError as exc:
            _say(f"error: {exc}. Start the agent first (docs/OPERATIONS.md section 1).")
            return EXIT_NO_AGENT
        _say(
            f"sampling meshforge-agent pid {first.pid} every {cfg.interval_s:g} s "
            f"for {cfg.hours} h"
        )
        state = new_state(cfg, clock.wall(), label)
    else:
        _say(
            f"resuming the RSS run started {state['started_at']} ({len(state['samples'])} samples)"
        )

    def save(doc: dict[str, Any]) -> None:
        checkpoint.save(path, KIND, doc)

    sampler = RssSampler(
        source,
        clock,
        operator,
        cfg,
        state,
        save,
        find_pid=real.by_pid if real is not None else None,
    )
    interrupted = False
    try:
        if not state["done"]:
            sampler.run()
    except KeyboardInterrupt:
        interrupted = True
        _say("interrupted - checkpoint kept; run the same command again to resume")
    except RssError as exc:
        _say(f"error: {exc}")
        return EXIT_NO_AGENT
    except OSError as exc:
        _say(f"error: cannot save the checkpoint: {exc}")
        return EXIT_WRITE
    try:
        written = reporting.write(
            "rss",
            _doc(state, cfg, clock),
            reports_dir=args.reports_dir,
            day=date.fromisoformat(state["day"]),
            extra_md=reporting.rss_md,
        )
    except (OSError, ReportMergeError, TemplateError) as exc:
        _say(f"error: cannot write the result: {exc}")
        return EXIT_WRITE
    _say(f"result JSON: {written.json_path}")
    _say(f"report:      {written.report_path}")
    s = summarize(state)
    _say(
        f"baseline {s['baseline_mib']} MiB -> end {s['end_mib']} MiB (max {s['max_mib']}), "
        f"growth {s['growth_pct']} %, agent restarts {s['restarts']}"
    )
    if interrupted:
        return EXIT_INTERRUPTED
    growth = s["growth_pct"]
    if growth is None or growth >= GROWTH_BOUND_PCT or s["restarts"]:
        return EXIT_INCOMPLETE
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    try:
        path, messages = reporting.render(
            "rss", reports_dir=args.reports_dir, day=args.day, extra_md=reporting.rss_md
        )
    except (OSError, ReportMergeError, TemplateError) as exc:
        _say(f"error: cannot write the report: {exc}")
        return EXIT_WRITE
    _say(f"report: {path}")
    for message in messages:
        _say(f"  ({message})")
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Clock | None = None,
    operator: Operator | None = None,
    source: ProcessSource | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "report":
        return cmd_report(args)
    clock = clock or SystemClock()
    try:
        return cmd_run(args, clock, operator or ConsoleOperator(clock), source)
    except RssError as exc:
        _say(f"error: {exc}")
        return EXIT_NO_PSUTIL if exc.failure_type == "psutil_missing" else EXIT_NO_AGENT


if __name__ == "__main__":
    sys.exit(main())
