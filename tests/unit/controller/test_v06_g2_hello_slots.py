"""V0.6 G2 (D105): the controller reads Hello's slot fields; a value above 64 is
ignored (None = the planner's defaults), never an admission failure."""

from __future__ import annotations

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.session.admission import MAX_HELLO_SLOTS, slots_of

pytestmark = pytest.mark.unit


def test_absent_fields_are_none() -> None:
    assert slots_of(agent_pb2.Hello()) == (None, None, None)


def test_bounds_max_accepted_and_max_plus_one_ignored() -> None:
    assert MAX_HELLO_SLOTS == 64
    ok = agent_pb2.Hello(max_cpu_jobs=64, max_gpu_jobs=0)
    assert slots_of(ok) == (64, 0, None)
    over = agent_pb2.Hello(max_cpu_jobs=65, max_gpu_jobs=2**32 - 1)
    assert slots_of(over) == (None, None, "max_cpu_jobs,max_gpu_jobs")
    one = agent_pb2.Hello(max_cpu_jobs=2, max_gpu_jobs=65)
    assert slots_of(one) == (2, None, "max_gpu_jobs")
