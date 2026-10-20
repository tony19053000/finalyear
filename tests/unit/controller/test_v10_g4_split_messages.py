"""V0.10 G4 (D263): a lane's chunk messages are bounded and refused, never
cleaned, before anything is looked up."""

from __future__ import annotations

import pytest

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.common.proto_codec import failure_type_to_proto
from meshforge.controller.split.messages import (
    MAX_HELD_LEASES,
    parse_chunk_request,
    parse_chunk_result,
    parse_lease_renew,
)

pytestmark = pytest.mark.unit

JOB, ATT = str(new_job_id()), str(new_attempt_id())
DIGEST = "sha256:" + "a" * 64


def _result(**kw: object) -> agent_pb2.ChunkResult:
    base: dict[str, object] = {
        "job_id": JOB,
        "attempt_id": ATT,
        "fencing_token": 3,
        "chunk_index": 1,
        "item_id": 9,
        "lease_seq": 2,
        "outcome": agent_pb2.ChunkResult.OUTCOME_DONE,
        "result_digest": DIGEST,
    }
    base.update(kw)
    return agent_pb2.ChunkResult(**base)  # type: ignore[arg-type]


def test_a_good_request_and_renew_parse() -> None:
    req = parse_chunk_request(
        agent_pb2.ChunkRequest(
            job_id=JOB,
            attempt_id=ATT,
            fencing_token=3,
            want=2,
            held=[agent_pb2.ChunkLeaseRef(item_id=5, lease_seq=1)],
        )
    )
    assert not isinstance(req, str)
    assert req.want == 2 and req.held == ((5, 1),)
    renew = parse_lease_renew(
        agent_pb2.LeaseRenew(
            job_id=JOB,
            attempt_id=ATT,
            fencing_token=3,
            leases=[agent_pb2.ChunkLeaseRef(item_id=5, lease_seq=1)],
        )
    )
    assert not isinstance(renew, str) and renew.leases == ((5, 1),)


@pytest.mark.parametrize(
    "msg",
    [
        agent_pb2.ChunkRequest(job_id="x", attempt_id=ATT, fencing_token=1, want=1),
        agent_pb2.ChunkRequest(job_id=JOB, attempt_id=ATT, fencing_token=0, want=1),
        agent_pb2.ChunkRequest(job_id=JOB, attempt_id=ATT, fencing_token=1, want=3),
        agent_pb2.ChunkRequest(
            job_id=JOB,
            attempt_id=ATT,
            fencing_token=1,
            want=1,
            held=[agent_pb2.ChunkLeaseRef(item_id=0, lease_seq=1)],
        ),
        agent_pb2.ChunkRequest(
            job_id=JOB,
            attempt_id=ATT,
            fencing_token=1,
            want=1,
            held=[
                agent_pb2.ChunkLeaseRef(item_id=1, lease_seq=1)
                for _ in range(MAX_HELD_LEASES + 1)
            ],
        ),
    ],
)
def test_bad_requests_are_refused(msg: agent_pb2.ChunkRequest) -> None:
    assert isinstance(parse_chunk_request(msg), str)


def test_a_done_result_parses() -> None:
    got = parse_chunk_result(_result())
    assert not isinstance(got, str)
    assert got.outcome == "done" and got.result_digest == DIGEST


def test_a_failed_result_carries_its_failure() -> None:
    msg = _result(outcome=agent_pb2.ChunkResult.OUTCOME_FAILED, result_digest="")
    msg.failure.CopyFrom(
        common_pb2.Failure(
            failure_type=failure_type_to_proto(FailureType.USER_ERROR),
            message="exit 1",
            retryable=True,
        )
    )
    got = parse_chunk_result(msg)
    assert not isinstance(got, str)
    assert got.failure == (FailureType.USER_ERROR, "exit 1", True)


@pytest.mark.parametrize(
    "kw",
    [
        {"outcome": agent_pb2.ChunkResult.OUTCOME_UNSPECIFIED},
        {"result_digest": "sha256:xyz"},
        {"outcome": agent_pb2.ChunkResult.OUTCOME_FAILED, "result_digest": ""},
        {"outcome": agent_pb2.ChunkResult.OUTCOME_RELEASED},  # a digest with it
        {"item_id": 0},
        {"lease_seq": 0},
    ],
)
def test_bad_results_are_refused(kw: dict[str, object]) -> None:
    assert isinstance(parse_chunk_result(_result(**kw)), str)


def test_a_failure_message_with_control_characters_is_refused() -> None:
    msg = _result(outcome=agent_pb2.ChunkResult.OUTCOME_FAILED, result_digest="")
    msg.failure.CopyFrom(
        common_pb2.Failure(
            failure_type=failure_type_to_proto(FailureType.USER_ERROR),
            message="bad\x1b[2J",
        )
    )
    assert isinstance(parse_chunk_result(msg), str)
