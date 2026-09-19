"""One S3 client run against one server -> a result envelope (no CLI coupling).

Order: ``hello`` (identity, ports, reachability) -> for every payload size, the
same six transfers (raw TCP down/up, gRPC down/up, HTTP GET, HTTP parallel
ranges) -> the gRPC chunk-size sweep at one size -> a one-shot probe of gRPC's
*default* 4 MB message limit. Each case is ``warmup + reps`` repetitions through
the shared harness; a fatal failure aborts that case, and if the server has
really gone the remaining cases are skipped and recorded, so a dead peer costs a
bounded time instead of ``reps x cases`` timeouts.

Sizes ascend, so a laptop that runs out of patience or link speed still has the
small sizes complete, and ``--max-size`` cuts the tail off deterministically.
"""

from __future__ import annotations

import functools
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from spikes._harness import MIN_VALID_REPS
from spikes._harness.envelope import interpreter, make_envelope
from spikes._harness.reps import CaseResult, RepError, Sample, run_reps
from spikes._s3 import payload
from spikes._s3.client import (
    Endpoint,
    grpc_download,
    grpc_upload,
    hello,
    http_download,
    tcp_download,
    tcp_upload,
)
from spikes._s3.grpc_server import grpc_version
from spikes._s3.protocol import (
    CHUNK_SWEEP,
    DEFAULT_CHUNK_BYTES,
    DEFAULT_RANGE_PARTS,
    DEFAULT_SIZES,
    GRPC_DEFAULT_LIMIT,
    GRPC_PORT_OFFSET,
    HTTP_PORT_OFFSET,
    MIB,
    PROTOCOL,
    FailureType,
    Message,
    TransferError,
    format_size,
    select_sizes,
)

SPIKE: Final = "S3"
TOPIC: Final = "S3-transport"
#: metric -> unit (the report shows one row per case x metric)
METRICS: Final = {
    "recv_mbps": "Mbit/s",
    "recv_mib_s": "MiB/s",
    "wall_s": "s",
    "ttfb_s": "s",
    "client_cpu_s": "s",
    "server_cpu_s": "s",
    "cpu_ms_per_mib": "ms/MiB",
    "client_rss_peak_mib": "MiB",
    "server_rss_peak_mib": "MiB",
}

type Log = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class ClientConfig:
    server: str
    port: int
    link: str
    reps: int = 5
    warmup: int = 1
    sizes: tuple[int, ...] = DEFAULT_SIZES
    max_size: int = max(DEFAULT_SIZES)
    chunk: int = DEFAULT_CHUNK_BYTES
    chunk_sweep: tuple[int, ...] = CHUNK_SWEEP
    chunk_sweep_size: int = 64 * MIB
    range_parts: int = DEFAULT_RANGE_PARTS
    uploads: bool = True
    pause_s: float = 0.5
    connect_timeout_s: float = 5.0
    io_timeout_s: float = 30.0
    transfer_timeout_s: float = 600.0
    name: str | None = None

    @property
    def endpoint(self) -> Endpoint:
        """The endpoint before ``hello``: the +1/+2 port convention."""
        return Endpoint(
            self.server,
            self.port,
            self.port + GRPC_PORT_OFFSET,
            self.port + HTTP_PORT_OFFSET,
            self.connect_timeout_s,
            self.io_timeout_s,
            self.transfer_timeout_s,
        )

    def endpoint_from_hello(self, reply: Message) -> Endpoint:
        """The endpoint with the ports the server *actually* listens on.

        A server started with ``--port 0`` (tests, the loopback ceiling) has
        arbitrary gRPC/HTTP ports, and a server without grpcio reports ``0`` for
        gRPC; the ``hello`` reply is the truth, the +1/+2 convention the default.
        """
        ports = reply.get("ports")
        base = self.endpoint
        if not isinstance(ports, dict):
            return base
        return Endpoint(
            base.host,
            base.tcp_port,
            _port_or(ports.get("grpc"), base.grpc_port),
            _port_or(ports.get("http"), base.http_port),
            base.connect_timeout_s,
            base.io_timeout_s,
            base.transfer_timeout_s,
        )

    @property
    def selected_sizes(self) -> tuple[int, ...]:
        return select_sizes(self.sizes, self.max_size)


