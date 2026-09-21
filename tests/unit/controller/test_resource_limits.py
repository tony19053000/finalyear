"""V0.3 G4 (brief §5d D12): the controller's bounds on ``Hello.hardware`` and reports.

Two policies, and the tests keep them apart: an over-size ``HardwareSummary`` is a
*reason* (the caller turns it into INVALID_ARGUMENT and persists nothing), an
over-size ``ResourceUpdate`` is *clamped* and the caller counts it.

The last test is the parity check the caps depend on: the agent clips to its own
copy of these numbers (it never imports the controller), so if the two drift the
agent starts sending Hellos its peer refuses.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from meshforge.agent.hardware import limits as agent_limits
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.session.limits import (
    CAPABILITY_MAX_LENGTH,
    CPU_MODEL_MAX_LENGTH,
    GPU_COMPUTE_CAPABILITY_MAX_LENGTH,
    GPU_DRIVER_MAX_LENGTH,
    GPU_MODEL_MAX_LENGTH,
    HARDWARE_SUMMARY_MAX_BYTES,
    MAX_CAPABILITIES,
    MAX_DISKS,
    MAX_GPUS,
    MOUNT_MAX_LENGTH,
    check_hardware,
    check_resource_update,
)

pytestmark = pytest.mark.unit


def maximal_hardware() -> agent_pb2.HardwareSummary:
    """Exactly what an agent clipping to its own caps may send."""
    summary = agent_pb2.HardwareSummary(
        cpu_model="c" * CPU_MODEL_MAX_LENGTH,
        cpu_cores=64,
        cpu_threads=128,
        ram_bytes=2**40,
        capabilities=[
            f"{'t' * (CAPABILITY_MAX_LENGTH - 3)}{i:03d}"
            for i in range(MAX_CAPABILITIES)
        ],
    )
    for index in range(MAX_GPUS):
        summary.gpus.append(
            agent_pb2.HardwareSummary.Gpu(
                index=index,
                model="m" * GPU_MODEL_MAX_LENGTH,
                vram_bytes=2**36,
                driver_version="d" * GPU_DRIVER_MAX_LENGTH,
                compute_capability="8" * GPU_COMPUTE_CAPABILITY_MAX_LENGTH,
            )
        )
    return summary


def test_the_agents_maximal_output_is_accepted() -> None:
    summary = maximal_hardware()
    assert check_hardware(summary) is None
    assert summary.ByteSize() <= HARDWARE_SUMMARY_MAX_BYTES


def test_an_empty_hardware_summary_is_accepted() -> None:
    assert check_hardware(agent_pb2.HardwareSummary()) is None


def test_one_cap_at_a_time_is_rejected_and_names_the_field() -> None:
    over = maximal_hardware()
    over.cpu_model += "x"
    reason = check_hardware(over)
    assert reason is not None and "cpu_model" in reason and "129" in reason
    assert "x" * 5 not in reason  # never echoes the value

    over = maximal_hardware()
    over.gpus.add(index=99, model="one too many")
    reason = check_hardware(over)
    assert reason is not None and "gpus has 17" in reason

    over = maximal_hardware()
    over.gpus[0].model += "x"
    reason = check_hardware(over)
    assert reason is not None and "gpus[0].model" in reason

    over = maximal_hardware()
    over.gpus[3].driver_version += "x"
    reason = check_hardware(over)
    assert reason is not None and "gpus[3].driver_version" in reason

    over = maximal_hardware()
    over.gpus[1].compute_capability += "x"
    reason = check_hardware(over)
    assert reason is not None and "compute_capability" in reason

    over = maximal_hardware()
    over.capabilities.append("one too many")
    reason = check_hardware(over)
    assert reason is not None and "capabilities has 33 tags" in reason

    over = maximal_hardware()
    over.capabilities[0] = "t" * (CAPABILITY_MAX_LENGTH + 1)
    reason = check_hardware(over)
    assert reason is not None and "capabilities[0]" in reason


def test_the_byte_backstop_catches_a_message_that_is_huge_for_another_reason() -> None:
    """Every named cap is respected but the message is 20 KB of unknown field.

    Protobuf keeps unknown fields on the message, so ``ByteSize()`` sees them -
    which is exactly what the backstop is for: a peer growing the message through
    something this module does not know about.
    """
    summary = maximal_hardware()
    # field 100, wire type 2 (length-delimited), 20 000 bytes of payload.
    padding = b"\xa2\x06" + b"\xa0\x9c\x01" + b"y" * 20_000
    grown = agent_pb2.HardwareSummary()
    grown.MergeFromString(summary.SerializeToString() + padding)

    assert grown.cpu_model == summary.cpu_model  # the known fields survived
    assert grown.ByteSize() > HARDWARE_SUMMARY_MAX_BYTES
    reason = check_hardware(grown)
    assert reason is not None
    assert "Hello.hardware is" in reason and str(HARDWARE_SUMMARY_MAX_BYTES) in reason
    assert "y" * 5 not in reason


# ------------------------------------------------------------------- reports


def test_a_well_formed_report_is_returned_unchanged() -> None:
    update = agent_pb2.ResourceUpdate(cpu_percent=42.5, ram_used_bytes=1, sequence=7)
    update.disks.add(mount="/", free_bytes=1, total_bytes=2)
    result = check_resource_update(update)
    assert result.clamped is False
    assert result.update is update
    assert result.dropped == ()


@pytest.mark.parametrize("value", [-1.0, 100.1, float("inf"), float("nan")])
def test_an_out_of_range_cpu_percent_is_clamped_not_rejected(value: float) -> None:
    result = check_resource_update(agent_pb2.ResourceUpdate(cpu_percent=value))
    assert result.clamped is True
    assert "cpu_percent" in result.dropped[0]
    assert 0.0 <= result.update.cpu_percent <= 100.0
    assert not math.isnan(result.update.cpu_percent)


def test_gpu_and_disk_counts_are_truncated_and_reported() -> None:
    update = agent_pb2.ResourceUpdate(cpu_percent=1.0, sequence=3)
    for index in range(MAX_GPUS + 4):
        update.gpus.add(index=index, util_percent=1.0)
    for index in range(MAX_DISKS + 4):
        update.disks.add(mount=f"/m{index}", free_bytes=1, total_bytes=2)
    result = check_resource_update(update)
    assert result.clamped is True
    assert len(result.update.gpus) == MAX_GPUS
    assert len(result.update.disks) == MAX_DISKS
    assert result.update.sequence == 3
    assert any("gpus" in reason for reason in result.dropped)
    assert any("disks" in reason for reason in result.dropped)


def test_a_gpu_percentage_and_a_long_mount_are_clamped() -> None:
    update = agent_pb2.ResourceUpdate(cpu_percent=1.0)
    update.gpus.add(index=0, util_percent=900.0, vram_used_bytes=5, vram_total_bytes=9)
    update.disks.add(mount="m" * 500, free_bytes=1, total_bytes=2)
    result = check_resource_update(update)
    assert result.update.gpus[0].util_percent == 100.0
    assert result.update.gpus[0].vram_used_bytes == 5
    assert len(result.update.disks[0].mount) == MOUNT_MAX_LENGTH
    assert len(result.dropped) == 2


def test_the_observation_time_survives_a_clamp() -> None:
    update = agent_pb2.ResourceUpdate(cpu_percent=500.0)
    update.observed_at.FromSeconds(1_700_000_000)
    result = check_resource_update(update)
    assert result.update.observed_at.seconds == 1_700_000_000


# -------------------------------------------------------------------- parity


def test_the_agent_and_controller_caps_are_the_same_numbers() -> None:
    assert agent_limits.CPU_MODEL_MAX_LENGTH == CPU_MODEL_MAX_LENGTH
    assert agent_limits.GPU_MODEL_MAX_LENGTH == GPU_MODEL_MAX_LENGTH
    assert agent_limits.GPU_DRIVER_MAX_LENGTH == GPU_DRIVER_MAX_LENGTH
    assert (
        agent_limits.GPU_COMPUTE_CAPABILITY_MAX_LENGTH
        == GPU_COMPUTE_CAPABILITY_MAX_LENGTH
    )
    assert agent_limits.MAX_GPUS == MAX_GPUS
    assert agent_limits.MAX_CAPABILITIES == MAX_CAPABILITIES
    assert agent_limits.CAPABILITY_MAX_LENGTH == CAPABILITY_MAX_LENGTH
    assert agent_limits.MAX_DISKS == MAX_DISKS
    assert agent_limits.MOUNT_MAX_LENGTH == MOUNT_MAX_LENGTH
    assert agent_limits.HARDWARE_SUMMARY_MAX_BYTES == HARDWARE_SUMMARY_MAX_BYTES


def test_whatever_the_agent_clips_passes_the_controllers_check() -> None:
    hostile = agent_pb2.HardwareSummary(
        cpu_model="c" * 5000,
        capabilities=[f"tag-{i}" * 40 for i in range(200)],
    )
    for index in range(200):
        hostile.gpus.add(
            index=index,
            model="m" * 5000,
            driver_version="d" * 5000,
            compute_capability="x" * 500,
        )
    assert check_hardware(hostile) is not None
    assert check_hardware(agent_limits.clip_hardware(hostile)) is None


def test_the_caps_have_exactly_one_definition_inside_the_controller() -> None:
    """G4 follow-up 5: ``session.limits`` and ``resources`` read the same objects.

    ``controller/caps.py`` exists so the downsampler can import the caps at module
    level (the deferred import it used to need hid a real cycle). If a future edit
    re-declares either number in one of the two modules, this fails.
    """
    from meshforge.controller import caps, resources
    from meshforge.controller.session import limits as session_limits

    assert session_limits.MAX_GPUS is caps.MAX_GPUS
    assert session_limits.MAX_DISKS is caps.MAX_DISKS
    downsampler = resources.Downsampler()
    assert downsampler.max_gpu_keys == caps.MAX_GPUS
    assert downsampler.max_disk_keys == caps.MAX_DISKS
    source = Path(session_limits.__file__).read_text(encoding="utf-8") + Path(
        resources.__file__
    ).read_text(encoding="utf-8")
    assert "MAX_GPUS: Final" not in source
    assert "MAX_DISKS: Final" not in source
