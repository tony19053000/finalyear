"""V0.6 G1 / D114: bounds of ``[agent.execution] upload_retry_s`` and
``upload_part_bytes`` (max accepted, max + 1 refused; min likewise)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meshforge.agent.settings import ExecutionSettings

pytestmark = pytest.mark.unit

KIB = 1024
MIB = 1024 * KIB


def test_defaults() -> None:
    cfg = ExecutionSettings()
    assert cfg.upload_retry_s == 120.0
    assert cfg.upload_part_bytes == 8 * MIB


@pytest.mark.parametrize("value", [0.0, 3600.0])
def test_upload_retry_s_bounds_accepted(value: float) -> None:
    assert ExecutionSettings(upload_retry_s=value).upload_retry_s == value


@pytest.mark.parametrize("value", [-0.001, 3600.001])
def test_upload_retry_s_bounds_refused(value: float) -> None:
    with pytest.raises(ValidationError):
        ExecutionSettings(upload_retry_s=value)


@pytest.mark.parametrize("value", [64 * KIB, 64 * MIB])
def test_upload_part_bytes_bounds_accepted(value: int) -> None:
    assert ExecutionSettings(upload_part_bytes=value).upload_part_bytes == value


@pytest.mark.parametrize("value", [64 * KIB - 1, 64 * MIB + 1])
def test_upload_part_bytes_bounds_refused(value: int) -> None:
    with pytest.raises(ValidationError):
        ExecutionSettings(upload_part_bytes=value)
