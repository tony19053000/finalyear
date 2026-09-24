"""V0.3 lab kit (roadmap 0.3.15): check the live fabric through the controller's
read-only REST ``/v1`` surface. Runs on the **controller laptop**; standard library
only, so a plain ``uv sync`` is enough (also on native Windows).

Subcommands (the human procedure is lab/checklists/V0.3-fabric.md)::

    uv run python lab/fabric_check.py inventory                      # criterion 1
    uv run python lab/fabric_check.py watch --node HOST_OR_ID --link ethernet
                                                                     # criteria 2, 4
    uv run python lab/fabric_check.py restart-check --link ethernet  # criteria 3, 4
    uv run python lab/fabric_check.py soak --hours 24 --link ethernet
                                                                     # criterion 5 (1)

``--controller URL`` (default ``$MESHFORGE_CLI__CONTROLLER_URL`` or
``http://127.0.0.1:47110``). Each run writes its JSON under
``lab/reports/raw/<day>-V0.3-fabric-<sub>/`` and (re)generates
``lab/reports/<day>-V0.3-fabric-<sub>.md`` from every run of that day (merged:
hand-written sections survive).

Exit codes: ``0`` done, every measured value within its bound; ``1`` cannot
write the result (or the checkpoint is unreadable); ``2`` usage error; ``3``
done but something failed or a bound was exceeded (see the report);
``6`` the controller could not be reached; ``130`` interrupted (Ctrl-C) - what
was measured so far is still written, and ``soak`` resumes on the next run.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

_LAB_DIR: Final = Path(__file__).resolve().parent
if str(_LAB_DIR) not in sys.path:
    sys.path.insert(0, str(_LAB_DIR))

from _fabric import checkpoint, inventory, reporting, soak  # noqa: E402
from _fabric.api import (  # noqa: E402
    ApiError,
    ControllerUnreachable,
    FabricApi,
    FabricApiError,
    default_url,
)
from _fabric.clock import Clock, ConsoleOperator, Operator, SystemClock  # noqa: E402
from _fabric.restart import (  # noqa: E402
    RestartConfig,
    RestartResult,
    run_restart_check,
)
from _fabric.timeline import EXPECTABLE  # noqa: E402
from _fabric.watch import WatchConfig, WatchResult, run_watch  # noqa: E402
from spikes._harness.envelope import LINKS  # noqa: E402
from spikes._harness.report import ReportMergeError, TemplateError  # noqa: E402
from spikes._harness.reps import CaseResult  # noqa: E402

EXIT_OK: Final = 0
EXIT_WRITE: Final = 1
EXIT_USAGE: Final = 2
EXIT_INCOMPLETE: Final = 3
EXIT_UNREACHABLE: Final = 6
"""Same code as ``compute`` (``ExitCode.UNAVAILABLE``, docs/OPERATIONS.md §1)."""
EXIT_INTERRUPTED: Final = 130
REPORTS_DIR: Final = _LAB_DIR / "reports"


def _say(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def _positive(kind: type[int] | type[float]) -> Any:
    def parse(text: str) -> int | float:
        value = kind(text)
        if value <= 0:
            raise argparse.ArgumentTypeError(f"must be > 0, got {text}")
        return value

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fabric_check",
        description="V0.3 lab checks over the controller's REST /v1 (roadmap 0.3.15).",
    )
    parser.add_argument(
        "--controller",
        default=None,
        help="controller REST URL (default: %(default)s -> "
        "$MESHFORGE_CLI__CONTROLLER_URL or http://127.0.0.1:47110)",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=REPORTS_DIR,
        help="where reports go (default lab/reports)",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="label of this (controller) laptop in reports, e.g. L1",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inv = sub.add_parser(
        "inventory", help="every node's hardware (criterion 1; a human compares)"
    )
    inv.add_argument(
        "--json", action="store_true", help="print the node details as JSON to stdout"
    )

    watch = sub.add_parser(
        "watch", help="time OFFLINE/ONLINE detection of one node (criteria 2, 4)"
    )
    watch.add_argument(
        "--node", required=True, help="node_id (node_...) or the node's hostname"
    )
    watch.add_argument(
        "--expect",
        default="offline,online",
        help=f"states to walk per repetition, from {', '.join(EXPECTABLE)} (default %(default)s)",
    )
    watch.add_argument(
        "--reps",
        type=_positive(int),
        default=5,
        help="repetitions (default %(default)s; >= 5 for a valid row)",
    )
    watch.add_argument(
        "--link", choices=LINKS, required=True, help="the link the node uses"
    )
    watch.add_argument(
        "--poll",
        type=_positive(float),
        default=0.1,
        help="poll interval s (default %(default)s)",
    )
    watch.add_argument(
        "--timeout",
        type=_positive(float),
        default=60.0,
        help="give up on a state this many s after Enter (default %(default)s)",
    )
    watch.add_argument(
        "--action",
        choices=("cable", "agent"),
        default="cable",
        help="what the operator does: pull the cable (criterion 2) or stop/start the "
        "agent (criterion 4)",
    )

    rst = sub.add_parser(
        "restart-check",
        help="kill+restart the controller; time the reconnect (criteria 3, 4)",
    )
    rst.add_argument(
        "--reps",
        type=_positive(int),
        default=5,
        help="repetitions (default %(default)s)",
    )
    rst.add_argument("--link", choices=LINKS, required=True)
    rst.add_argument("--poll", type=_positive(float), default=0.1)
    rst.add_argument(
        "--timeout",
        type=_positive(float),
        default=120.0,
        help="give up waiting for the nodes this many s after the controller is back",
    )

    sk = sub.add_parser(
        "soak", help="record every transition for --hours (criterion 5 part 1)"
    )
    sk.add_argument("--hours", type=_positive(float), default=24.0)
    sk.add_argument("--link", choices=LINKS, required=True)
    sk.add_argument(
        "--poll", type=_positive(float), default=1.0, help="node poll interval s"
    )
    sk.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help="checkpoint file (default <reports>/raw/V0.3-fabric-soak.checkpoint.json)",
    )
    sk.add_argument(
        "--checkpoint-every",
        type=_positive(float),
        default=300.0,
        help="seconds (default 5 min)",
    )
    sk.add_argument(
        "--fresh",
        action="store_true",
        help="ignore an existing checkpoint and start over",
    )
    return parser


def _cases_watch(result: WatchResult, cfg: WatchConfig) -> list[CaseResult]:
    return [
        CaseResult(
            case_id=result.node_id,
            label=f"{result.hostname or '?'} ({result.node_id})",
            metrics={f"{state}_s": "s" for state in cfg.expect},
            attrs={
                "node_id": result.node_id,
                "hostname": result.hostname,
                "expect": list(cfg.expect),
            },
            requested_reps=cfg.reps,
            samples=list(result.samples),
            errors=list(result.errors),
            aborted=result.aborted,
        )
    ]


def _cases_restart(result: RestartResult, cfg: RestartConfig) -> list[CaseResult]:
    cases = [
        CaseResult(
            case_id="all-nodes",
            label="all connected nodes",
            metrics={"reconnect_all_s": "s", "outage_s": "s"},
            attrs={"kind": "all"},
            requested_reps=cfg.reps,
            samples=list(result.samples),
            errors=list(result.errors),
            aborted=result.aborted,
        )
    ]
    for node_id, hostname in sorted(result.nodes.items()):
        samples = [
            {"rep": s["rep"], **s["nodes"][node_id]}
            for s in result.samples
            if node_id in s["nodes"]
        ]
        cases.append(
            CaseResult(
                case_id=node_id,
                label=f"{hostname} ({node_id})",
                metrics={"reconnect_s": "s"},
                attrs={"kind": "node", "node_id": node_id, "hostname": hostname},
                requested_reps=cfg.reps,
                samples=samples,
            )
        )
    return cases


class _Ctx:
    def __init__(
        self, args: argparse.Namespace, api: FabricApi, clock: Clock, operator: Operator
    ) -> None:
        self.args, self.api, self.clock, self.operator = args, api, clock, operator

    def write(
        self, sub: str, doc: dict[str, Any], *, suffix: str | None, extra_md: Any
    ) -> int:
        try:
            written = reporting.write(
                sub,
                doc,
                reports_dir=self.args.reports_dir,
                day=self.clock.wall().date(),
                suffix=suffix,
                extra_md=extra_md,
            )
        except (OSError, ReportMergeError, TemplateError) as exc:
            _say(f"error: cannot write the result: {exc}")
            return EXIT_WRITE
        _say(f"result JSON: {written.json_path}")
        _say(f"report:      {written.report_path}")
        for message in written.messages:
            _say(f"  ({message})")
        return EXIT_OK

    def envelope(self, sub: str, **kwargs: Any) -> dict[str, Any]:
        return reporting.envelope(
            sub, now=self.clock.wall(), host_label=self.args.name, **kwargs
        )


def _resolve_node(api: FabricApi, value: str) -> str:
    if value.startswith("node_"):
        return value
    matches = [n for n in api.nodes() if n.get("hostname") == value]
    live = [n for n in matches if n.get("connected") is True] or matches
    if len(live) != 1:
        found = ", ".join(str(n.get("node_id")) for n in live) or "none"
        raise ValueError(
            f"hostname {value!r} matches {len(live)} node(s) ({found}); pass --node node_..."
        )
    return str(live[0]["node_id"])


def cmd_inventory(ctx: _Ctx) -> int:
    details = inventory.collect(ctx.api)
    notes = inventory.notes(details)
    if ctx.args.json:
        import json

        sys.stdout.write(json.dumps(details, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            inventory.text_table([inventory.row(d) for d in details]) + "\n"
        )
    for note in notes:
        _say(f"note: {note}")
    _say(
        "criterion 1 is a human comparison: check each row against docs/LAB.md section 1."
    )
    doc = ctx.envelope(
        "inventory",
        link=None,
        params={"controller_url": ctx.api.base_url},
        cases=[],
        extra={"nodes": details, "notes": notes},
    )
    return ctx.write("inventory", doc, suffix=None, extra_md=reporting.inventory_md)


_AGENT_ACTIONS: Final = {
    "offline": "STOP the agent on {host} (Ctrl-C in its terminal / systemctl --user stop / "
    "schtasks /end) - press Enter at the same moment",
    "online": "START the agent on {host} again - press Enter at the same moment",
}


def cmd_watch(ctx: _Ctx) -> int:
    args = ctx.args
    expect = tuple(s.strip() for s in args.expect.split(",") if s.strip())
    try:
        cfg = WatchConfig(
            node_id=_resolve_node(ctx.api, args.node),
            expect=expect,
            reps=args.reps,
            poll_s=args.poll,
            detect_timeout_s=args.timeout,
            action_text=dict(_AGENT_ACTIONS) if args.action == "agent" else {},
        )
    except ValueError as exc:
        _say(f"error: {exc}")
        return EXIT_USAGE
    result = WatchResult(node_id=cfg.node_id)
    interrupted = False
    try:
        run_watch(ctx.api, ctx.clock, ctx.operator, cfg, result)
    except KeyboardInterrupt:
        interrupted = True
        _say("interrupted - writing what was measured so far")
    doc = ctx.envelope(
        "watch",
        link=args.link,
        params={
            "controller_url": ctx.api.base_url,
            "node_id": cfg.node_id,
            "expect": list(cfg.expect),
            "reps": cfg.reps,
            "poll_s": cfg.poll_s,
            "detect_timeout_s": cfg.detect_timeout_s,
            "action": args.action,
            "bounds_s": {"offline": 8.0, "online": 10.0},
        },
        cases=_cases_watch(result, cfg),
        extra={"interrupted": interrupted},
    )
    stamp = ctx.clock.wall().strftime("%H%M%S")
    code = ctx.write(
        "watch",
        doc,
        suffix=f"{result.hostname or cfg.node_id}-{args.action}-{stamp}",
        extra_md=reporting.watch_md,
    )
    if code or interrupted:
        return code or EXIT_INTERRUPTED
    bad_bound = any(
        s.get(f"{k}_within_bound") is False for s in result.samples for k in cfg.expect
    )
    restarted = args.action == "cable" and any(
        s.get("restarted") is True for s in result.samples
    )
    if result.errors or bad_bound or restarted or len(result.samples) < cfg.reps:
        _say("done, with failures - see the report")
        return EXIT_INCOMPLETE
    return EXIT_OK


def cmd_restart(ctx: _Ctx) -> int:
    args = ctx.args
    cfg = RestartConfig(
        reps=args.reps, poll_s=args.poll, reconnect_timeout_s=args.timeout
    )
    result = RestartResult()
    interrupted = False
    try:
        run_restart_check(ctx.api, ctx.clock, ctx.operator, cfg, result)
    except KeyboardInterrupt:
        interrupted = True
        _say("interrupted - writing what was measured so far")
    doc = ctx.envelope(
        "restart",
        link=args.link,
        params={
            "controller_url": ctx.api.base_url,
            "reps": cfg.reps,
            "poll_s": cfg.poll_s,
            "reconnect_timeout_s": cfg.reconnect_timeout_s,
            "bound_s": 15.0,
        },
        cases=_cases_restart(result, cfg),
        extra={"interrupted": interrupted},
    )
    stamp = ctx.clock.wall().strftime("%H%M%S")
    code = ctx.write("restart", doc, suffix=stamp, extra_md=reporting.restart_md)
    if code or interrupted:
        return code or EXIT_INTERRUPTED
    bad = any(s.get("reconnect_all_within_bound") is not True for s in result.samples)
    if result.errors or bad or len(result.samples) < cfg.reps:
        _say("done, with failures - see the report")
        return EXIT_INCOMPLETE
    return EXIT_OK


def _soak_doc(ctx: _Ctx, state: dict[str, Any], cfg: soak.SoakConfig) -> dict[str, Any]:
    summary = soak.summarize(state)
    offline = [
        {**t, "reason": soak.attribute(t, state["session_ends"])}
        for t in state["transitions"]
        if t["new"] == "offline" and t["field"] in ("state", "appeared")
    ]
    return ctx.envelope(
        "soak",
        link=cfg.link,
        params={
            "controller_url": ctx.api.base_url,
            "hours": cfg.hours,
            "poll_s": cfg.poll_s,
            "sessions_every_s": cfg.sessions_every_s,
            "checkpoint_every_s": cfg.checkpoint_every_s,
        },
        cases=[],
        extra={"summary": summary, "offline_transitions": offline, "state": state},
    )


def cmd_soak(ctx: _Ctx) -> int:
    args = ctx.args
    cfg = soak.SoakConfig(
        hours=args.hours,
        link=args.link,
        poll_s=args.poll,
        checkpoint_every_s=args.checkpoint_every,
    )
    path: Path = args.checkpoint or (
        args.reports_dir / "raw" / "V0.3-fabric-soak.checkpoint.json"
    )
    try:
        state = None if args.fresh else checkpoint.load(path, soak.KIND)
    except checkpoint.CheckpointError as exc:
        _say(f"error: {exc}. Move it away or pass --fresh to start a new soak.")
        return EXIT_WRITE
    if state is not None:
        if state.get("link") != cfg.link or state.get("hours") != cfg.hours:
            _say(
                f"error: the checkpoint is a {state.get('hours')} h soak on {state.get('link')}; "
                "re-run with the same --hours/--link, or --fresh to start over"
            )
            return EXIT_USAGE
        _say(
            f"resuming the soak started {state['started_at']} "
            f"({state['elapsed_s'] / 3600:.2f} h observed)"
        )
    else:
        state = soak.new_state(cfg, ctx.clock.wall())
        _say(f"new soak: {cfg.hours} h on {cfg.link}; checkpoint {path}")

    def save(doc: dict[str, Any]) -> None:
        checkpoint.save(path, soak.KIND, doc)

    runner = soak.Soak(ctx.api, ctx.clock, ctx.operator, cfg, state, save)
    interrupted = False
    try:
        if not state["done"]:
            runner.run()
    except KeyboardInterrupt:
        interrupted = True
        try:
            runner.save()
        except OSError as exc:
            _say(f"error: cannot save the checkpoint: {exc}")
        _say("interrupted - checkpoint saved; run the same command again to resume")
    except OSError as exc:
        _say(f"error: cannot save the checkpoint: {exc}")
        return EXIT_WRITE
    day = state["day"]
    doc = _soak_doc(ctx, state, cfg)
    try:
        written = reporting.write(
            "soak",
            doc,
            reports_dir=args.reports_dir,
            day=__import__("datetime").date.fromisoformat(day),
            suffix=None,
            extra_md=reporting.soak_md,
        )
    except (OSError, ReportMergeError, TemplateError) as exc:
        _say(f"error: cannot write the result: {exc}")
        return EXIT_WRITE
    _say(f"result JSON: {written.json_path}")
    _say(f"report:      {written.report_path}")
    summary = doc["extra"]["summary"]
    _say(
        f"observed {summary['observed_h']} of {summary['target_h']} h; OFFLINE events: "
        f"{summary['offline_events']}; SUSPECT: {summary['suspect_events']}; "
        f"controller outages: {summary['outages']}"
    )
    if interrupted:
        return EXIT_INTERRUPTED
    if summary["offline_events"] or summary["outages"]:
        return EXIT_INCOMPLETE
    return EXIT_OK


_COMMANDS: Final = {
    "inventory": cmd_inventory,
    "watch": cmd_watch,
    "restart-check": cmd_restart,
    "soak": cmd_soak,
}


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Clock | None = None,
    operator: Operator | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        api = FabricApi(args.controller or default_url())
    except ValueError as exc:
        _say(f"error: --controller: {exc}")
        return EXIT_USAGE
    clock = clock or SystemClock()
    operator = operator or ConsoleOperator(clock)
    try:
        api.healthz()
    except ControllerUnreachable as exc:
        _say(f"error: {exc}.")
        _say(
            "Is the controller running, and is this the right URL? Pass --controller "
            "http://<controller-ip>:47110 or set MESHFORGE_CLI__CONTROLLER_URL."
        )
        return EXIT_UNREACHABLE
    except ApiError as exc:
        _say(f"error: {api.base_url}/healthz: {exc} - is this a MeshForge controller?")
        return EXIT_UNREACHABLE
    ctx = _Ctx(args, api, clock, operator)
    try:
        return _COMMANDS[args.command](ctx)
    except ControllerUnreachable as exc:
        _say(f"error: lost the controller: {exc}")
        return EXIT_UNREACHABLE
    except FabricApiError as exc:
        _say(
            f"error: unexpected answer from the controller: {exc} ({exc.failure_type})"
        )
        return EXIT_INCOMPLETE
    except KeyboardInterrupt:
        _say("interrupted")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
