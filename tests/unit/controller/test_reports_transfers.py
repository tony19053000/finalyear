"""V0.5 G5 D93: transfer telemetry in AttemptStatus is bounded and validated."""

from __future__ import annotations

import math

import pytest

from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.controller.jobs.reports import (
    MAX_TRANSFER_SOURCES,
    MAX_TRANSFERS,
    parse_status,
)

pytestmark = [pytest.mark.unit]

TREE = "sha256:" + "b" * 64
PEER = str(new_node_id())


def _status(
    *transfers: agent_pb2.TransferSummary, done: bool = True
) -> agent_pb2.AttemptStatus:
    status = agent_pb2.AttemptStatus(
        job_id=str(new_job_id()),
        attempt_id=str(new_attempt_id()),
        fencing_token=1,
        state=common_pb2.ATTEMPT_STATE_DONE
        if done
        else common_pb2.ATTEMPT_STATE_RUNNING,
        transfers=list(transfers),
    )
    return status


def _summary(**kw: object) -> agent_pb2.TransferSummary:
    sources = kw.pop("sources", None)
    kw.setdefault("duration_s", 1.5)
    summary = agent_pb2.TransferSummary(tree_id=TREE, **kw)  # type: ignore[arg-type]
    if sources is None:
        sources = [
            agent_pb2.TransferSource(node_id="", bytes=100, duration_s=0.25, units=2),
            agent_pb2.TransferSource(
                node_id=PEER,
                bytes=50,
                duration_s=0.1,
                units=1,
                dropped="integrity_error",
            ),
        ]
    summary.sources.extend(sources)  # type: ignore[arg-type]
    return summary


def test_rows_per_source_plus_the_cache_row() -> None:
    report = parse_status(
        _status(
            _summary(blobs_hit=3, bytes_hit=30, chunks_resumed=2, corrupt_refetches=1)
        )
    )
    assert not isinstance(report, str)
    rows = {r.source: r for r in report.transfers}
    assert rows["cache"].units == 3 and rows["cache"].bytes == 30
    assert rows["cache"].resumed_chunks == 2 and rows["cache"].failures == 1
    assert rows["cache"].duration_ms == 1500
    assert rows["controller"].bytes == 100 and rows["controller"].duration_ms == 250
    assert rows[PEER].dropped == "integrity_error"


def test_the_same_tree_twice_is_merged_not_duplicated() -> None:
    report = parse_status(_status(_summary(blobs_hit=1), _summary(blobs_hit=2)))
    assert not isinstance(report, str)
    rows = {r.source: r for r in report.transfers}
    assert len(report.transfers) == 3
    assert rows["cache"].units == 3 and rows["controller"].bytes == 200


def test_bounds_max_accepted_and_max_plus_one_refused() -> None:
    assert not isinstance(parse_status(_status(*[_summary()] * MAX_TRANSFERS)), str)
    assert isinstance(parse_status(_status(*[_summary()] * (MAX_TRANSFERS + 1))), str)
    many = [
        agent_pb2.TransferSource(node_id=str(new_node_id()), bytes=1, duration_s=0.0)
        for _ in range(MAX_TRANSFER_SOURCES + 1)
    ]
    assert not isinstance(
        parse_status(_status(_summary(sources=many[:MAX_TRANSFER_SOURCES]))), str
    )
    assert isinstance(parse_status(_status(_summary(sources=many))), str)


@pytest.mark.parametrize(
    "bad",
    [
        {"tree_id": "sha256:xyz"},
        {"duration_s": -1.0},
        {"duration_s": math.nan},
        {"duration_s": math.inf},
        {"blobs_hit": 2**31},
        {"sources": [agent_pb2.TransferSource(node_id="not-a-node", duration_s=0.0)]},
        {"sources": [agent_pb2.TransferSource(duration_s=0.0, dropped="Bad Reason")]},
        {"sources": [agent_pb2.TransferSource(duration_s=-0.5)]},
        {"sources": [agent_pb2.TransferSource(duration_s=0.0, units=2**31)]},
    ],
)
def test_malformed_telemetry_refuses_the_report(bad: dict[str, object]) -> None:
    summary = _summary(**{k: v for k, v in bad.items() if k != "tree_id"})
    if "tree_id" in bad:
        summary.tree_id = str(bad["tree_id"])
    assert isinstance(parse_status(_status(summary)), str)


def test_telemetry_only_on_a_terminal_report() -> None:
    assert isinstance(parse_status(_status(_summary(), done=False)), str)
    report = parse_status(_status())
    assert not isinstance(report, str) and report.transfers == ()
