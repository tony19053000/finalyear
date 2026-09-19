"""Spike S1 (roadmap 0.2.5): network matrix - TCP bandwidth, RTT, jitter, loss.

Standard library only; runs after a plain ``uv sync`` on Linux and native
Windows. Three subcommands (run from the repo root)::

    # on the laptop being measured (the "server"), bound to its lab IP:
    uv run python lab/spikes/s1_network.py serve --bind 192.168.1.20
    # on the other laptop of the pair:
    uv run python lab/spikes/s1_network.py client --server 192.168.1.20 --link wifi
    # anywhere, after collecting every host's JSON in one folder:
    uv run python lab/spikes/s1_network.py matrix \\
        --results lab/reports/raw/2026-09-20-S1-network \\
        --out lab/reports/2026-09-20-S1-network.md --labels L1=host1,L2=host2

``client`` measures both directions of the pair (upload = client sends,
download = server sends) with 1 and 4 TCP streams, bytes counted at the
receiver, plus UDP echo RTT / jitter / loss, ``--warmup`` + ``--reps``
repetitions each. It writes one result envelope per server to
``lab/reports/raw/<today>-S1-network/<client>__to-<server>_<link>.json``.

Exit codes: ``0`` done (for ``client``: every case complete); ``1`` could not
bind / write output; ``2`` usage error; ``3`` (``client``) results written but
incomplete (server lost, failed repetitions - see ``errors`` in the JSON);
``130`` interrupted (Ctrl-C).
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import re
import signal
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from types import FrameType
from typing import Final

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
    write_envelope,
)
from spikes._harness.report import (  # noqa: E402
    TEMPLATE_PATH,
    TemplateError,
    render_report,
)
from spikes._s1 import iperf  # noqa: E402
from spikes._s1.ceiling import loopback_ceiling  # noqa: E402
from spikes._s1.client import socket_buffer_defaults  # noqa: E402
from spikes._s1.matrix import render_matrix  # noqa: E402
from spikes._s1.protocol import (  # noqa: E402
    DEFAULT_PORT,
    MAX_PING_BYTES,
    MAX_SECONDS,
    MAX_STREAMS,
    MIN_PING_BYTES,
    MIN_SECONDS,
    check_bind_address,
    is_lan_address,
)
from spikes._s1.run import TOPIC, ClientConfig, ClientRun, run_client  # noqa: E402
from spikes._s1.server import S1Server, ServerLimits  # noqa: E402

SCRIPT: Final = Path(__file__).resolve()
EXIT_OK: Final = 0
EXIT_IO: Final = 1
EXIT_USAGE: Final = 2
EXIT_INCOMPLETE: Final = 3
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
    value = _number(0, 65535, integer=True)(text)
    return int(value)


def _streams(text: str) -> tuple[int, ...]:
    try:
        values = tuple(sorted({int(p) for p in text.split(",") if p.strip()}))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"comma-separated integers expected: {text!r}"
        ) from None
    if not values or not all(1 <= v <= MAX_STREAMS for v in values):
        raise argparse.ArgumentTypeError(f"each stream count must be 1..{MAX_STREAMS}")
    return values


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
        prog="lab/spikes/s1_network.py",
        description="Spike S1: network matrix (TCP bandwidth both ways, RTT, jitter, loss).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the measurement server on this laptop")
    serve.add_argument("--bind", required=True, help="this laptop's lab IP address")
    serve.add_argument(
        "--port", type=_port, default=DEFAULT_PORT, help="TCP+UDP port (0 = any free)"
    )
    serve.add_argument(
        "--allow-any",
        action="store_true",
        help="allow binding the wildcard (0.0.0.0/::) or a non-private address",
    )
    serve.add_argument(
        "--idle",
        type=_number(1, 86400),
        default=600.0,
        metavar="SECONDS",
        help="stop after this long without any client (default %(default)g)",
    )
    serve.add_argument(
        "--duration",
        type=_number(1, 86400),
        default=3600.0,
        metavar="SECONDS",
        help="stop after this long in total (default %(default)g)",
    )
    serve.add_argument(
        "--header-timeout",
        type=_number(0.1, 60),
        default=2.0,
        metavar="SECONDS",
        help="drop a connection that has not sent its complete request header "
        "within this time (default 2)",
    )
    serve.add_argument(
        "--name", type=_name, help="identity shown in the matrix (default: hostname)"
    )
    serve.add_argument(
        "--ready-file", type=Path, help="write the bound port here once listening"
    )
    serve.add_argument("--quiet", action="store_true", help="no progress output")

    client = sub.add_parser(
        "client", help="measure one or more servers from this laptop"
    )
    client.add_argument(
        "--server",
        action="append",
        required=True,
        metavar="IP",
        help="server lab IP; repeat to measure several servers one after another",
    )
    client.add_argument("--port", type=_port, default=DEFAULT_PORT)
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
        "--warmup",
        type=_number(0, 100, integer=True),
        default=1,
        help="unrecorded-in-summary warmup repetitions per case",
    )
    client.add_argument(
        "--seconds",
        type=_number(MIN_SECONDS, MAX_SECONDS),
        default=10.0,
        help="duration of one TCP repetition (default %(default)g)",
    )
    client.add_argument(
        "--streams",
        type=_streams,
        default=(1, 4),
        help="parallel TCP stream counts, comma-separated (default 1,4)",
    )
    client.add_argument(
        "--pings",
        type=_number(2, 100000, integer=True),
        default=200,
        help="UDP pings per repetition (default %(default)d)",
    )
    client.add_argument(
        "--ping-interval",
        type=_number(0.001, 1.0),
        default=0.02,
        metavar="SECONDS",
        help="gap between pings (default %(default)g)",
    )
    client.add_argument(
        "--ping-size",
        type=_number(MIN_PING_BYTES, MAX_PING_BYTES, integer=True),
        default=64,
        metavar="BYTES",
    )
    client.add_argument(
        "--ping-timeout",
        type=_number(0.05, 10.0),
        default=1.0,
        metavar="SECONDS",
        help="wait for late replies after the last ping",
    )
    client.add_argument(
        "--pause",
        type=_number(0, 60),
        default=1.0,
        metavar="SECONDS",
        help="pause between TCP repetitions (default %(default)g)",
    )
    client.add_argument(
        "--connect-timeout", type=_number(0.1, 60), default=5.0, metavar="SECONDS"
    )
    client.add_argument(
        "--io-timeout", type=_number(0.5, 120), default=10.0, metavar="SECONDS"
    )
    client.add_argument(
        "--ceiling-seconds",
        type=_number(0, 60),
        default=3.0,
        metavar="SECONDS",
        help="loopback ceiling check before measuring (0 = skip)",
    )
    client.add_argument(
        "--iperf3",
        action="store_true",
        help="also run iperf3 once per direction (needs `iperf3 -s` on the server)",
    )
    client.add_argument("--iperf3-port", type=_port, default=iperf.DEFAULT_IPERF3_PORT)
    client.add_argument(
        "--name", type=_name, help="identity shown in the matrix (default: hostname)"
    )
    client.add_argument(
        "--out-dir",
        type=Path,
        help=f"result folder (default {RAW_ROOT.name}/<today>-{TOPIC} under lab/reports)",
    )
    client.add_argument(
        "--allow-any", action="store_true", help="allow a non-private server address"
    )

    matrix = sub.add_parser(
        "matrix", help="build the report + LAB.md §2 matrices from result JSONs"
    )
    matrix.add_argument(
        "--results",
        action="append",
        required=True,
        type=Path,
        metavar="DIR",
        help="folder with client result JSONs (repeatable)",
    )
    matrix.add_argument(
        "--out", required=True, type=Path, help="markdown report to write"
    )
    matrix.add_argument(
        "--labels",
        type=_labels,
        default={},
        help="short names, e.g. L1=hostA,L2=hostB,L3=hostC,L4=hostD",
    )
    matrix.add_argument(
        "--date",
        type=date.fromisoformat,
        help="report date YYYY-MM-DD (default: from the folder name, else today)",
    )
    matrix.add_argument(
        "--template", type=Path, default=TEMPLATE_PATH, help=argparse.SUPPRESS
    )
    return parser


# ---------------------------------------------------------------------- serve
def _write_ready(path: Path, port: int) -> None:
    atomic_write_text(path, str(port), encoding="ascii")


def cmd_serve(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        bind = check_bind_address(args.bind, allow_any=args.allow_any)
    except ValueError as exc:
        parser.error(str(exc))
    log = (lambda _m: None) if args.quiet else _say
    server = S1Server(
        bind,
        args.port,
        ServerLimits(
            duration_s=args.duration,
            idle_s=args.idle,
            header_timeout_s=args.header_timeout,
        ),
        log=log,
        name=args.name,
    )
    try:
        port = server.open()
    except OSError as exc:
        _say(f"serve: cannot listen on {bind}:{args.port} (TCP+UDP): {exc}")
        return EXIT_IO
    if sys.platform != "win32":

        def _on_term(_sig: int, _frame: FrameType | None) -> None:
            server.stop()

        signal.signal(signal.SIGTERM, _on_term)
    if args.ready_file is not None:
        try:
            _write_ready(args.ready_file, port)
        except OSError as exc:
            server.stop()
            server.serve()  # closes the sockets at once
            _say(f"serve: cannot write --ready-file: {exc}")
            return EXIT_IO
    log(
        f"S1 server '{server.name}' listening on {bind} port {port} (TCP + UDP); "
        f"LAN clients only; stops after {args.idle:g} s idle or {args.duration:g} s total. "
        "Ctrl-C to stop."
    )
    log("Remember: remove the temporary firewall rule when the spike is finished.")
    try:
        reason = server.serve()
    except KeyboardInterrupt:
        log("interrupted (Ctrl-C); server stopped")
        return EXIT_OK
    c = server.counters
    log(
        f"server stopped: {reason}. TCP accepted {c.tcp_accepted}, refused non-LAN "
        f"{c.tcp_refused_peer}, refused busy {c.tcp_refused_busy} "
        f"(evicted without header {c.tcp_evicted_pending}), header timeouts "
        f"{c.tcp_header_timeouts}; UDP echoed "
        f"{c.udp_echoed}, dropped {c.udp_dropped}; errors {len(c.errors)}"
    )
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


def _fmt(value: object, digits: int) -> str:
    return f"{value:.{digits}f}" if isinstance(value, int | float) else "—"


def print_summary(run: ClientRun, reps: int) -> None:
    doc = run.envelope
    _say(f"--- S1 summary [{doc['link']}] {run.client_name} ↔ {run.server_name} ---")
    for case_id in sorted(doc["cases"]):
        case = doc["cases"][case_id]
        n_ok = len(case["samples"])
        flag = "" if n_ok >= max(reps, MIN_VALID_REPS) else f"  INVALID (n={n_ok})"
        parts = []
        for metric, s in sorted(case["summary"].items()):
            parts.append(
                f"{metric} {_fmt(s['mean'], 3)} ± {_fmt(s['stdev'], 3)} {case['metrics'][metric]}"
            )
        _say(f"{case['label']}: n={n_ok}{flag}\n    " + "\n    ".join(parts))
    for err in doc["errors"]:
        _say(f"run error: {err['failure_type']}: {err['message']}")


def cmd_client(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        servers = [_check_server(s, allow_any=args.allow_any) for s in args.server]
    except ValueError as exc:
        parser.error(str(exc))
    if args.reps < MIN_VALID_REPS:
        _say(
            f"warning: --reps {args.reps} < {MIN_VALID_REPS}: every cell will be INVALID"
        )
    today = datetime.now(UTC).date()
    out_dir: Path = args.out_dir or raw_dir(TOPIC, today)
    buffers = socket_buffer_defaults()
    _say(
        f"buffers: app send chunk {buffers['app_send_chunk']} B, app recv buffer "
        f"{buffers['app_recv_buffer']} B, kernel SO_SNDBUF/SO_RCVBUF initial "
        f"{buffers['so_sndbuf_initial']}/{buffers['so_rcvbuf_initial']} B (autotuned by the OS)"
    )
    ceiling = None
    if args.ceiling_seconds > 0:
        _say(f"loopback ceiling check ({args.ceiling_seconds:g} s, 1 stream) ...")
        ceiling = loopback_ceiling(SCRIPT, args.ceiling_seconds)
        if ceiling.get("ok"):
            _say(
                f"loopback ceiling: {ceiling['receiver_mbps']:.0f} Mbit/s. LAN results near "
                "this are limited by this tool/CPU, not the link (cross-check with --iperf3)."
            )
        else:
            _say(f"loopback ceiling check failed: {ceiling.get('error')}")
    worst = EXIT_OK
    for server in servers:
        cfg = ClientConfig(
            server=server,
            port=args.port,
            link=args.link,
            reps=args.reps,
            warmup=args.warmup,
            seconds=args.seconds,
            streams=args.streams,
            pings=args.pings,
            ping_interval_s=args.ping_interval,
            ping_size=args.ping_size,
            ping_timeout_s=args.ping_timeout,
            pause_s=args.pause,
            connect_timeout_s=args.connect_timeout,
            io_timeout_s=args.io_timeout,
            name=args.name,
            iperf3=args.iperf3,
            iperf3_port=args.iperf3_port,
        )
        run = run_client(cfg, log=_say, ceiling=ceiling)
        path = (
            out_dir
            / raw_result_path(
                TOPIC,
                run.client_name,
                today,
                suffix=f"to-{run.server_name}_{args.link}",
            ).name
        )
        try:
            write_envelope(run.envelope, path)
        except OSError as exc:
            _say(f"client: cannot write {path}: {exc}")
            return EXIT_IO
        print_summary(run, args.reps)
        _say(f"written: {path}")
        if not run.complete:
            worst = EXIT_INCOMPLETE
    return worst


# --------------------------------------------------------------------- matrix
def _report_day(dirs: Sequence[Path], explicit: date | None) -> date:
    if explicit is not None:
        return explicit
    for d in dirs:
        m = _DAY_DIR_RE.match(d.name)
        if m:
            with contextlib.suppress(ValueError):
                return date.fromisoformat(m.group(1))
    return datetime.now(UTC).date()


def cmd_matrix(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    dirs: list[Path] = args.results
    missing = [str(d) for d in dirs if not d.is_dir()]
    if missing:
        parser.error(f"--results folder not found: {', '.join(missing)}")
    envelopes = []
    skipped: list[str] = []
    for d in dirs:
        docs, bad = load_dir(d)
        envelopes += [e for e in docs if e.get("spike") == "S1"]
        skipped += bad
    for reason in skipped:
        _say(f"skipped invalid result file: {reason}")
    if not envelopes:
        _say("warning: no S1 result files found; every cell will be TBD")
    day = _report_day(dirs, args.date).isoformat()
    try:
        template = args.template.read_text(encoding="utf-8")
        text = render_report(
            template,
            envelopes,
            topic=TOPIC,
            title="S1 network matrix",
            day=day,
            task="0.2.5",
            spike="S1",
            extra_results=render_matrix(envelopes, args.labels)
            + (
                "\nSkipped result files:\n"
                + "\n".join(f"- {s}" for s in skipped)
                + "\n"
                if skipped
                else ""
            ),
        )
    except (OSError, TemplateError) as exc:
        _say(f"matrix: cannot build the report: {exc}")
        return EXIT_IO
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        _say(f"matrix: cannot write {args.out}: {exc}")
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
        return cmd_matrix(args, parser)
    except KeyboardInterrupt:
        _say("interrupted (Ctrl-C)")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
