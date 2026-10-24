"""Pure shaping of registry/memory records into the ``/v1`` response models.

No FastAPI, no I/O, no clock of its own: every function takes the records it needs
and ``now`` (the controller's **monotonic** clock, the same one the session manager
and the live ring use). That is what makes ``sample_age_s`` correct across a
controller restart and unit-testable with an injected clock.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any, TypedDict

from meshforge.common.capabilities import HARDWARE_SCORES_CAPABILITY
from meshforge.common.clock_offset import ClockOffset, round_up
from meshforge.controller.api.models import (
    CacheCount,
    DiskUsage,
    GpuUsage,
    NodeCertificate,
    NodeClock,
    NodeDetail,
    NodeScores,
    NodeSummary,
    ResourceSample,
    ResourceSnapshot,
    ScoreConditions,
    ScoreMetric,
    SessionSummary,
)
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner.inputs import bound_to
from meshforge.controller.registry import NodeRecord, SessionRecord, SnapshotRecord
from meshforge.controller.resources import LiveSample
from meshforge.controller.scores import UNITS
from meshforge.controller.session.clocks import uncertain_reasons
from meshforge.controller.session.manager import LiveSession

__all__ = [
    "node_detail",
    "node_scores",
    "node_summary",
    "resource_sample",
    "resource_snapshot",
    "sample_age_s",
    "session_summary",
]


def sample_age_s(sample: LiveSample | None, now: float) -> float | None:
    """Seconds since ``sample`` arrived, on the monotonic clock. ``None`` = no sample.

    Clamped at zero: a sample recorded a microsecond after ``now`` was read would
    otherwise be served as a negative age.
    """
    if sample is None:
        return None
    return round(max(now - sample.received_at, 0.0), 3)


def _gpus(sample: LiveSample | None) -> tuple[GpuUsage, ...]:
    if sample is None:
        return ()
    return tuple(
        GpuUsage(
            index=gpu.index,
            util_percent=gpu.util_percent,
            vram_used_bytes=gpu.vram_used_bytes,
            vram_total_bytes=gpu.vram_total_bytes,
        )
        for gpu in sample.gpus
    )


def _scores_capable(hardware: Mapping[str, Any]) -> bool:
    capabilities = hardware.get("capabilities")
    return isinstance(capabilities, list) and HARDWARE_SCORES_CAPABILITY in capabilities


def node_scores(node: NodeRecord) -> NodeScores:
    """``scores`` for one node (D38): measured / pending / none, never inferred.

    The stored document was validated at ingestion (``session/limits.py``) and
    shaped by ``controller/scores.py``; ``unit`` is added here from the metric name.
    """
    document = node.hardware_scores
    if document is None or not bound_to(document, node.node_id):  # D145b
        if _scores_capable(node.hardware):
            return NodeScores(status="pending")
        return NodeScores(status="none")
    return NodeScores(
        status="measured",
        run_id=document["run_id"],
        measured_at=dt.datetime.fromisoformat(document["measured_at"]),
        score_version=document["score_version"],
        conditions=ScoreConditions(**_known(ScoreConditions, document["conditions"])),
        metrics=tuple(
            ScoreMetric(**_known(ScoreMetric, metric), unit=UNITS.get(metric["name"]))
            for metric in document["metrics"]
        ),
    )


def _known(
    model: type[ScoreMetric | ScoreConditions], values: Mapping[str, Any]
) -> dict[str, Any]:
    """Only the fields ``model`` declares: a document written by a *newer*
    controller (after a downgrade) must not turn ``/v1/nodes`` into a 500."""
    return {k: v for k, v in values.items() if k in model.model_fields and k != "unit"}


class _SummaryFields(TypedDict):
    """Exactly the fields :class:`NodeSummary` declares (and NodeDetail inherits)."""

    node_id: str
    hostname: str
    display_name: str
    os: str
    platform: str
    state: NodeState
    connected: bool
    last_seen_at: dt.datetime
    sample_age_s: float | None
    rtt_ms: float | None
    rtt_ewma_ms: float | None
    cpu_percent: float | None
    ram_used_bytes: int | None
    ram_total_bytes: int | None
    gpus: tuple[GpuUsage, ...]
    scores: NodeScores
    certificate: NodeCertificate | None
    not_enforced: tuple[str, ...] | None


def _certificate(node: NodeRecord) -> NodeCertificate | None:
    if (
        node.cert_serial is None
        or node.cert_fingerprint is None
        or node.cert_not_after is None
        or node.public_key_fingerprint is None
    ):
        return None
    return NodeCertificate(
        serial=node.cert_serial,
        fingerprint_sha256=node.cert_fingerprint,
        not_after=node.cert_not_after,
        public_key_fingerprint_sha256=node.public_key_fingerprint,
    )


def _summary_fields(
    node: NodeRecord,
    *,
    live: LiveSession | None,
    sample: LiveSample | None,
    now: float,
) -> _SummaryFields:
    return {
        "node_id": node.node_id,
        "hostname": node.hostname,
        "display_name": node.display_name,
        "os": node.os,
        "platform": node.platform,
        "state": node.state,
        # D16: three separate notions - persisted state, a live stream right now,
        # and how stale the newest sample is. Never one "online" boolean.
        "connected": live is not None,
        "last_seen_at": node.last_seen_at,
        "sample_age_s": sample_age_s(sample, now),
        "rtt_ms": live.last_rtt_ms if live is not None else None,
        "rtt_ewma_ms": live.rtt_ewma_ms if live is not None else None,
        "cpu_percent": sample.cpu_percent if sample is not None else None,
        "ram_used_bytes": sample.ram_used_bytes if sample is not None else None,
        "ram_total_bytes": sample.ram_total_bytes if sample is not None else None,
        "gpus": _gpus(sample),
        "scores": node_scores(node),
        "certificate": _certificate(node),
        "not_enforced": (
            tuple(live.policy.not_enforced)
            if live is not None and live.policy is not None
            else None
        ),
    }


def node_summary(
    node: NodeRecord,
    *,
    live: LiveSession | None,
    sample: LiveSample | None,
    now: float,
) -> NodeSummary:
    return NodeSummary(**_summary_fields(node, live=live, sample=sample, now=now))


def node_detail(
    node: NodeRecord,
    *,
    live: LiveSession | None,
    sample: LiveSample | None,
    session: SessionRecord | None,
    now: float,
    cache: Mapping[str, tuple[int, int]] | None = None,
) -> NodeDetail:
    """``session`` is the node's most recent session row (``last_session_id``);
    ``cache`` is ``kind -> (count, bytes)`` from the location index."""
    return NodeDetail(
        **_summary_fields(node, live=live, sample=sample, now=now),
        first_seen_at=node.first_seen_at,
        session_epoch=node.session_epoch,
        last_session_id=node.last_session_id,
        agent_version=session.agent_version if session is not None else None,
        protocol_version=session.protocol_version if session is not None else None,
        peer_address=(
            live.peer_address
            if live is not None
            else (session.peer_address if session is not None else None)
        ),
        peer_endpoint=(live.peer_endpoint or None) if live is not None else None,
        cache={
            kind: CacheCount(count=count, bytes=size)
            for kind, (count, size) in sorted((cache or {}).items())
        },
        hardware=dict(node.hardware),
        owner_policy=owner_policy_view(live),
        clock=node_clock(live, session),
    )


def node_clock(
    live: LiveSession | None, session: SessionRecord | None
) -> NodeClock | None:
    """V0.9 G1 (D213): the live session's estimate, else the stored one."""
    if live is not None and live.clock.latest is not None:
        latest = live.clock.latest
        return NodeClock(
            clock_offset_ms=round(latest.offset_ms, 3),
            bound_ms=round_up(latest.bound_ms, 3),
            consistent=latest.consistent,
            exchanges=latest.exchanges,
            uncertain=bool(uncertain_reasons(latest)),
            live=True,
        )
    if (
        session is None
        or session.clock_offset_ms is None
        or session.clock_offset_bound_ms is None
    ):
        return None
    stored = ClockOffset(
        offset_ms=session.clock_offset_ms,
        bound_ms=session.clock_offset_bound_ms,
        rtt_ms=0.0,
        exchanges=session.clock_exchanges or 0,
        consistent=session.clock_consistent is not False,
    )
    return NodeClock(
        clock_offset_ms=round(stored.offset_ms, 3),
        bound_ms=round_up(stored.bound_ms, 3),
        consistent=stored.consistent,
        exchanges=stored.exchanges,
        uncertain=bool(uncertain_reasons(stored)),
        live=False,
    )


