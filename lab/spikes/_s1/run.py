"""One S1 client run against one server -> a result envelope (no CLI coupling).

Order: ``hello`` (identity + reachability) -> UDP RTT/jitter/loss on the idle
link -> TCP bulk for every stream count, upload then download -> optional
iperf3 cross-check. Each case is ``warmup + reps`` repetitions via the shared
harness. A fatal failure (server vanished, timeout) aborts that case at once;
the server is then re-checked with ``hello`` and, if it is gone, every later
case is skipped and recorded, so a dead peer costs a bounded time, not
``reps x cases`` timeouts.

Host identity in the matrix is ``name`` (default: the hostname). Server and
client each report their own, so a run fills the cells ``client -> server``
(upload) and ``server -> client`` (download).
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from spikes._harness import MIN_VALID_REPS
from spikes._harness.envelope import interpreter, make_envelope
from spikes._harness.reps import CaseResult, RepError, Sample, run_reps
from spikes._s1 import iperf
from spikes._s1.client import (
    DIRECTIONS,
    Direction,
    Endpoint,
    hello,
    local_address_towards,
    socket_buffer_defaults,
    tcp_throughput,
    udp_rtt,
)
from spikes._s1.protocol import PROTOCOL, FailureType, MeasureError

SPIKE: Final = "S1"
TOPIC: Final = "S1-network"
TCP_METRICS: Final = {"receiver_mbps": "Mbit/s", "sender_mbps": "Mbit/s"}
UDP_METRICS: Final = {
    "rtt_mean_ms": "ms",
    "rtt_stdev_ms": "ms",
    "rtt_p95_ms": "ms",
    "jitter_ms": "ms",
    "loss_pct": "%",
}

type Log = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class ClientConfig:
    server: str
    port: int
    link: str
    reps: int = 5
    warmup: int = 1
    seconds: float = 10.0
    streams: tuple[int, ...] = (1, 4)
    pings: int = 200
    ping_interval_s: float = 0.02
    ping_size: int = 64
    ping_timeout_s: float = 1.0
    pause_s: float = 1.0
    connect_timeout_s: float = 5.0
    io_timeout_s: float = 10.0
    name: str | None = None
    iperf3: bool = False
    iperf3_port: int = iperf.DEFAULT_IPERF3_PORT

    @property
    def endpoint(self) -> Endpoint:
        return Endpoint(
            self.server, self.port, self.connect_timeout_s, self.io_timeout_s
        )


@dataclass(slots=True)
class ClientRun:
    envelope: dict[str, Any]
    client_name: str
    server_name: str
    #: every case produced ``reps`` samples and no error was recorded
    complete: bool
    errors: list[dict[str, Any]] = field(default_factory=list)


def _as_rep_error(exc: MeasureError) -> RepError:
    return RepError(str(exc.failure_type), str(exc), fatal=exc.fatal)


def _tcp_measure(
    cfg: ClientConfig, direction: Direction, streams: int
) -> Callable[[int], Sample]:
    def measure(_index: int) -> Sample:
        try:
            return tcp_throughput(
                cfg.endpoint, direction=direction, streams=streams, seconds=cfg.seconds
            )
        except MeasureError as exc:
            raise _as_rep_error(exc) from exc

    return measure


def _udp_measure(cfg: ClientConfig) -> Callable[[int], Sample]:
    def measure(_index: int) -> Sample:
        try:
            return udp_rtt(
                cfg.endpoint,
                count=cfg.pings,
                interval_s=cfg.ping_interval_s,
                size=cfg.ping_size,
                reply_timeout_s=cfg.ping_timeout_s,
            )
        except MeasureError as exc:
            raise _as_rep_error(exc) from exc

    return measure


def _progress(log: Log, case_id: str) -> Callable[[str, int, Sample | RepError], None]:
    def on_rep(phase: str, index: int, outcome: Sample | RepError) -> None:
        if isinstance(outcome, RepError):
            log(
                f"  {case_id} rep {index} ({phase}): FAILED {outcome.failure_type}: {outcome}"
            )
        elif "receiver_mbps" in outcome:
            log(
                f"  {case_id} rep {index} ({phase}): {outcome['receiver_mbps']:.1f} Mbit/s"
            )
        else:
            log(
                f"  {case_id} rep {index} ({phase}): RTT {outcome['rtt_mean_ms']:.3f} ms, "
                f"jitter {outcome['jitter_ms']:.3f} ms, loss {outcome['loss_pct']:.1f}%"
            )

    return on_rep


def _still_there(cfg: ClientConfig) -> MeasureError | None:
    try:
        hello(cfg.endpoint)
    except MeasureError as exc:
        return exc
    return None


def planned_cases(cfg: ClientConfig) -> list[str]:
    return ["udp-rtt"] + [f"tcp-{d}-x{n}" for n in cfg.streams for d in DIRECTIONS]


def run_client(
    cfg: ClientConfig,
    *,
    log: Log = lambda _msg: None,
    ceiling: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ClientRun:
    """Measure ``cfg.server`` and return the (sanitised) envelope. Never raises
    for network failures; they are recorded as typed errors."""
    client_name = cfg.name or socket.gethostname()
    errors: list[dict[str, Any]] = []
    cases: list[CaseResult] = []
    server_name = cfg.server
    server_info: dict[str, Any] = {}
    try:
        reply = hello(cfg.endpoint)
    except MeasureError as exc:
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
        server_info = {k: reply.get(k) for k in ("hostname", "name", "os", "peer")}
        log(f"server {cfg.server}:{cfg.port} is {server_name} ({reply.get('os')})")
        cases = _run_cases(cfg, client_name, server_name, errors, log)
    iperf_runs: list[dict[str, Any]] = []
    if cfg.iperf3 and reply is not None:
        for reverse in (False, True):
            log(f"iperf3 cross-check ({'download' if reverse else 'upload'}) ...")
            iperf_runs.append(
                iperf.run_iperf3(
                    cfg.server, cfg.iperf3_port, seconds=cfg.seconds, reverse=reverse
                )
            )
    complete = (
        not errors
        and len(cases) == len(planned_cases(cfg))
        and all(not c.errors and len(c.samples) == cfg.reps for c in cases)
    )
    params: dict[str, Any] = {
        "server": cfg.server,
        "port": cfg.port,
        "server_name": server_name,
        "client_name": client_name,
        "local_address": local_address_towards(cfg.endpoint),
        "reps": cfg.reps,
        "warmup": cfg.warmup,
        "seconds": cfg.seconds,
        "streams": list(cfg.streams),
        "pings": cfg.pings,
        "ping_interval_s": cfg.ping_interval_s,
        "ping_size_bytes": cfg.ping_size,
        "ping_timeout_s": cfg.ping_timeout_s,
        "pause_s": cfg.pause_s,
        "connect_timeout_s": cfg.connect_timeout_s,
        "io_timeout_s": cfg.io_timeout_s,
        "min_valid_reps": MIN_VALID_REPS,
    }
    extra: dict[str, Any] = {
        "server_hello": server_info,
        "buffers": socket_buffer_defaults(),
        "loopback_ceiling": ceiling,
        "iperf3": iperf_runs,
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
            "name": "lab/spikes/s1_network.py",
            "protocol": PROTOCOL,
            "interpreter": interpreter(),
        },
        now=now,
    )
    return ClientRun(envelope, client_name, server_name, complete, errors)


def _run_cases(
    cfg: ClientConfig,
    client_name: str,
    server_name: str,
    errors: list[dict[str, Any]],
    log: Log,
) -> list[CaseResult]:
    plan: list[
        tuple[str, str, Callable[[int], Sample], dict[str, str], dict[str, Any]]
    ] = [
        (
            "udp-rtt",
            f"UDP echo {client_name} ↔ {server_name}",
            _udp_measure(cfg),
            UDP_METRICS,
            {"kind": "udp", "client": client_name, "server": server_name},
        )
    ]
    for streams in cfg.streams:
        for direction in DIRECTIONS:
            sender, receiver = (
                (client_name, server_name)
                if direction == "upload"
                else (server_name, client_name)
            )
            plan.append(
                (
                    f"tcp-{direction}-x{streams}",
                    f"TCP x{streams} {sender} → {receiver}",
                    _tcp_measure(cfg, direction, streams),
                    TCP_METRICS,
                    {
                        "kind": "tcp",
                        "direction": direction,
                        "streams": streams,
                        "sender": sender,
                        "receiver": receiver,
                        "client": client_name,
                        "server": server_name,
                    },
                )
            )
    cases: list[CaseResult] = []
    for index, (case_id, label, measure, metrics, attrs) in enumerate(plan):
        log(f"{case_id}: {label} ({cfg.warmup} warmup + {cfg.reps} reps)")
        result = run_reps(
            case_id,
            measure,
            reps=cfg.reps,
            warmup=cfg.warmup,
            metrics=metrics,
            label=label,
            attrs=attrs,
            pause_s=cfg.pause_s if attrs["kind"] == "tcp" else 0.0,
            on_rep=_progress(log, case_id),
        )
        cases.append(result)
        if not result.aborted:
            continue
        gone = _still_there(cfg)
        if gone is None:
            log(
                f"{case_id} aborted after a fatal error; server still answers, continuing"
            )
            continue
        skipped = [p[0] for p in plan[index + 1 :]]
        errors.append(
            {
                "failure_type": str(FailureType.CONNECTION_LOST),
                "message": f"server lost during {case_id} ({gone.failure_type}: {gone}); "
                f"skipped cases: {', '.join(skipped) or 'none'}",
                "phase": case_id,
            }
        )
        log(f"server lost during {case_id}; skipping {', '.join(skipped) or 'nothing'}")
        break
    return cases