def _port_or(value: object, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 65536:
        return value
    return default


@dataclass(slots=True)
class ClientRun:
    envelope: dict[str, Any]
    client_name: str
    server_name: str
    #: every planned case produced ``reps`` samples and no error was recorded
    complete: bool
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Case:
    case_id: str
    label: str
    attrs: dict[str, Any]
    measure: Callable[[Endpoint], Sample]


def _as_rep_error(exc: TransferError) -> RepError:
    return RepError(str(exc.failure_type), str(exc), fatal=exc.fatal)


def _guard(fn: Callable[[Endpoint], Sample], ep: Endpoint) -> Callable[[int], Sample]:
    def measure(_index: int) -> Sample:
        try:
            return fn(ep)
        except TransferError as exc:
            raise _as_rep_error(exc) from exc

    return measure


def build_cases(cfg: ClientConfig) -> list[Case]:
    """The full case plan, smallest payload first."""
    cases: list[Case] = []
    for size in cfg.selected_sizes:
        label = format_size(size)
        chunk = cfg.chunk
        plan: list[tuple[str, str, dict[str, Any], Callable[[Endpoint], Sample]]] = [
            (
                f"tcp-down-{label}",
                f"raw TCP download {label}",
                {"transport": "tcp", "direction": "download"},
                functools.partial(tcp_download, size=size, chunk=chunk),
            ),
            (
                f"grpc-down-{label}",
                f"gRPC server-stream download {label} (chunk {format_size(chunk)})",
                {"transport": "grpc", "direction": "download"},
                functools.partial(grpc_download, size=size, chunk=chunk),
            ),
            (
                f"http-get-{label}",
                f"HTTP GET {label}",
                {"transport": "http", "direction": "download", "ranged": False},
                functools.partial(http_download, size=size, chunk=chunk),
            ),
            (
                f"http-range{cfg.range_parts}-{label}",
                f"HTTP {cfg.range_parts} parallel Range requests {label}",
                {
                    "transport": "http",
                    "direction": "download",
                    "ranged": True,
                    "parts": cfg.range_parts,
                },
                functools.partial(
                    http_download, size=size, chunk=chunk, parts=cfg.range_parts
                ),
            ),
        ]
        if cfg.uploads:
            plan += [
                (
                    f"tcp-up-{label}",
                    f"raw TCP upload {label}",
                    {"transport": "tcp", "direction": "upload"},
                    functools.partial(tcp_upload, size=size, chunk=chunk),
                ),
                (
                    f"grpc-up-{label}",
                    f"gRPC client-stream upload {label} (chunk {format_size(chunk)})",
                    {"transport": "grpc", "direction": "upload"},
                    functools.partial(grpc_upload, size=size, chunk=chunk),
                ),
            ]
        cases += [
            Case(case_id, text, {**attrs, "size_bytes": size, "chunk_bytes": chunk}, fn)
            for case_id, text, attrs, fn in plan
        ]
    cases += _chunk_sweep_cases(cfg)
    return cases


def _chunk_sweep_cases(cfg: ClientConfig) -> list[Case]:
    """gRPC download at one size with several chunk (= message) sizes."""
    size = min(cfg.chunk_sweep_size, cfg.max_size)
    size = min(size, max(cfg.selected_sizes))
    label = format_size(size)
    out: list[Case] = []
    for chunk in cfg.chunk_sweep:
        if chunk == cfg.chunk and size in cfg.selected_sizes:
            continue  # already measured as grpc-down-<size>
        out.append(
            Case(
                f"grpc-down-{label}-chunk{format_size(chunk)}",
                f"gRPC download {label}, chunk {format_size(chunk)}",
                {
                    "transport": "grpc",
                    "direction": "download",
                    "size_bytes": size,
                    "chunk_bytes": chunk,
                    "sweep": True,
                },
                functools.partial(grpc_download, size=size, chunk=chunk),
            )
        )
    return out


def _probe_shot(endpoint: Endpoint, chunk: int, limit: int) -> dict[str, Any]:
    result: dict[str, Any] = {"limit_bytes": limit}
    try:
        grpc_download(endpoint, 2 * chunk, chunk, limit=limit)
    except TransferError as exc:
        return {
            **result,
            "ok": False,
            "failure_type": str(exc.failure_type),
            "error": str(exc),
        }
    return {**result, "ok": True, "failure_type": None, "error": None}


def grpc_default_limit_probe(endpoint: Endpoint) -> dict[str, Any]:
    """What gRPC's *default* 4 MB message limit does to 4 MiB messages.

    One shot each, not a measurement. gRPC's default
    ``max_receive_message_length`` is 4 MB = 4 MiB = 4194304 bytes and the check
    is inclusive, so ``at_default_limit`` is expected to **pass** (the largest
    chunk this spike sends is exactly 4 MiB); ``over_limit`` lowers the client's
    limit by one byte to record the exact error the fabric would hit when a
    tensor chunk is larger than the receiver's limit (expected: refused with
    ``transport_error`` / RESOURCE_EXHAUSTED). ``ok`` is the first shot's result;
    ``failure_type``/``error`` belong to it as well (``grpc_unavailable`` when
    either side has no gRPC).
    """
    chunk = 4 * MIB
    at_limit = _probe_shot(endpoint, chunk, GRPC_DEFAULT_LIMIT)
    over = (
        _probe_shot(endpoint, chunk, GRPC_DEFAULT_LIMIT - 1)
        if at_limit["failure_type"] != str(FailureType.GRPC_UNAVAILABLE)
        else {**at_limit, "limit_bytes": GRPC_DEFAULT_LIMIT - 1}
    )
    return {
        "chunk_bytes": chunk,
        "default_limit_bytes": GRPC_DEFAULT_LIMIT,
        "ok": at_limit["ok"],
        "failure_type": at_limit["failure_type"],
        "error": at_limit["error"],
        "at_default_limit": at_limit,
        "over_limit": over,
    }


def _progress(log: Log, case_id: str) -> Callable[[str, int, Sample | RepError], None]:
    def on_rep(phase: str, index: int, outcome: Sample | RepError) -> None:
        if isinstance(outcome, RepError):
            log(
                f"  {case_id} rep {index} ({phase}): FAILED {outcome.failure_type}: {outcome}"
            )
        else:
            log(
                f"  {case_id} rep {index} ({phase}): {outcome['recv_mbps']:.1f} Mbit/s "
                f"({outcome['recv_mib_s']:.1f} MiB/s), cpu "
                f"{outcome['cpu_ms_per_mib']:.2f} ms/MiB"
            )

    return on_rep


def _still_there(endpoint: Endpoint) -> TransferError | None:
    try:
        hello(endpoint)
    except TransferError as exc:
        return exc
    return None


def run_client(
    cfg: ClientConfig,
    *,
    log: Log = lambda _msg: None,
    ceiling: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ClientRun:
    """Measure ``cfg.server`` and return the (sanitised) envelope.

    Never raises for network failures: they are recorded as typed errors.
    """
    client_name = cfg.name or socket.gethostname()
    errors: list[dict[str, Any]] = []
    cases: list[CaseResult] = []
    server_name = cfg.server
    server_info: dict[str, Any] = {}
    probe: dict[str, Any] | None = None
    plan = build_cases(cfg)
    try:
        reply = hello(cfg.endpoint)
    except TransferError as exc:
        errors.append(
            {
                "failure_type": str(exc.failure_type),
                "message": str(exc),
                "phase": "hello",
            }
        )
        log(f"server {cfg.server}:{cfg.port} not usable: {exc.failure_type}: {exc}")
        reply = None
    if reply is not None:
        server_name = str(reply.get("name") or reply["hostname"])
        server_info = {
            k: reply.get(k)
            for k in ("hostname", "name", "os", "peer", "ports", "grpc_error")
        }
        endpoint = cfg.endpoint_from_hello(reply)
        log(
            f"server {cfg.server}:{cfg.port} is {server_name} ({reply.get('os')}), "
            f"ports tcp {endpoint.tcp_port}, grpc {endpoint.grpc_port}, "
            f"http {endpoint.http_port}"
        )
        if reply.get("grpc_error"):
            log(f"server has no gRPC: {reply['grpc_error']}")
        cases = _run_cases(cfg, plan, endpoint, errors, log)
        probe = grpc_default_limit_probe(endpoint)
        over = probe["over_limit"]
        log(
            "gRPC 4 MiB message at the default 4 MB limit: "
            + ("passed" if probe["ok"] else f"refused: {probe['error']}")
            + "; one byte over the limit: "
            + ("passed (unexpected)" if over["ok"] else f"refused: {over['error']}")
        )
    complete = (
        not errors
        and len(cases) == len(plan)
        and all(not c.errors and len(c.samples) == cfg.reps for c in cases)
    )
    params: dict[str, Any] = {
        "server": cfg.server,
        "port": cfg.port,
        "server_name": server_name,
        "client_name": client_name,
        "reps": cfg.reps,
        "warmup": cfg.warmup,
        "sizes_bytes": list(cfg.selected_sizes),
        "max_size_bytes": cfg.max_size,
        "chunk_bytes": cfg.chunk,
        "chunk_sweep_bytes": list(cfg.chunk_sweep),
        "chunk_sweep_size_bytes": min(cfg.chunk_sweep_size, cfg.max_size),
        "range_parts": cfg.range_parts,
        "uploads": cfg.uploads,
        "pause_s": cfg.pause_s,
        "connect_timeout_s": cfg.connect_timeout_s,
        "io_timeout_s": cfg.io_timeout_s,
        "transfer_timeout_s": cfg.transfer_timeout_s,
        "min_valid_reps": MIN_VALID_REPS,
    }
    extra: dict[str, Any] = {
        "server_hello": server_info,
        "loopback_ceiling": ceiling,
        "grpc_default_limit_probe": probe,
        "payload": {
            "block_bytes": payload.BLOCK_BYTES,
            "seed": payload.SEED,
            "verification": "crc32 over the received range, compared locally",
        },
        "complete": complete,
    }
    envelope = make_envelope(
        spike=SPIKE,
        topic=TOPIC,
        link=cfg.link,
        params=params,
        cases=cases,
        errors=errors,
        extra=extra,
        host_label=client_name,
        tool={
            "name": "lab/spikes/s3_transport.py",
            "protocol": PROTOCOL,
            "interpreter": interpreter(),
            "grpc": grpc_version(),
        },
        now=now,
    )
    return ClientRun(envelope, client_name, server_name, complete, errors)


def _run_cases(
    cfg: ClientConfig,
    plan: list[Case],
    endpoint: Endpoint,
    errors: list[dict[str, Any]],
    log: Log,
) -> list[CaseResult]:
    cases: list[CaseResult] = []
    for index, case in enumerate(plan):
        log(f"{case.case_id}: {case.label} ({cfg.warmup} warmup + {cfg.reps} reps)")
        result = run_reps(
            case.case_id,
            _guard(case.measure, endpoint),
            reps=cfg.reps,
            warmup=cfg.warmup,
            metrics=METRICS,
            label=case.label,
            attrs=case.attrs,
            pause_s=cfg.pause_s,
            on_rep=_progress(log, case.case_id),
        )
        cases.append(result)
        if not result.aborted:
            continue
        gone = _still_there(endpoint)
        if gone is None:
            log(f"{case.case_id} aborted after a fatal error; server still answers")
            continue
        skipped = [c.case_id for c in plan[index + 1 :]]
        errors.append(
            {
                "failure_type": str(FailureType.CONNECTION_LOST),
                "message": f"server lost during {case.case_id} ({gone.failure_type}: "
                f"{gone}); skipped cases: {', '.join(skipped) or 'none'}",
                "phase": case.case_id,
            }
        )
        log(f"server lost during {case.case_id}; skipping {len(skipped)} case(s)")
        break
    return cases
