# ruff: noqa: T201 - a lab tool: its output is the point
"""V1.0 lab kit (roadmap 1.0.2, D409-D411): the 72 h soak - plan, sample, judge.

The procedure is ``lab/checklists/V1.0-soak.md``. One run = one folder
(``lab/reports/raw/<day>-V1.0-soak/``); every tool of the run reads its
``plan.json``.

    uv run python lab/soak.py plan --out DIR --targets targets.json --seed 7
            [--start 2026-11-02T09:00:00+05:30 | --start-in-min 10]
            [--hours 72 --quiet-h 6 --tail-h 0.5 --warmup-min 30]
            [--job-gap-s 30 --chaos-gap-min 20-45 --duration-scale 1 --link wifi]
        Write DIR/plan.json and DIR/chaos-schedule.json (seeded, D409/D411) and
        print the job mix and the chaos actions.
    uv run --extra agent python lab/soak.py run --dir DIR --label L2
            [--target NAME=ROLE[@HOST][:MATCH] ...] [--dir-watch NAME=PATH[@CAP_MB] ...]
            [--interval 60] [--db-url postgresql://meshforge_admin@127.0.0.1:47150/meshforge
             --db-password-file FILE]
        Sample this laptop until the plan's end into DIR/samples.<label>.jsonl
        (RSS, FDs / handles, threads, CPU per process; folder sizes; on the
        controller laptop the database). Without --target, the roles the plan
        gives this label are followed (controller -> controller + postgres).
        Killed or rebooted: run the same command again - it resumes.
    uv run python lab/soak.py report DIR
        Apply the D410 rules to every file in DIR; write DIR/verdict.json and
        lab/reports/<day>-V1.0-soak.md (``--md`` to choose).

Exit codes: 0 done (report: PASS); 1 cannot read/write a file; 2 usage error;
3 report FAIL; 4 report INCONCLUSIVE (data missing - never a pass); 5 run:
psutil missing (``uv sync --extra agent``); 130 interrupted (run: resumable).
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from collections.abc import Sequence
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any, Final

_LAB_DIR: Final = Path(__file__).resolve().parent
if str(_LAB_DIR) not in sys.path:
    sys.path.insert(0, str(_LAB_DIR))

from _fabric.clock import Clock, SystemClock  # noqa: E402
from _fabric.rss import PsutilSource, RssError  # noqa: E402
from _soak import chaos, load, report, rules  # noqa: E402
from _soak.inputs import load_inputs  # noqa: E402
from _soak.plan import (  # noqa: E402
    PLAN_FILE,
    SCHEDULE_FILE,
    VERDICT_FILE,
    PlanError,
    RunPlan,
    load_plan,
    load_targets,
    new_nonce,
    write_json,
)
from _soak.sampler import DirTarget, ProcTarget, Sampler, SamplerConfig  # noqa: E402
from _soak.stats import parse_ts  # noqa: E402
from spikes._harness.atomic import atomic_write_text  # noqa: E402
from spikes._harness.envelope import git_info, host_info, safe_name  # noqa: E402

EXIT_OK: Final = 0
EXIT_IO: Final = 1
EXIT_USAGE: Final = 2
EXIT_FAIL: Final = 3
EXIT_INCONCLUSIVE: Final = 4
EXIT_NO_PSUTIL: Final = 5
EXIT_INTERRUPTED: Final = 130
REPORTS_DIR: Final = _LAB_DIR / "reports"
DB_PASSWORD_ENV: Final = "MF_SOAK_DB_PASSWORD"  # noqa: S105 - a variable name


def _say(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _range(text: str) -> tuple[float, float]:
    low, _, high = text.partition("-")
    return float(low), float(high or low)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="soak", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="write plan.json + chaos-schedule.json")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--targets", type=Path, required=True)
    p.add_argument("--seed", type=int, required=True)
    start = p.add_mutually_exclusive_group()
    start.add_argument("--start", default=None, help="t0, ISO-8601 with offset")
    start.add_argument("--start-in-min", type=float, default=5.0)
    p.add_argument("--hours", type=float, default=72.0)
    p.add_argument("--quiet-h", type=float, default=6.0)
    p.add_argument("--tail-h", type=float, default=0.5)
    p.add_argument("--warmup-min", type=float, default=30.0)
    p.add_argument("--job-gap-s", type=float, default=30.0)
    p.add_argument("--chaos-gap-min", default="20-45")
    p.add_argument("--duration-scale", type=float, default=1.0)
    p.add_argument("--kinds", default=None, help="comma list (default: every kind)")
    p.add_argument("--link", choices=("wifi", "ethernet"), default="wifi")
    p.add_argument("--force", action="store_true", help="overwrite an existing plan")

    r = sub.add_parser("run", help="sample this laptop until the plan's end")
    r.add_argument("--dir", type=Path, required=True, dest="run_dir")
    r.add_argument("--label", required=True, help="this laptop's label in targets.json")
    r.add_argument("--target", action="append", default=[])
    r.add_argument("--dir-watch", action="append", default=[])
    r.add_argument("--interval", type=float, default=60.0)
    r.add_argument("--dirs-every", type=float, default=300.0)
    r.add_argument("--db-every", type=float, default=300.0)
    r.add_argument("--db-url", default=None)
    r.add_argument("--db-password-file", type=Path, default=None)

    rep = sub.add_parser("report", help="judge a run folder (D410)")
    rep.add_argument("run_dir", type=Path)
    rep.add_argument("--md", type=Path, default=None)
    return parser


# ------------------------------------------------------------------ plan
def _check_kinds(kinds: list[str] | None) -> None:
    unknown = sorted(set(kinds or ()) - set(chaos.CATALOGUE))
    if unknown:
        raise PlanError(f"unknown chaos kinds: {unknown}")


def cmd_plan(args: argparse.Namespace, clock: Clock) -> int:
    plan_path = args.out / PLAN_FILE
    if plan_path.exists() and not args.force:
        _say(
            f"error: {plan_path} exists (a run in progress?) - pass --force to replace it"
        )
        return EXIT_USAGE
    if args.start:
        t0 = parse_ts(args.start)
        if t0 is None:
            _say(f"error: --start {args.start!r} is not an ISO-8601 time")
            return EXIT_USAGE
    else:
        t0 = (clock.wall() + timedelta(minutes=args.start_in_min)).replace(
            microsecond=0
        )
    try:
        plan = RunPlan(
            seed=args.seed,
            t0=t0.astimezone(UTC),
            hours=args.hours,
            quiet_h=args.quiet_h,
            tail_h=args.tail_h,
            warmup_min=args.warmup_min,
            job_gap_s=args.job_gap_s,
            chaos_gap_min=_range(args.chaos_gap_min),
            duration_scale=args.duration_scale,
            targets=load_targets(args.targets),
            nonce=new_nonce(),
            link=args.link,
        )
        kinds = args.kinds.split(",") if args.kinds else None
        _check_kinds(kinds)
        actions = chaos.build_schedule(plan, kinds=kinds)
    except (PlanError, ValueError) as exc:
        _say(f"error: {exc}")
        return EXIT_USAGE
    try:
        write_json(plan_path, plan.as_dict())
        write_json(args.out / SCHEDULE_FILE, chaos.schedule_doc(plan, actions))
    except OSError as exc:
        _say(f"error: {exc}")
        return EXIT_IO
    mix = load.mix_summary(load.job_specs(plan))
    print(f"plan: {plan_path}  t0 {plan.t0.isoformat()}  end {plan.end.isoformat()}")
    print(f"jobs: {mix}")
    print(f"chaos: {len(actions)} actions")
    for a in actions:
        print(f"  {a.id} +{a.offset_s / 3600:6.2f} h  {a.kind:<17} {a.target:<6} "
              f"{a.executor:<8} {a.duration_s:g} s")  # fmt: skip
    return EXIT_OK


# ------------------------------------------------------------------- run
def _db_probe(args: argparse.Namespace) -> Any:
    if not args.db_url:
        return None
    from _soak.db import DbProbe

    url = urllib.parse.urlsplit(args.db_url)
    if url.scheme not in ("postgresql", "postgres") or not url.hostname:
        raise ValueError("--db-url wants postgresql://USER@HOST:PORT/DB")
    if url.password:
        raise ValueError("put the password in --db-password-file, not in the URL")
    password: str | None = os.environ.get(DB_PASSWORD_ENV)
    if args.db_password_file is not None:
        password = args.db_password_file.read_text(encoding="utf-8").strip()
    return DbProbe(
        host=url.hostname,
        port=url.port or 5432,
        database=url.path.lstrip("/") or "meshforge",
        user=urllib.parse.unquote(url.username or "meshforge_admin"),
        password=password,
    )


def _default_targets(plan: RunPlan, label: str) -> tuple[ProcTarget, ...]:
    roles = plan.target(label).roles
    out: list[ProcTarget] = []
    if "controller" in roles:
        out += [
            ProcTarget("controller", "controller", label),
            ProcTarget("postgres", "postgres", label),
        ]
    if "agent" in roles:
        out.append(ProcTarget("agent", "agent", label))
    return tuple(out)


def cmd_run(args: argparse.Namespace, clock: Clock, source: Any) -> int:
    try:
        plan = load_plan(args.run_dir)
        procs = (
            tuple(ProcTarget.parse(t, args.label) for t in args.target)
            if args.target
            else _default_targets(plan, args.label)
        )
        cfg = SamplerConfig(
            label=args.label,
            end_at=plan.end,
            interval_s=args.interval,
            dirs_every_s=args.dirs_every,
            db_every_s=args.db_every,
            procs=procs,
            dirs=tuple(DirTarget.parse(d) for d in args.dir_watch),
        )
        probe = _db_probe(args)
    except (PlanError, ValueError, OSError) as exc:
        _say(f"error: {exc}")
        return EXIT_USAGE
    if source is None:
        try:
            source = PsutilSource()
        except RssError as exc:
            _say(f"error: {exc}")
            return EXIT_NO_PSUTIL
    path = args.run_dir / f"samples.{safe_name(args.label)}.jsonl"
    sampler = Sampler(cfg, path, clock, source, db_probe=probe, say=_say)
    wait = (plan.t0 - clock.wall()).total_seconds()
    if wait > 0:
        _say(f"t0 is in {wait:.0f} s - sampling from now (the warm-up baseline)")
    _say(f"sampling {[p.name for p in cfg.procs]} every {cfg.interval_s:g} s until "
         f"{plan.end.isoformat()} -> {path}")  # fmt: skip
    try:
        ticks = sampler.run()
    except KeyboardInterrupt:
        _say("interrupted - run the same command again to resume")
        return EXIT_INTERRUPTED
    except OSError as exc:
        _say(f"error: cannot write {path}: {exc}")
        return EXIT_IO
    _say(f"done: {ticks} ticks this run")
    return EXIT_OK


# ---------------------------------------------------------------- report
def _package_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("meshforge")
    except Exception:  # noqa: BLE001 - informational only
        return None


def cmd_report(args: argparse.Namespace, clock: Clock) -> int:
    try:
        inputs = load_inputs(args.run_dir)
    except (PlanError, ValueError, OSError) as exc:
        _say(f"error: {exc}")
        return EXIT_IO
    verdict = rules.evaluate(
        inputs,
        generated_at=clock.wall(),
        meta={
            "git": git_info(),
            "package_version": _package_version(),
            "analyzer_host": host_info()["hostname"],
            "run_dir": args.run_dir.name,
        },
    )
    day = inputs.plan.t0.date().isoformat()
    md_path = args.md or REPORTS_DIR / f"{day}-V1.0-soak.md"
    try:
        write_json(args.run_dir / VERDICT_FILE, verdict)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(md_path, report.render(verdict, json_name=VERDICT_FILE))
    except OSError as exc:
        _say(f"error: {exc}")
        return EXIT_IO
    for rule in verdict["rules"]:
        print(
            f"{rule['verdict']:<13} {rule['rule']:<7} {rule['id']:<10} {rule['summary']}"
        )
    print(f"VERDICT: {verdict['verdict']}")
    print(f"json: {args.run_dir / VERDICT_FILE}\nreport: {md_path}")
    return {rules.PASS: EXIT_OK, rules.FAIL: EXIT_FAIL}.get(
        verdict["verdict"], EXIT_INCONCLUSIVE
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Clock | None = None,
    source: Any = None,
) -> int:
    args = build_parser().parse_args(argv)
    clock = clock or SystemClock()
    if args.command == "plan":
        return cmd_plan(args, clock)
    if args.command == "run":
        return cmd_run(args, clock, source)
    return cmd_report(args, clock)


if __name__ == "__main__":
    sys.exit(main())
