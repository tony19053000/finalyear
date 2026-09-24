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
from meshforge.controller.api.models import (
    DiskUsage,
    GpuUsage,
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
from meshforge.controller.registry import NodeRecord, SessionRecord, SnapshotRecord
from meshforge.controller.resources import LiveSample
from meshforge.controller.scores import UNITS
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
    if document is None:
        if _scores_capable(node.hardware):
            return NodeScores(status="pending")
        return NodeScores(status="none")
    return NodeScores(
        status="measured",
        run_id=document["run_id"],
        measured_at=dt.datetime.fromisoformat(document["measured_at"]),
        score_version=document["score_version"],
        conditions=ScoreConditions(**document["conditions"]),
        metrics=tuple(
            ScoreMetric(**metric, unit=UNITS.get(metric["name"]))
            for metric in document["metrics"]
        ),
    )


class _SummaryFields(TypedDict):
    """Exactly the fields :class:`NodeSummary` declares (and NodeDetail inherits)."""

    node_id: str
    hostname: str
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
) -> NodeDetail:
    """``session`` is the node's most recent session row (``last_session_id``)."""
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
        hardware=dict(node.hardware),
    )


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
