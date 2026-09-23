"""V0.3 G5 unit tests for :mod:`meshforge.controller.api.shape` (roadmap 0.3.12).

Pure shaping: fabricated records in, response models out. No FastAPI, no database,
no clock of the module's own - ``now`` is injected, which is the whole point of
``sample_age_s`` being comparable with the session manager's monotonic clock (D16).

Nothing here asserts anything about *this* machine.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from mf_api_records import (
    T0,
    live_session,
    node_record,
    sample,
    session_record,
    snapshot_record,
)

from meshforge.controller.api.models import NodeDetail, NodeSummary
from meshforge.controller.api.shape import (
    node_detail,
    node_summary,
    resource_sample,
    resource_snapshot,
    sample_age_s,
    session_summary,
)
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.resources import DiskSample, GpuSample

pytestmark = pytest.mark.unit

# ------------------------------------------------------------- sample_age_s


def test_sample_age_is_none_without_a_sample() -> None:
    assert sample_age_s(None, now=1_000.0) is None


def test_sample_age_is_measured_on_the_injected_monotonic_clock() -> None:
    assert sample_age_s(sample(received_at=100.0), now=102.25) == 2.25


def test_sample_age_is_clamped_at_zero_for_a_sample_from_the_future() -> None:
    # `now` is read before the sample lands; a negative age must never be served.
    assert sample_age_s(sample(received_at=100.001), now=100.0) == 0.0


def test_sample_age_is_rounded_to_milliseconds() -> None:
    assert sample_age_s(sample(received_at=0.0), now=1.0004999) == 1.0


# ------------------------------------------------------------- node_summary


def test_node_summary_of_a_node_with_no_sample_no_session_and_no_gpus() -> None:
    """Every optional field is null/empty and nothing raises (the cold-start row)."""
    record = node_record(state=NodeState.OFFLINE, hardware={})
    summary = node_summary(record, live=None, sample=None, now=500.0)

    assert isinstance(summary, NodeSummary)
    assert summary.state is NodeState.OFFLINE
    assert summary.connected is False
    assert summary.sample_age_s is None
    assert (summary.rtt_ms, summary.rtt_ewma_ms) == (None, None)
    assert (summary.cpu_percent, summary.ram_used_bytes, summary.ram_total_bytes) == (
        None,
        None,
        None,
    )
    assert summary.gpus == ()
    dumped = summary.model_dump(mode="json")
    assert dumped["sample_age_s"] is None
    assert dumped["gpus"] == []


def test_node_summary_carries_live_session_and_newest_sample() -> None:
    record = node_record()
    summary = node_summary(
        record,
        live=live_session(session_id="node-session", last_rtt_ms=3.0, rtt_ewma_ms=3.5),
        sample=sample(
            received_at=90.0,
            gpus=(
                GpuSample(
                    index=0, util_percent=50.0, vram_used_bytes=1, vram_total_bytes=2
                ),
                GpuSample(
                    index=1, util_percent=60.0, vram_used_bytes=3, vram_total_bytes=4
                ),
            ),
        ),
        now=91.5,
    )

    assert summary.connected is True
    assert summary.sample_age_s == 1.5
    assert (summary.rtt_ms, summary.rtt_ewma_ms) == (3.0, 3.5)
    assert summary.cpu_percent == 12.5
    assert [gpu.index for gpu in summary.gpus] == [0, 1]


def test_state_connected_and_sample_age_disagree_independently() -> None:
    """D16: a node the database calls ONLINE with no live stream and no sample.

    This is exactly the controller-restart case, and it must not collapse into one
    "online" boolean.
    """
    summary = node_summary(
        node_record(state=NodeState.ONLINE), live=None, sample=None, now=1.0
    )
    assert (summary.state, summary.connected, summary.sample_age_s) == (
        NodeState.ONLINE,
        False,
        None,
    )


def test_summary_json_field_order_is_the_declaration_order() -> None:
    summary = node_summary(node_record(), live=None, sample=None, now=1.0)
    assert list(summary.model_dump(mode="json")) == [
        "node_id",
        "hostname",
        "os",
        "platform",
        "state",
        "connected",
        "last_seen_at",
        "sample_age_s",
        "rtt_ms",
        "rtt_ewma_ms",
        "cpu_percent",
        "ram_used_bytes",
        "ram_total_bytes",
        "gpus",
    ]


def test_timestamps_serialise_as_utc_with_an_explicit_offset() -> None:
    naive = dt.datetime(2026, 1, 2, 3, 4, 5)  # noqa: DTZ001 - a naive column value
    summary = node_summary(
        node_record(last_seen_at=naive), live=None, sample=None, now=1.0
    )
    assert (
        summary.model_dump(mode="json")["last_seen_at"] == "2026-01-02T03:04:05+00:00"
    )


def test_a_non_utc_timestamp_is_converted_not_relabelled() -> None:
    plus_two = dt.datetime(
        2026, 1, 2, 5, 4, 5, tzinfo=dt.timezone(dt.timedelta(hours=2))
    )
    summary = node_summary(
        node_record(last_seen_at=plus_two), live=None, sample=None, now=1.0
    )
    assert (
        summary.model_dump(mode="json")["last_seen_at"] == "2026-01-02T03:04:05+00:00"
    )


# -------------------------------------------------------------- node_detail


def test_node_detail_without_any_session_leaves_the_session_fields_null() -> None:
    detail = node_detail(
        node_record(hardware={}), live=None, sample=None, session=None, now=1.0
    )
    assert isinstance(detail, NodeDetail)
    assert (detail.agent_version, detail.protocol_version, detail.peer_address) == (
        None,
        None,
        None,
    )
    assert detail.last_session_id is None
    assert detail.hardware == {}
    assert detail.session_epoch == 3


def test_node_detail_prefers_the_live_peer_address_over_the_stored_one() -> None:
    record = node_record(last_session_id="sess")
    detail = node_detail(
        record,
        live=live_session(session_id="sess", peer_address="10.0.0.9:51000"),
        sample=None,
        session=session_record(record.node_id, peer_address="10.0.0.8:1"),
        now=1.0,
    )
    assert detail.peer_address == "10.0.0.9:51000"


def test_node_detail_falls_back_to_the_stored_peer_address_when_disconnected() -> None:
    record = node_record(last_session_id="sess", state=NodeState.OFFLINE)
    detail = node_detail(
        record,
        live=None,
        sample=None,
        session=session_record(record.node_id, peer_address="10.0.0.8:1"),
        now=1.0,
    )
    assert detail.connected is False
    assert detail.peer_address == "10.0.0.8:1"
    assert detail.agent_version == "0.1.0.dev0"


def test_node_detail_copies_the_hardware_mapping_rather_than_aliasing_it() -> None:
    stored: dict[str, Any] = {"cpu_threads": 8}
    detail = node_detail(
        node_record(hardware=stored), live=None, sample=None, session=None, now=1.0
    )
    stored["cpu_threads"] = 999
    assert detail.hardware == {"cpu_threads": 8}


# ------------------------------------------------------------ resource rows


def test_resource_snapshot_shaping_keeps_the_jsonb_payloads() -> None:
    record = snapshot_record(
        "node_x", gpu={"0": {"util_mean": 1.0}}, disks={"/": {"free_min": 5}}
    )
    shaped = resource_snapshot(record)
    assert shaped.samples == 30
    assert shaped.gpu == {"0": {"util_mean": 1.0}}
    assert shaped.disks == {"/": {"free_min": 5}}


def test_resource_sample_shaping_includes_age_gpus_and_disks() -> None:
    shaped = resource_sample(
        sample(
            received_at=10.0,
            gpus=(
                GpuSample(
                    index=0, util_percent=5.0, vram_used_bytes=1, vram_total_bytes=2
                ),
            ),
            disks=(DiskSample(mount="/", free_bytes=7, total_bytes=9),),
        ),
        now=12.5,
    )
    assert shaped.age_s == 2.5
    assert shaped.sequence == 11
    assert [gpu.index for gpu in shaped.gpus] == [0]
    assert [disk.mount for disk in shaped.disks] == ["/"]


def test_resource_sample_age_is_zero_not_negative_for_a_future_sample() -> None:
    assert resource_sample(sample(received_at=5.0), now=4.0).age_s == 0.0


# --------------------------------------------------------- session_summary


def test_session_summary_is_connected_only_for_the_session_the_manager_holds() -> None:
    record = session_record("node_x")
    held = session_summary(record, live=live_session(session_id=record.session_id))
    other = session_summary(record, live=live_session(session_id="a-different-session"))
    closed = session_summary(record, live=None)
    assert (held.connected, other.connected, closed.connected) == (True, False, False)


def test_session_summary_serialises_the_end_reason_value_or_null() -> None:
    record = session_record("node_x")
    open_row = session_summary(record, live=None)
    ended = session_summary(
        session_record(
            "node_x",
            ended_at=T0 + dt.timedelta(seconds=5),
            end_reason=SessionEndReason.SUPERSEDED,
        ),
        live=None,
    )
    assert open_row.end_reason is None
    assert open_row.ended_at is None
    assert ended.end_reason == SessionEndReason.SUPERSEDED.value
    assert isinstance(ended.model_dump(mode="json")["end_reason"], str)
