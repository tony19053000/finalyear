"""V0.5 G2 (D86): the controller accepts the agent's ``venv`` runtime name."""

from __future__ import annotations

import pytest

from meshforge.common.ids import new_attempt_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.controller.jobs.reports import RUNTIMES, parse_status
from meshforge.controller.store.models.job import RUNTIME_VALUES

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("runtime", ["docker", "venv", "subprocess"])
def test_each_agent_runtime_name_parses(runtime: str) -> None:
    status = agent_pb2.AttemptStatus(
        job_id=str(new_job_id()),
        attempt_id=str(new_attempt_id()),
        fencing_token=1,
        state=common_pb2.ATTEMPT_STATE_STAGING,
        runtime=runtime,
    )
    report = parse_status(status)
    assert not isinstance(report, str), report
    assert report.runtime == runtime


def test_report_vocabulary_matches_the_database_check() -> None:
    # reports.RUNTIMES gates what is written; RUNTIME_VALUES is the DB CHECK
    assert set(RUNTIME_VALUES) == RUNTIMES == {"docker", "venv", "subprocess"}
