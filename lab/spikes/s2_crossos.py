"""Spike S2 (roadmap 0.2.6): cross-OS reachability without WSL.

Answers, for one ordered pair of laptops: can a **native** (non-WSL) Python
process on the far machine be reached over HTTP and gRPC, does a long-lived
agent-initiated bidirectional gRPC stream (ADR-0002) survive idle periods, and
does the outbound-only model work from Windows with **no inbound firewall
rule** on the client? Standard library plus ``grpcio`` (a core dependency), so
it runs after a plain ``uv sync`` on Linux and native Windows.

Three subcommands, run from the repo root::

    # on the laptop that accepts connections (the "server"), bound to its lab IP:
    uv run python lab/spikes/s2_crossos.py serve --bind 192.168.1.20
    # on the other laptop (this side needs no inbound firewall rule):
    uv run python lab/spikes/s2_crossos.py client --server 192.168.1.20 \\
        --link ethernet --no-inbound-rule
    # anywhere, after collecting every host's JSON in one folder:
    uv run python lab/spikes/s2_crossos.py report \\
        --results lab/reports/raw/2026-09-20-S2-crossos \\
        --out lab/reports/2026-09-20-S2-crossos.md --hosts L1=host1,L2=host2

The tool **refuses to run inside WSL** (``--allow-wsl`` overrides): WSL2 sits
behind its own NAT, so a result from there would answer a different question
than ADR-0009 asks.

Exit codes: ``0`` done; ``1`` could not bind / write output; ``2`` usage error;
``3`` results written but incomplete (peer lost, failed repetitions - see
``errors`` in the JSON); ``4`` refused: running inside WSL; ``130`` interrupted.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import re
import signal
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from types import FrameType
from typing import Any, Final

_LAB_DIR: Final = Path(__file__).resolve().parents[1]
if str(_LAB_DIR) not in sys.path:
    sys.path.insert(0, str(_LAB_DIR))

from spikes._harness import MIN_VALID_REPS  # noqa: E402
from spikes._harness.atomic import atomic_write_text  # noqa: E402
from spikes._harness.envelope import (  # noqa: E402
    LINKS,
    RAW_ROOT,
    load_dir,
    raw_dir,
    raw_result_path,
    sanitize_text,
    write_envelope,
)
from spikes._harness.report import (  # noqa: E402
    TEMPLATE_PATH,
    TemplateError,
    render_report,
)
from spikes._s1.protocol import check_bind_address, is_lan_address  # noqa: E402
from spikes._s2.env import (  # noqa: E402
    firewall_profile,
    non_private_profiles,
    refuse_if_wsl,
    wsl_status_here,
)
from spikes._s2.protocol import (  # noqa: E402
    DEFAULT_GRPC_PORT,
    DEFAULT_HTTP_PORT,
    KEEPALIVE_MODES,
    MAX_BLOB_BYTES,
    MAX_HOLD_S,
    MIN_BLOB_BYTES,
)
from spikes._s2.run import TOPIC, ClientConfig, ClientRun, run_client  # noqa: E402
from spikes._s2.server import S2Server, ServerLimits  # noqa: E402
from spikes._s2.verdict import (  # noqa: E402
    direction_verdict,
    direction_verdicts,
    expected_directions,
    render_verdicts,
)

EXIT_OK: Final = 0
EXIT_IO: Final = 1
EXIT_USAGE: Final = 2
EXIT_INCOMPLETE: Final = 3
EXIT_WSL: Final = 4
EXIT_INTERRUPTED: Final = 130
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


def _port(text: str) -> int:
    return int(_number(0, 65535, integer=True)(text))


def _name(text: str) -> str:
    if not _NAME_RE.match(text):
        raise argparse.ArgumentTypeError("use 1-64 characters from A-Z a-z 0-9 . _ -")
    return text


def _gaps(text: str) -> tuple[float, ...]:
    try:
        values = tuple(sorted({float(p) for p in text.split(",") if p.strip()}))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"comma-separated seconds expected: {text!r}"
        ) from None
    if not values or not all(0.0 <= v <= MAX_HOLD_S for v in values):
        raise argparse.ArgumentTypeError(f"each idle gap must be 0..{MAX_HOLD_S:g} s")
    return values


def _keepalive(text: str) -> tuple[str, ...]:
    if text == "both":
        return KEEPALIVE_MODES
    if text in KEEPALIVE_MODES:
        return (text,)
    raise argparse.ArgumentTypeError("use on, off or both")


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
        prog="lab/spikes/s2_crossos.py",
        description="Spike S2: cross-OS reachability (HTTP + gRPC, native, no WSL).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="accept connections on this laptop")
    serve.add_argument("--bind", required=True, help="this laptop's lab IP address")
    serve.add_argument("--http-port", type=_port, default=DEFAULT_HTTP_PORT)
    serve.add_argument("--grpc-port", type=_port, default=DEFAULT_GRPC_PORT)
    serve.add_argument(
        "--allow-any",
        action="store_true",
        help="allow binding the wildcard (0.0.0.0/::) or a non-private address",
    )
    serve.add_argument(
        "--idle",
        type=_number(1, 86400),
        default=900.0,
        metavar="SECONDS",
        help="stop after this long without any request (default %(default)g)",
    )
    serve.add_argument(
        "--duration",
        type=_number(1, 86400),
        default=7200.0,
        metavar="SECONDS",
        help="stop after this long in total (default %(default)g)",
    )
    serve.add_argument(
        "--io-timeout", type=_number(1, 600), default=30.0, metavar="SECONDS"
    )
    serve.add_argument("--name", type=_name, help="identity shown in the report")
    serve.add_argument(
        "--ready-file",
        type=Path,
        help="write the bound ports here (JSON) once listening",
    )
    serve.add_argument("--quiet", action="store_true", help="no progress output")
    serve.add_argument(
        "--allow-wsl", action="store_true", help="record a deliberately-WSL data point"
    )

    client = sub.add_parser("client", help="measure one peer from this laptop")
    client.add_argument("--server", required=True, metavar="IP", help="peer lab IP")
    client.add_argument("--http-port", type=_port, default=DEFAULT_HTTP_PORT)
    client.add_argument("--grpc-port", type=_port, default=DEFAULT_GRPC_PORT)
    client.add_argument(
        "--link", required=True, choices=LINKS, help="physical link in use"
    )
    client.add_argument(
        "--reps",
        type=_number(1, 1000, integer=True),
        default=5,
        help=f"measured repetitions per case (>= {MIN_VALID_REPS} to be valid)",
    )
    client.add_argument(
        "--warmup", type=_number(0, 100, integer=True), default=1, help="warmup reps"
    )
    client.add_argument(
        "--blob-bytes",
        type=_number(MIN_BLOB_BYTES, MAX_BLOB_BYTES, integer=True),
        default=32 * 1024 * 1024,
        metavar="BYTES",
        help="size of the streamed body (default %(default)d)",
    )
    client.add_argument(
        "--hold",
        type=_number(0.1, MAX_HOLD_S),
        default=5.0,
        metavar="SECONDS",
        help="active phase of each bidi session (default %(default)g)",
    )
    client.add_argument(
        "--ping-interval", type=_number(0.05, 60), default=1.0, metavar="SECONDS"
    )
    client.add_argument(
        "--idle-gaps",
        type=_gaps,
        default=(30.0, 120.0),
        metavar="SECONDS",
        help="idle gaps to test, comma-separated (default 30,120)",
    )
    client.add_argument(
        "--keepalive",
        type=_keepalive,
        default=KEEPALIVE_MODES,
        metavar="on|off|both",
        help="gRPC keepalive modes to test (default both)",
    )
    client.add_argument(
        "--connect-timeout", type=_number(0.1, 120), default=5.0, metavar="SECONDS"
    )
    client.add_argument(
        "--io-timeout", type=_number(0.5, 600), default=20.0, metavar="SECONDS"
    )
    client.add_argument("--pause", type=_number(0, 60), default=0.5, metavar="SECONDS")
    client.add_argument("--name", type=_name, help="identity shown in the report")
    client.add_argument(
        "--out-dir",
        type=Path,
        help=f"result folder (default {RAW_ROOT.name}/<today>-{TOPIC} under lab/reports)",
    )
    client.add_argument(
        "--allow-any", action="store_true", help="allow a non-private peer address"
    )
    client.add_argument(
        "--no-inbound-rule",
        action="store_true",
        help="record that NO inbound firewall rule exists on this machine "
        "(the ADR-0002 outbound-only claim)",
    )
    client.add_argument(
        "--note",
        default="",
        help="operator note, e.g. 'Windows Defender prompt appeared: no'",
    )
    client.add_argument(
        "--allow-wsl", action="store_true", help="record a deliberately-WSL data point"
    )

    report = sub.add_parser(
        "report", help="build the markdown report + verdict tables from result JSONs"
    )
    report.add_argument(
        "--results",
        action="append",
        required=True,
        type=Path,
        metavar="DIR",
        help="folder with client result JSONs (repeatable)",
    )
    report.add_argument(
        "--out", required=True, type=Path, help="markdown report to write"
    )
    report.add_argument(
        "--hosts",
        type=_labels,
        default={},
        help="name every host that should have been tested, e.g. "
        "L1=hostA,L2=hostB: hostA is shown as L1 in the report (matching is "
        "case-insensitive) and every untested direction between the listed "
        "hosts is reported TBD",
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


# ------------------------------------------------------------------ WSL gate
def _wsl_gate(allow_wsl: bool) -> tuple[dict[str, Any], int | None]:
    status = wsl_status_here()
    refusal = refuse_if_wsl(status, allow_wsl=allow_wsl)
    if refusal is not None:
        _say(refusal)
        return status, EXIT_WSL
    if status.get("is_wsl"):
        _say(
            "warning: running inside WSL with --allow-wsl; this does NOT validate ADR-0009"
        )
    return status, None


def _warn_firewall() -> None:
    firewall = firewall_profile()
    if not firewall.get("supported"):
        return
    if firewall.get("error"):
        _say(f"note: could not read the Windows network profile: {firewall['error']}")
        return
    public = non_private_profiles(firewall)
    if public:
        _say(
            "warning: these adapters are NOT on a Private profile: "
            f"{', '.join(public)}. docs/LAB.md §6 requires Private for the lab network."
        )


# ---------------------------------------------------------------------- serve
def cmd_serve(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    _, refusal = _wsl_gate(args.allow_wsl)
    if refusal is not None:
        return refusal
    try:
        bind = check_bind_address(args.bind, allow_any=args.allow_any)
    except ValueError as exc:
        parser.error(str(exc))
    log = (lambda _m: None) if args.quiet else _say
    _warn_firewall()
    server = S2Server(
        bind,
        args.http_port,
        args.grpc_port,
        ServerLimits(
            duration_s=args.duration, idle_s=args.idle, io_timeout_s=args.io_timeout
        ),
        name=args.name or "",
        log=log,
    )
    try:
        http_port, grpc_port = server.open()
    except OSError as exc:
        _say(
            f"serve: cannot listen on {bind} (HTTP {args.http_port} / "
            f"gRPC {args.grpc_port}): {exc}"
        )
        return EXIT_IO
    if sys.platform != "win32":

        def _on_term(_sig: int, _frame: FrameType | None) -> None:
            server.stop()

        signal.signal(signal.SIGTERM, _on_term)
    if args.ready_file is not None:
        try:
            atomic_write_text(
                args.ready_file,
                json.dumps({"http_port": http_port, "grpc_port": grpc_port}),
            )
        except OSError as exc:
            server.stop()
            server.serve()
            _say(f"serve: cannot write --ready-file: {exc}")
            return EXIT_IO
    log(
        f"S2 server listening on {bind}: HTTP {http_port}, gRPC {grpc_port}; LAN peers "
        f"only; stops after {args.idle:g} s idle or {args.duration:g} s total. Ctrl-C to stop."
    )
    log("Remember: remove the temporary firewall rule when the spike is finished.")
    try:
        reason = server.serve()
    except KeyboardInterrupt:
        # 130, like every other Ctrl-C in this tool (S2.md exit-code table):
        # an interrupted serve must not be pasted back as a clean completion.
        log("interrupted (Ctrl-C); server stopped")
        return EXIT_INTERRUPTED
    counters = server.counters()
    log(f"server stopped: {reason}. {json.dumps(counters, sort_keys=True)}")
    return EXIT_OK


# --------------------------------------------------------------------- client
def _check_server(text: str, *, allow_any: bool) -> str:
    try:
        addr = ipaddress.ip_address(text.strip())
    except ValueError:
        raise ValueError(f"--server needs an IP address, got {text!r}") from None
    if addr.is_unspecified or addr.is_multicast:
        raise ValueError(f"--server {text} is not a unicast address")
    if not allow_any and not is_lan_address(str(addr)):
        raise ValueError(
            f"--server {text} is not a private/LAN address (trusted LAN only, "
            "docs/LAB.md §6); pass --allow-any to override"
        )
    return str(addr)


def print_summary(run: ClientRun) -> None:
    verdict = direction_verdict(run.envelope)
    _say(f"--- S2 verdict [{run.envelope['link']}] {verdict.direction} ---")
    _say(f"client: {verdict.client_os}    server: {verdict.server_os}")
    for name, item in {**verdict.cases, **verdict.sessions}.items():
        _say(f"  {name:<28} {item.cell()}")
    _say(f"  {'outbound-only (ADR-0002)':<28} {verdict.outbound_only.cell()}")
    metrics = verdict.metrics
    _say(
        "  metrics: HTTP first byte "
        f"{_fmt(metrics['http_ttfb_ms'])} ms, HTTP stream {_fmt(metrics['http_mbps'], 1)} "
        f"Mbit/s, gRPC unary RTT {_fmt(metrics['grpc_rtt_ms'])} ms, gRPC stream "
        f"{_fmt(metrics['grpc_mbps'], 1)} Mbit/s"
    )
    fixes = verdict.fixes()
    if fixes:
        _say("  what to fix:")
        for fix in fixes:
            _say(f"    - {fix}")
    notes = verdict.notes()
    if notes:
        # warmup-only failures: reported, but they change no verdict
        _say("  notes (not counted):")
        for note in notes:
            _say(f"    - {note}")


def _fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def cmd_client(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    _, refusal = _wsl_gate(args.allow_wsl)
    if refusal is not None:
        return refusal
    try:
        server = _check_server(args.server, allow_any=args.allow_any)
    except ValueError as exc:
        parser.error(str(exc))
    if args.reps < MIN_VALID_REPS:
        _say(
            f"warning: --reps {args.reps} < {MIN_VALID_REPS}: every cell will be INVALID"
        )
    if not args.no_inbound_rule:
        _say(
            "note: pass --no-inbound-rule once you have confirmed this machine has no "
            "inbound firewall rule for the spike ports; without it the outbound-only "
            "verdict stays TBD."
        )
    _warn_firewall()
    cfg = ClientConfig(
        server=server,
        http_port=args.http_port,
        grpc_port=args.grpc_port,
        link=args.link,
        reps=args.reps,
        warmup=args.warmup,
        blob_bytes=args.blob_bytes,
        hold_s=args.hold,
        ping_interval_s=args.ping_interval,
        idle_gaps_s=args.idle_gaps,
        keepalives=args.keepalive,
        connect_timeout_s=args.connect_timeout,
        io_timeout_s=args.io_timeout,
        pause_s=args.pause,
        name=args.name,
        no_inbound_rule=args.no_inbound_rule,
        note=args.note,
    )
    _say(f"estimated run time: about {cfg.estimated_seconds() / 60:.0f} min")
    today = datetime.now(UTC).date()
    out_dir: Path = args.out_dir or raw_dir(TOPIC, today)
    run = run_client(cfg, log=_say)
    path = (
        out_dir
        / raw_result_path(
            TOPIC, run.client_name, today, suffix=f"to-{run.server_name}_{args.link}"
        ).name
    )
    try:
        write_envelope(run.envelope, path)
    except OSError as exc:
        _say(f"client: cannot write {path}: {exc}")
        return EXIT_IO
    print_summary(run)
    _say(f"written: {path}")
    return EXIT_OK if run.complete else EXIT_INCOMPLETE


# --------------------------------------------------------------------- report
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
    envelopes: list[dict[str, Any]] = []
    skipped: list[str] = []
    for d in dirs:
        docs, bad = load_dir(d)
        envelopes += [e for e in docs if e.get("spike") == "S2"]
        skipped += bad
    for reason in skipped:
        _say(f"skipped invalid result file: {reason}")
    if not envelopes:
        _say("warning: no S2 result files found; every cell will be TBD")
    # ``--hosts L1=hostA`` both *renames* hostA to L1 in every row and says
    # which directions were expected; without the rename the expected list
    # would never match the measured rows and every direction would be TBD.
    names = {host.casefold(): label for host, label in args.hosts.items()}
    verdicts = direction_verdicts(envelopes, names)
    labels = sorted(set(names.values())) or sorted(
        {v.client for v in verdicts.values()} | {v.server for v in verdicts.values()}
    )
    extra = render_verdicts(verdicts, expected_directions(labels))
    if skipped:
        extra += (
            "\nSkipped result files:\n" + "\n".join(f"- {s}" for s in skipped) + "\n"
        )
    day = _report_day(dirs, args.date).isoformat()
    try:
        text = render_report(
            args.template.read_text(encoding="utf-8"),
            envelopes,
            topic=TOPIC,
            title="S2 cross-OS reachability",
            day=day,
            task="0.2.6",
            spike="S2",
            extra_results=sanitize_text(extra),
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
        if args.command == "serve":
            return cmd_serve(args, parser)
        if args.command == "client":
            return cmd_client(args, parser)
        return cmd_report(args, parser)
    except KeyboardInterrupt:
        _say("interrupted (Ctrl-C)")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
