"""Spike S3 (roadmap 0.2.7): transport throughput - raw TCP vs gRPC vs HTTP.

Answers ARCHITECTURE §27 Q2 with numbers: how much of a plain TCP connection do
we keep when blobs go over HTTP (ADR-0003) and tensors over gRPC byte streams
(ADR-0005), from 1 MiB to 2 GiB. Standard library plus ``grpcio`` (a core
dependency), so it runs after a plain ``uv sync`` on Linux and native Windows.

Three subcommands, run from the repo root::

    # on the laptop that serves the bytes, bound to its lab IP
    # (opens three ports: 47301 raw TCP + control, 47302 gRPC, 47303 HTTP):
    uv run python lab/spikes/s3_transport.py serve --bind 192.168.1.20
    # on the other laptop of the pair:
    uv run python lab/spikes/s3_transport.py client --server 192.168.1.20 --link wifi
    # anywhere, after collecting the result JSONs of every pair in one folder:
    uv run python lab/spikes/s3_transport.py report \\
        --results lab/reports/raw/2026-09-20-S3-transport \\
        --out lab/reports/2026-09-20-S3-transport.md

The client measures, per payload size: raw TCP down/up, gRPC server-stream /
client-stream, HTTP GET and HTTP parallel ``Range`` requests - plus a gRPC
chunk-size sweep and this laptop's loopback ceiling per transport. Every
transfer is verified with a CRC-32 over the deterministic payload, so a fast
transport that loses bytes fails instead of winning. Results:
``lab/reports/raw/<today>-S3-transport/<client>__to-<server>_<link>.json``.

Exit codes: ``0`` done; ``1`` could not bind / write output; ``2`` usage error;
``3`` results written but incomplete (server lost, failed repetitions - see
``errors`` in the JSON); ``130`` interrupted (Ctrl-C).
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
    sanitize_text,
    write_envelope,
)
from spikes._harness.report import (  # noqa: E402
    TEMPLATE_PATH,
    TemplateError,
    render_report,
)
from spikes._s3.ceiling import loopback_ceiling  # noqa: E402
from spikes._s3.context import ServerLimits  # noqa: E402
from spikes._s3.protocol import (  # noqa: E402
    CHUNK_SWEEP,
    DEFAULT_CHUNK_BYTES,
    DEFAULT_PORT,
    DEFAULT_RANGE_PARTS,
    DEFAULT_SIZES,
    GRPC_PORT_OFFSET,
    HTTP_PORT_OFFSET,
    MAX_RANGE_PARTS,
    MIB,
    check_bind_address,
    format_size,
    is_lan_address,
    parse_chunk,
    parse_size,
    parse_sizes,
)
from spikes._s3.run import TOPIC, ClientConfig, ClientRun, run_client  # noqa: E402
from spikes._s3.server import S3Server  # noqa: E402
from spikes._s3.tables import render_tables  # noqa: E402

SCRIPT: Final = Path(__file__).resolve()
EXIT_OK: Final = 0
EXIT_IO: Final = 1
EXIT_USAGE: Final = 2
EXIT_INCOMPLETE: Final = 3
EXIT_INTERRUPTED: Final = 130
_DAY_DIR_RE: Final = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
_NAME_RE: Final = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
DEFAULT_CEILING_SIZE: Final = 64 * MIB


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
    return int(_number(0, 65535 - HTTP_PORT_OFFSET, integer=True)(text))


def _name(text: str) -> str:
    if not _NAME_RE.match(text):
        raise argparse.ArgumentTypeError("use 1-64 characters from A-Z a-z 0-9 . _ -")
    return text


def _size(text: str) -> int:
    try:
        return parse_size(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _sizes(text: str) -> tuple[int, ...]:
    try:
        return parse_sizes(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _chunk(text: str) -> int:
    try:
        return parse_chunk(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _chunks(text: str) -> tuple[int, ...]:
    values = tuple(sorted({_chunk(part) for part in text.split(",") if part.strip()}))
    if not values:
        raise argparse.ArgumentTypeError("no chunk sizes given")
    return values


def _ceiling_size(text: str) -> int:
    if text.strip() in {"0", "off", "none"}:
        return 0
    return _size(text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lab/spikes/s3_transport.py",
        description="Spike S3: transport throughput (raw TCP vs gRPC vs HTTP range).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="serve bytes over all three transports")
    serve.add_argument("--bind", required=True, help="this laptop's lab IP address")
    serve.add_argument(
        "--port",
        type=_port,
        default=DEFAULT_PORT,
        help=f"raw TCP port; gRPC uses +{GRPC_PORT_OFFSET}, HTTP +{HTTP_PORT_OFFSET} "
        "(0 = three arbitrary free ports)",
    )
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
        help="stop after this long without any traffic (default %(default)g)",
    )
    serve.add_argument(
        "--duration",
        type=_number(1, 86400),
        default=7200.0,
        metavar="SECONDS",
        help="stop after this long in total (default %(default)g)",
    )
    serve.add_argument(
        "--header-timeout",
        type=_number(0.1, 60),
        default=5.0,
        metavar="SECONDS",
        help="drop a connection without a complete request header in this time",
    )
    serve.add_argument(
        "--io-timeout",
        type=_number(0.5, 600),
        default=30.0,
        metavar="SECONDS",
        help="per socket operation (default %(default)g)",
    )
    serve.add_argument(
        "--name", type=_name, help="identity shown in the report (default: hostname)"
    )
    serve.add_argument(
        "--ready-file",
        type=Path,
        help="write the bound ports (JSON) here once listening",
    )
    serve.add_argument("--quiet", action="store_true", help="no progress output")

    client = sub.add_parser("client", help="measure one or more servers from here")
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
        help="warmup repetitions per case (recorded, never summarised)",
    )
    client.add_argument(
        "--sizes",
        type=_sizes,
        default=DEFAULT_SIZES,
        help="payload sizes, comma-separated (default "
        + ",".join(format_size(s) for s in DEFAULT_SIZES)
        + ")",
    )
    client.add_argument(
        "--max-size",
        type=_size,
        default=max(DEFAULT_SIZES),
        help="skip sizes above this (little RAM/disk/time; default %(default)s bytes)",
    )
    client.add_argument(
        "--chunk",
        type=_chunk,
        default=DEFAULT_CHUNK_BYTES,
        help=f"application write / gRPC message size (default {format_size(DEFAULT_CHUNK_BYTES)})",
    )
    client.add_argument(
        "--chunk-sweep",
        type=_chunks,
        default=CHUNK_SWEEP,
        help="gRPC chunk sizes to sweep (default "
        + ",".join(format_size(c) for c in CHUNK_SWEEP)
        + ")",
    )
    client.add_argument(
        "--chunk-sweep-size",
        type=_size,
        default=64 * MIB,
        help="payload size used for the gRPC chunk sweep (default 64MiB)",
    )
    client.add_argument(
        "--range-parts",
        type=_number(1, MAX_RANGE_PARTS, integer=True),
        default=DEFAULT_RANGE_PARTS,
        help="parallel HTTP Range requests (default %(default)d)",
    )
    client.add_argument(
        "--no-uploads",
        action="store_true",
        help="measure only the server -> client direction (halves the run time)",
    )
    client.add_argument(
        "--pause",
        type=_number(0, 60),
        default=0.5,
        metavar="SECONDS",
        help="pause between repetitions (default %(default)g)",
    )
    client.add_argument(
        "--connect-timeout", type=_number(0.1, 60), default=5.0, metavar="SECONDS"
    )
    client.add_argument(
        "--io-timeout", type=_number(0.5, 600), default=30.0, metavar="SECONDS"
    )
    client.add_argument(
        "--transfer-timeout",
        type=_number(1, 7200),
        default=600.0,
        metavar="SECONDS",
        help="hard cap on one repetition (default %(default)g)",
    )
    client.add_argument(
        "--ceiling-size",
        type=_ceiling_size,
        default=DEFAULT_CEILING_SIZE,
        help="payload for the loopback ceiling check per transport (0 = skip)",
    )
    client.add_argument("--ceiling-reps", type=_number(1, 50, integer=True), default=3)
    client.add_argument(
        "--name", type=_name, help="identity shown in the report (default: hostname)"
    )
    client.add_argument(
        "--out-dir",
        type=Path,
        help=f"result folder (default {RAW_ROOT.name}/<today>-{TOPIC} under lab/reports)",
    )
    client.add_argument(
        "--allow-any", action="store_true", help="allow a non-private server address"
    )

    report = sub.add_parser(
        "report", help="build the S3 report (tables + Q2 answer) from result JSONs"
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
        "--date",
        type=date.fromisoformat,
        help="report date YYYY-MM-DD (default: from the folder name, else today)",
    )
    report.add_argument(
        "--template", type=Path, default=TEMPLATE_PATH, help=argparse.SUPPRESS
    )
    return parser


# ---------------------------------------------------------------------- serve
def cmd_serve(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        bind = check_bind_address(args.bind, allow_any=args.allow_any)
    except ValueError as exc:
        parser.error(str(exc))
    log = (lambda _m: None) if args.quiet else _say
    server = S3Server(
        bind,
        args.port,
        ServerLimits(
            duration_s=args.duration,
            idle_s=args.idle,
            header_timeout_s=args.header_timeout,
            io_timeout_s=args.io_timeout,
        ),
        name=args.name,
        log=log,
    )
    try:
        ports = server.open()
    except OSError as exc:
        _say(f"serve: cannot listen on {bind} (ports {args.port}+): {exc}")
        return EXIT_IO
    if sys.platform != "win32":

        def _on_term(_sig: int, _frame: FrameType | None) -> None:
            server.stop()

        signal.signal(signal.SIGTERM, _on_term)
    if args.ready_file is not None:
        try:
            server.write_ready_file(args.ready_file)
        except OSError as exc:
            server.stop()
            server.close()
            _say(f"serve: cannot write --ready-file: {exc}")
            return EXIT_IO
    log(
        f"S3 server '{server.ctx.name}' listening on {bind}: raw TCP {ports['tcp']}, "
        f"gRPC {ports['grpc']}, HTTP {ports['http']}; LAN clients only; stops after "
        f"{args.idle:g} s idle or {args.duration:g} s total. Ctrl-C to stop."
    )
    log("Remember: remove the temporary firewall rule when the spike is finished.")
    try:
        reason = server.serve()
    except KeyboardInterrupt:
        server.stop()
        log("interrupted (Ctrl-C); server stopped")
        return EXIT_OK
    log(f"server stopped: {reason}. {server.summary()}")
    for error in server.ctx.counters.errors[:10]:
        log(f"  error: {error}")
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
    _say(f"--- S3 summary [{doc['link']}] {run.client_name} ↔ {run.server_name} ---")
    for case_id in sorted(doc["cases"]):
        case = doc["cases"][case_id]
        n_ok = len(case["samples"])
        flag = "" if n_ok >= max(reps, MIN_VALID_REPS) else f"  INVALID (n={n_ok})"
        summary = case["summary"]
        _say(
            f"{case['label']}: n={n_ok}{flag}\n    "
            f"{_fmt(summary['recv_mbps']['mean'], 1)} ± "
            f"{_fmt(summary['recv_mbps']['stdev'], 1)} Mbit/s, "
            f"{_fmt(summary['recv_mib_s']['mean'], 1)} MiB/s, CPU "
            f"{_fmt(summary['cpu_ms_per_mib']['mean'], 2)} ms/MiB"
        )
    for err in doc["errors"]:
        _say(f"run error: {err['failure_type']}: {err['message']}")


def cmd_client(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        servers = [_check_server(s, allow_any=args.allow_any) for s in args.server]
    except ValueError as exc:
        parser.error(str(exc))
    if args.reps < MIN_VALID_REPS:
        _say(
            f"warning: --reps {args.reps} < {MIN_VALID_REPS}: every row will be INVALID"
        )
    today = datetime.now(UTC).date()
    out_dir: Path = args.out_dir or raw_dir(TOPIC, today)
    ceiling = None
    if args.ceiling_size > 0:
        _say(
            f"loopback ceiling per transport ({format_size(args.ceiling_size)} x "
            f"{args.ceiling_reps} reps) ..."
        )
        ceiling = loopback_ceiling(
            SCRIPT,
            size=args.ceiling_size,
            chunk=args.chunk,
            reps=args.ceiling_reps,
            timeout_s=args.transfer_timeout,
        )
        if ceiling.get("ok"):
            for case_id, item in sorted(ceiling["per_transport"].items()):
                _say(f"  ceiling {case_id}: {_fmt(item.get('mean_mbps'), 0)} Mbit/s")
            _say(
                "LAN results close to these are limited by this tool (Python, framing, "
                "CRC verification), not by the link."
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
            sizes=tuple(args.sizes),
            max_size=args.max_size,
            chunk=args.chunk,
            chunk_sweep=tuple(args.chunk_sweep),
            chunk_sweep_size=args.chunk_sweep_size,
            range_parts=args.range_parts,
            uploads=not args.no_uploads,
            pause_s=args.pause,
            connect_timeout_s=args.connect_timeout,
            io_timeout_s=args.io_timeout,
            transfer_timeout_s=args.transfer_timeout,
            name=args.name,
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


# --------------------------------------------------------------------- report
def _report_day(dirs: Sequence[Path], explicit: date | None) -> date:
    if explicit is not None:
        return explicit
    for d in dirs:
        m = _DAY_DIR_RE.match(d.name)
        if m:
            with contextlib.suppress(ValueError):
                return date.fromisoformat(m.group(1))
    return datetime.now(UTC).date()


def cmd_report(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    dirs: list[Path] = args.results
    missing = [str(d) for d in dirs if not d.is_dir()]
    if missing:
        parser.error(f"--results folder not found: {', '.join(missing)}")
    envelopes = []
    skipped: list[str] = []
    for d in dirs:
        docs, bad = load_dir(d)
        envelopes += [e for e in docs if e.get("spike") == "S3"]
        skipped += bad
    for reason in skipped:
        _say(f"skipped invalid result file: {reason}")
    if not envelopes:
        _say("warning: no S3 result files found; every cell will be TBD")
    day = _report_day(dirs, args.date).isoformat()
    extra = render_tables(envelopes)
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
            title="S3 transport throughput",
            day=day,
            task="0.2.7",
            spike="S3",
            # envelopes are sanitised when written; the extra markdown (skip
            # reasons, labels) is free text, so it goes through the same filter
            extra_results=sanitize_text(extra),
        )
    except (OSError, TemplateError) as exc:
        _say(f"report: cannot build the report: {exc}")
        return EXIT_IO
    try:
        atomic_write_text(args.out, text)
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
