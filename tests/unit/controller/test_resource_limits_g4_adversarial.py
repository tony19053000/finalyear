"""V0.3 G4 tester round: multi-GPU clamping beyond the implementer's single-GPU cases."""

from __future__ import annotations

import math

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.session.limits import check_resource_update

pytestmark = pytest.mark.unit


def test_every_out_of_range_gpu_is_clamped_not_only_the_first_flagged_one() -> None:
    """``check_resource_update`` records the ``gpus[].util_percent`` reason once
    (``break`` after the first bad entry, by design - one log line per report) but
    must still clamp *every* offending entry in the returned message, not only the
    one that triggered the reason string."""
    update = agent_pb2.ResourceUpdate(cpu_percent=1.0)
    update.gpus.add(index=0, util_percent=-5.0)  # under range
    update.gpus.add(index=1, util_percent=50.0)  # fine
    update.gpus.add(index=2, util_percent=float("nan"))  # NaN
    update.gpus.add(index=3, util_percent=250.0)  # over range

    result = check_resource_update(update)
    assert result.clamped is True
    percents = [g.util_percent for g in result.update.gpus]
    assert percents[0] == 0.0
    assert percents[1] == 50.0
    assert percents[2] == 0.0
    assert not math.isnan(percents[2])
    assert percents[3] == 100.0


def test_every_over_length_mount_is_clamped_not_only_the_first() -> None:
    update = agent_pb2.ResourceUpdate(cpu_percent=1.0)
    update.disks.add(mount="a" * 500, free_bytes=1, total_bytes=2)
    update.disks.add(mount="/ok", free_bytes=1, total_bytes=2)
    update.disks.add(mount="b" * 300, free_bytes=1, total_bytes=2)

    result = check_resource_update(update)
    assert result.clamped is True
    mounts = [d.mount for d in result.update.disks]
    assert len(mounts[0]) == 128
    assert mounts[1] == "/ok"
    assert len(mounts[2]) == 128