def owner_policy_view(live: LiveSession | None) -> dict[str, Any] | None:
    """The live owner policy for ``GET /v1/nodes/{id}`` (user names counted,
    not listed: another user's allow-list is not every user's business)."""
    if live is None or live.policy is None:
        return None
    view = live.policy.as_dict()
    users = view.pop("allowed_users")
    view["allowed_users_count"] = len(users) if isinstance(users, list) else 0
    view["refused"] = live.policy_refused
    return view


def resource_snapshot(record: SnapshotRecord) -> ResourceSnapshot:
    return ResourceSnapshot(
        bucket_start=record.bucket_start,
        samples=record.samples,
        cpu_percent_mean=record.cpu_percent_mean,
        cpu_percent_max=record.cpu_percent_max,
        ram_used_bytes_mean=record.ram_used_bytes_mean,
        ram_used_bytes_max=record.ram_used_bytes_max,
        ram_total_bytes=record.ram_total_bytes,
        gpu=dict(record.gpu),
        disks=dict(record.disks),
    )


def resource_sample(sample: LiveSample, now: float) -> ResourceSample:
    age = sample_age_s(sample, now)
    return ResourceSample(
        sequence=sample.sequence,
        session_epoch=sample.session_epoch,
        observed_at=sample.observed_at,
        age_s=age if age is not None else 0.0,
        cpu_percent=sample.cpu_percent,
        ram_used_bytes=sample.ram_used_bytes,
        ram_total_bytes=sample.ram_total_bytes,
        gpus=_gpus(sample),
        disks=tuple(
            DiskUsage(
                mount=disk.mount,
                free_bytes=disk.free_bytes,
                total_bytes=disk.total_bytes,
            )
            for disk in sample.disks
        ),
    )


def session_summary(
    record: SessionRecord, *, live: LiveSession | None
) -> SessionSummary:
    return SessionSummary(
        session_id=record.session_id,
        node_id=record.node_id,
        session_epoch=record.session_epoch,
        started_at=record.started_at,
        ended_at=record.ended_at,
        end_reason=record.end_reason.value if record.end_reason else None,
        peer_address=record.peer_address,
        agent_version=record.agent_version,
        protocol_version=record.protocol_version,
        last_rtt_ms=record.last_rtt_ms,
        rtt_ewma_ms=record.rtt_ewma_ms,
        connected=live is not None and live.session_id == record.session_id,
    )
