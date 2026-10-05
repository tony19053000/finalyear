"""V0.6 G2 (D105): the agent advertises its owner's slot limits in Hello."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from meshforge.agent.session.hello import build_hello
from meshforge.agent.settings import ExecutionSettings
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit


def test_hello_slot_fields_are_numbers_11_and_12() -> None:
    fields = agent_pb2.Hello.DESCRIPTOR.fields_by_name
    assert fields["max_cpu_jobs"].number == 11
    assert fields["max_gpu_jobs"].number == 12
    assert fields["max_cpu_jobs"].has_presence and fields["max_gpu_jobs"].has_presence


def _hello(slots: tuple[int, int] | None) -> agent_pb2.Hello:
    return build_hello(
        node_id="node_x",
        session_epoch=1,
        agent_version="0",
        hardware=agent_pb2.HardwareSummary(),
        facts=("h", "linux", "linux-x86_64"),
        slots=slots,
    )


def test_build_hello_sets_or_omits_the_slots() -> None:
    absent = _hello(None)
    assert not absent.HasField("max_cpu_jobs") and not absent.HasField("max_gpu_jobs")
    zero = _hello((0, 0))
    assert zero.HasField("max_cpu_jobs") and zero.max_cpu_jobs == 0
    assert zero.HasField("max_gpu_jobs") and zero.max_gpu_jobs == 0
    some = agent_pb2.Hello.FromString(_hello((3, 1)).SerializeToString())
    assert (some.max_cpu_jobs, some.max_gpu_jobs) == (3, 1)


def _settings(**kw: Any) -> ExecutionSettings:
    return ExecutionSettings.model_validate(kw)


def test_defaults_one_cpu_slot_and_one_gpu_slot_per_gpu() -> None:
    cfg = _settings()
    assert cfg.max_cpu_jobs == 1 and cfg.max_gpu_jobs is None
    assert cfg.slots(gpu_count=0) == (1, 0)
    assert cfg.slots(gpu_count=2) == (1, 2)
    # trimmed so the total never exceeds max_concurrent_attempts (4)
    assert cfg.slots(gpu_count=8) == (1, 3)
    assert _settings(max_gpu_jobs=0).slots(gpu_count=2) == (1, 0)
    assert _settings(max_cpu_jobs=0, max_gpu_jobs=2).slots(gpu_count=1) == (0, 2)


@pytest.mark.parametrize("field", ["max_cpu_jobs", "max_gpu_jobs"])
def test_slot_bounds(field: str) -> None:
    room = {"max_concurrent_attempts": 64, "max_cpu_jobs": 0}
    assert getattr(_settings(**{**room, field: 64}), field) == 64
    assert getattr(_settings(**{field: 0}), field) == 0
    for bad in (-1, 65):
        with pytest.raises(ValidationError):
            _settings(**{**room, field: bad})


def test_slots_must_fit_max_concurrent_attempts() -> None:
    assert _settings(max_cpu_jobs=2, max_gpu_jobs=2, max_concurrent_attempts=4)
    with pytest.raises(ValidationError, match="max_concurrent_attempts"):
        _settings(max_cpu_jobs=3, max_gpu_jobs=2, max_concurrent_attempts=4)
    with pytest.raises(ValidationError, match="max_concurrent_attempts"):
        _settings(max_cpu_jobs=5)
