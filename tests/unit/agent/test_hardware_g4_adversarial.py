"""V0.3 G4 tester round: adversarial probes at D9/D10/D12 boundaries.

Nothing here asserts anything about *this* machine (no hostname, no real GPU, no
real driver, no real IP) - every seam is a fake, per the standing cross-platform
rule (these must pass on a CPU-only Windows laptop after a plain ``uv sync``).

Focus of this file: D10's claim that "hardware discovery never raises and never
blocks the loop" is checked here specifically for the case the brief calls out -
"hang a probe past its timeout" - for a source (NVML) that is an in-process call,
not a subprocess ``run_command`` can kill. See
``test_a_wedged_nvml_call_blows_through_the_discovery_budget`` for the finding.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from meshforge.agent.hardware import gpu as gpu_mod
from meshforge.agent.hardware.probe import Budget, ProbeStatus

pytestmark = pytest.mark.unit


class SlowNvml:
    """An NVML stand-in whose ``nvmlInit`` blocks like a wedged driver call.

    Real NVML entry points are synchronous FFI calls into the driver: there is no
    way to cancel one from Python once it has started (no thread, no signal-safe
    interrupt), unlike ``run_command`` which owns a real OS process it can kill.
    """

    def __init__(self, *, delay_s: float) -> None:
        self.delay_s = delay_s

    def nvmlInit(self) -> None:  # noqa: N802 - NVML's own spelling
        time.sleep(self.delay_s)

    def nvmlShutdown(self) -> None:  # noqa: N802
        pass

    def nvmlDeviceGetCount(self) -> int:  # noqa: N802
        return 1

    def nvmlDeviceGetHandleByIndex(self, index: int) -> int:  # noqa: N802
        return index

    def nvmlSystemGetDriverVersion(self) -> bytes:  # noqa: N802
        return b"1.0"


def test_a_wedged_nvml_call_blows_through_the_discovery_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D10: "hardware discovery never raises and never blocks the loop" is checked
    against the budget only *between* probes, not *during* one. ``Budget.expired``
    is tested before ``NvmlSession.open()`` is called, but ``nvmlInit`` itself is a
    plain blocking call with no timeout, no thread and no subprocess around it - so
    a driver that hangs inside ``nvmlInit`` (a real Secure-Boot / wedged-driver
    failure mode the module's own docstring names) blocks the whole discovery pass
    for as long as the driver call takes, however much longer than the configured
    ``agent.hardware.timeout_s`` that is.

    This test uses a 1 s fake hang against a 0.5 s budget - finite so the suite
    stays fast - and shows the probe returns *after* the hang, roughly 2x over
    budget, not at the budget boundary. On a real machine with an unbounded hang
    (not a 1 s sleep) this would hang the agent's startup forever: ``runtime.serve``
    awaits ``discover_hardware`` -> ``asyncio.to_thread(provider.get)`` with no
    ``asyncio.wait_for`` around it, so the node would never send its first Hello.
    """
    monkeypatch.setattr(gpu_mod, "pynvml_module", lambda: SlowNvml(delay_s=1.0))
    monkeypatch.setattr(gpu_mod, "which", lambda name: None)  # no nvidia-smi fallback

    budget = Budget(0.5)
    started = time.monotonic()
    outcome = gpu_mod.probe_gpus(budget)
    elapsed = time.monotonic() - started

    # DEFECT: a correctly budget-respecting probe would return within ~0.5 s (the
    # configured discovery budget) with status TIMEOUT. It does not: it blocks for
    # the full fake hang and comes back UNAVAILABLE, having spent 1.0 s against a
    # 0.5 s budget. This assertion documents the *actual* (buggy) behaviour so the
    # test fails loudly if the implementer's fix changes the timing without the
    # tester noticing - flip this once D10 is actually enforced for NVML calls.
    assert elapsed >= 0.9, (
        "expected the NVML hang to blow through the budget (current defect); "
        f"got elapsed={elapsed:.3f}s, outcome={outcome.status}. If this now fails "
        "because elapsed is small, the defect may be fixed - update this test."
    )


class MidEnumerationSlowNvml:
    """NVML that opens fine but hangs inside the per-device usage/enumeration call."""

    def __init__(self, *, delay_s: float) -> None:
        self.delay_s = delay_s

    def nvmlInit(self) -> None:  # noqa: N802
        pass

    def nvmlShutdown(self) -> None:  # noqa: N802
        pass

    def nvmlDeviceGetCount(self) -> int:  # noqa: N802
        return 2

    def nvmlDeviceGetHandleByIndex(self, index: int) -> int:  # noqa: N802
        return index

    def nvmlSystemGetDriverVersion(self) -> bytes:  # noqa: N802
        return b"1.0"

    def nvmlDeviceGetMemoryInfo(self, handle: int) -> Any:  # noqa: N802
        time.sleep(self.delay_s)
        raise RuntimeError("should not reach here in the budget-respecting version")

    def nvmlDeviceGetName(self, handle: int) -> bytes:  # noqa: N802
        return b"Fake GPU"

    def nvmlDeviceGetCudaComputeCapability(self, handle: int) -> tuple[int, int]:  # noqa: N802
        return (0, 0)


def test_a_wedged_device_query_also_blows_through_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same finding as above, at the per-device enumeration step: ``devices()``
    loops over every handle with no budget check between iterations, so a driver
    that answers ``nvmlInit``/``nvmlDeviceGetCount`` but hangs on the first device's
    memory query still blocks past the discovery budget."""
    monkeypatch.setattr(
        gpu_mod, "pynvml_module", lambda: MidEnumerationSlowNvml(delay_s=1.0)
    )
    monkeypatch.setattr(gpu_mod, "which", lambda name: None)

    budget = Budget(0.5)
    started = time.monotonic()
    outcome = gpu_mod.probe_gpus(budget)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.9, (
        "expected the mid-enumeration NVML hang to blow through the budget "
        f"(current defect); got elapsed={elapsed:.3f}s, outcome={outcome.status}"
    )
    # Not asserting the exact status: the point is the *timing* violation.
    assert outcome.status in (ProbeStatus.ERROR, ProbeStatus.UNAVAILABLE)
