"""One S2 client run against one peer -> a result envelope (no CLI coupling).

Order: ``/ping`` (identity + reachability) -> HTTP blob -> gRPC unary -> gRPC
server-stream -> one long-lived bidi session per (idle gap x keepalive mode).
Each case is ``warmup + reps`` repetitions through the shared harness, so a
number without >= 5 measured repetitions is reported INVALID.

A fatal failure (nothing listening, SYN dropped, no route) aborts the case at
once and, once the peer is confirmed gone, skips the rest: a blocked port then
costs one timeout, not one per repetition. The diagnosis and its fix hint are
recorded, because "blocked by X, do Y" is the point of this spike.
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
from spikes._s2 import client as probe
from spikes._s2.env import (
    endpoint_facts,
    firewall_profile,
    local_facts,
    wsl_status_here,
)
from spikes._s2.protocol import (
    KEEPALIVE_MODES,
    PROTOCOL,
    FailureType,
    ProbeError,
    keepalive_settings,
)

SPIKE: Final = "S2"
TOPIC: Final = "S2-crossos"
HTTP_PING_METRICS: Final = {
    "connect_ms": "ms",
    "ttfb_ms": "ms",
    "total_ms": "ms",
}
BLOB_METRICS: Final = {"connect_ms": "ms", "ttfb_ms": "ms", "mbps": "Mbit/s"}
UNARY_METRICS: Final = {"channel_ready_ms": "ms", "rtt_ms": "ms"}
STREAM_METRICS: Final = {"channel_ready_ms": "ms", "ttfb_ms": "ms", "mbps": "Mbit/s"}
SESSION_METRICS: Final = {
    "survived": "0/1",
    "alive_s": "s",
    "idle_survived_s": "s",
    "final_rtt_ms": "ms",
    "active_rtt_mean_ms": "ms",
    "msgs_up": "count",
    "msgs_down": "count",
}

type Log = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class ClientConfig:
    server: str
    http_port: int
    grpc_port: int
    link: str
    reps: int = 5
    warmup: int = 1
    blob_bytes: int = 32 * 1024 * 1024
    hold_s: float = 5.0
    ping_interval_s: float = 1.0
    idle_gaps_s: tuple[float, ...] = (30.0, 120.0)
    keepalives: tuple[str, ...] = KEEPALIVE_MODES
    connect_timeout_s: float = 5.0
    io_timeout_s: float = 20.0
    pause_s: float = 0.5
    name: str | None = None
    no_inbound_rule: bool = False
    note: str = ""

    @property
    def endpoint(self) -> probe.Endpoint:
        return probe.Endpoint(
            self.server,
            self.http_port,
            self.grpc_port,
            self.connect_timeout_s,
            self.io_timeout_s,
        )

    def session_case_id(self, idle_s: float, keepalive: str) -> str:
        return f"grpc-bidi-idle{idle_s:g}s-ka{keepalive}"

    def estimated_seconds(self) -> float:
        """Rough wall-clock estimate, so the operator knows what they started."""
        rounds = self.reps + self.warmup
        sessions = sum(
            (self.hold_s + gap + 3.0) * rounds
            for gap in self.idle_gaps_s
            for _ in self.keepalives
        )
        return sessions + 4 * rounds * (2.0 + self.pause_s)


@dataclass(slots=True)
class ClientRun:
    envelope: dict[str, Any]
    client_name: str
    server_name: str
    #: every planned case produced ``reps`` samples and no error was recorded
    complete: bool
    errors: list[dict[str, Any]] = field(default_factory=list)


def _as_rep_error(exc: ProbeError) -> RepError:
    return RepError(str(exc.failure_type), f"{exc} [fix: {exc.fix}]", fatal=exc.fatal)


def _guard(call: Callable[[], Sample]) -> Sample:
    try:
        return call()
    except ProbeError as exc:
        raise _as_rep_error(exc) from exc


def planned_cases(cfg: ClientConfig) -> list[str]:
    ids = ["http-ping", "http-blob", "grpc-unary", "grpc-stream"]
    ids += [
        cfg.session_case_id(gap, ka) for gap in cfg.idle_gaps_s for ka in cfg.keepalives
    ]
    return ids


def _plan(
    cfg: ClientConfig, client_name: str, server_name: str
) -> list[tuple[str, str, Callable[[int], Sample], dict[str, str], dict[str, Any]]]:
    ep = cfg.endpoint
    pair = f"{client_name} → {server_name}"
    plan: list[
        tuple[str, str, Callable[[int], Sample], dict[str, str], dict[str, Any]]
    ] = [
        (
            "http-ping",
            f"HTTP GET /ping {pair}",
            lambda _i: _guard(lambda: probe.http_ping(ep)),
            HTTP_PING_METRICS,
            {"kind": "http", "op": "ping"},
        ),
        (
            "http-blob",
            f"HTTP GET /blob {cfg.blob_bytes / 1e6:.0f} MB {pair}",
            lambda _i: _guard(lambda: probe.http_blob(ep, cfg.blob_bytes)),
            BLOB_METRICS,
            {"kind": "http", "op": "blob", "bytes": cfg.blob_bytes},
        ),
        (
            "grpc-unary",
            f"gRPC Echo (unary) {pair}",
            lambda i: _guard(lambda: probe.grpc_unary(ep, "on", seq=i)),
            UNARY_METRICS,
            {"kind": "grpc", "op": "unary", "keepalive": "on"},
        ),
        (
            "grpc-stream",
            f"gRPC Download (server-streaming) {cfg.blob_bytes / 1e6:.0f} MB {pair}",
            lambda _i: _guard(lambda: probe.grpc_download(ep, cfg.blob_bytes, "on")),
            STREAM_METRICS,
            {
                "kind": "grpc",
                "op": "server_stream",
                "bytes": cfg.blob_bytes,
                "keepalive": "on",
            },
        ),
    ]
    plan.extend(
        (
            cfg.session_case_id(gap, keepalive),
            f"gRPC Session (bidi, {cfg.hold_s:g} s active + {gap:g} s idle, "
            f"keepalive {keepalive}) {pair}",
            _session_measure(cfg, gap, keepalive),
            SESSION_METRICS,
            {
                "kind": "grpc",
                "op": "bidi_session",
                "idle_s": gap,
                "keepalive": keepalive,
                "keepalive_settings": keepalive_settings(keepalive),
            },
        )
        for gap in cfg.idle_gaps_s
        for keepalive in cfg.keepalives
    )
    return plan


def _session_measure(
    cfg: ClientConfig, idle_s: float, keepalive: str
) -> Callable[[int], Sample]:
    def measure(_index: int) -> Sample:
        return _guard(
            lambda: probe.grpc_session(
                cfg.endpoint,
                keepalive=keepalive,
                hold_s=cfg.hold_s,
                ping_interval_s=cfg.ping_interval_s,
                idle_s=idle_s,
            )
        )

    return measure


def _progress(log: Log, case_id: str) -> Callable[[str, int, Sample | RepError], None]:
    def on_rep(phase: str, index: int, outcome: Sample | RepError) -> None:
        if isinstance(outcome, RepError):
            log(
                f"  {case_id} rep {index} ({phase}): FAILED {outcome.failure_type}: {outcome}"
            )
            return
        if "survived" in outcome:
            verdict = "alive" if outcome["survived"] else "DROPPED"
            log(
                f"  {case_id} rep {index} ({phase}): {verdict} after "
                f"{outcome['alive_s']:.1f} s, {outcome['msgs_up']} up / "
                f"{outcome['msgs_down']} down"
            )
        elif "mbps" in outcome:
            log(
                f"  {case_id} rep {index} ({phase}): {outcome['mbps']:.1f} Mbit/s, "
                f"first byte {outcome['ttfb_ms']:.1f} ms"
            )
        else:
            key = "rtt_ms" if "rtt_ms" in outcome else "total_ms"
            log(f"  {case_id} rep {index} ({phase}): {outcome[key]:.2f} ms")

    return on_rep


def _still_there(cfg: ClientConfig) -> ProbeError | None:
    try:
        probe.http_ping(cfg.endpoint)
    except ProbeError as exc:
        return exc
    return None


def run_client(
    cfg: ClientConfig,
    *,
    log: Log = lambda _msg: None,
    now: datetime | None = None,
) -> ClientRun:
    """Measure ``cfg.server`` and return the (sanitised) envelope.

    Never raises for network failures: they are recorded as typed errors with
    their fix hint.
    """
    client_name = cfg.name or socket.gethostname()
    errors: list[dict[str, Any]] = []
    cases: list[CaseResult] = []
    server_name = cfg.server
    server_identity: dict[str, Any] = {}
    try:
        identity = probe.http_ping(cfg.endpoint)["peer_identity"]
    except ProbeError as exc:
        errors.append({**exc.to_json(), "phase": "hello"})
        log(
            f"peer {cfg.server} not usable: {exc.failure_type}: {exc}\n  fix: {exc.fix}"
        )
        identity = None
    if identity is not None:
        server_name = str(
            identity.get("name") or identity.get("hostname") or cfg.server
        )
        server_identity = identity
        log(
            f"peer {cfg.server} is {server_name} "
            f"({identity.get('system')} {identity.get('release')}, "
            f"python {identity.get('python')}, "
            f"wsl={bool((identity.get('wsl') or {}).get('is_wsl'))})"
        )
        cases = _run_cases(cfg, client_name, server_name, errors, log)
    complete = (
        not errors
        and len(cases) == len(planned_cases(cfg))
        and all(not c.errors and len(c.samples) == cfg.reps for c in cases)
    )
    envelope = make_envelope(
        spike=SPIKE,
        topic=TOPIC,
        link=cfg.link,
        params=_params(cfg, client_name, server_name),
        cases=cases,
        errors=errors,
        extra={
            "peer_identity": server_identity,
            "local": local_facts(),
            "wsl": wsl_status_here(),
            "firewall": firewall_profile(),
            "endpoint": endpoint_facts(cfg.server, cfg.grpc_port),
            "outbound_only": {
                "no_inbound_rule_declared": cfg.no_inbound_rule,
                "client_listeners_opened": 0,
                "explanation": (
                    "the client dials out only (ADR-0002); it binds no listening "
                    "socket, so no inbound firewall rule is needed on this machine"
                ),
            },
            "operator_note": cfg.note,
            "complete": complete,
        },
        host_label=client_name,
        tool={
            "name": "lab/spikes/s2_crossos.py",
            "protocol": PROTOCOL,
            "interpreter": interpreter(),
        },
        now=now,
    )
    return ClientRun(envelope, client_name, server_name, complete, errors)


def _params(cfg: ClientConfig, client_name: str, server_name: str) -> dict[str, Any]:
    return {
        "server": cfg.server,
        "http_port": cfg.http_port,
        "grpc_port": cfg.grpc_port,
        "client_name": client_name,
        "server_name": server_name,
        "reps": cfg.reps,
        "warmup": cfg.warmup,
        "blob_bytes": cfg.blob_bytes,
        "hold_s": cfg.hold_s,
        "ping_interval_s": cfg.ping_interval_s,
        "idle_gaps_s": list(cfg.idle_gaps_s),
        "keepalives": list(cfg.keepalives),
        "keepalive_settings": {k: keepalive_settings(k) for k in cfg.keepalives},
        "connect_timeout_s": cfg.connect_timeout_s,
        "io_timeout_s": cfg.io_timeout_s,
        "pause_s": cfg.pause_s,
        "min_valid_reps": MIN_VALID_REPS,
    }


def _run_cases(
    cfg: ClientConfig,
    client_name: str,
    server_name: str,
    errors: list[dict[str, Any]],
    log: Log,
) -> list[CaseResult]:
    plan = _plan(cfg, client_name, server_name)
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
            pause_s=cfg.pause_s,
            on_rep=_progress(log, case_id),
        )
        cases.append(result)
        if not result.aborted:
            continue
        gone = _still_there(cfg)
        if gone is None:
            log(
                f"{case_id} aborted after a fatal error; peer still answers, continuing"
            )
            continue
        skipped = [p[0] for p in plan[index + 1 :]]
        errors.append(
            {
                "failure_type": str(FailureType.STREAM_DROPPED),
                "message": f"peer lost during {case_id} ({gone.failure_type}: {gone}); "
                f"skipped cases: {', '.join(skipped) or 'none'}",
                "fix": gone.fix,
                "phase": case_id,
            }
        )
        log(f"peer lost during {case_id}; skipping {', '.join(skipped) or 'nothing'}")
        break
    return cases
