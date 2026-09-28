"""V0.4 G3 (D51/D45): RunSpec validation bounds, failure classification, the
observer's phase timings on an injected clock, and the exec capability rules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.execution import (
    EXEC_CAPABILITY,
    EXEC_DOCKER_CAPABILITY,
    EXEC_GPU_CAPABILITY,
    EXEC_SUBPROCESS_CAPABILITY,
    GpuNeed,
    Phase,
    RunLimits,
    RunObserver,
    RunResult,
    RunSpec,
    RuntimeAvailability,
    choose_runtime,
    classify,
    execution_offer,
)
from meshforge.agent.execution.runtime import (
    MAX_ARG_LENGTH,
    MAX_ARGV_ITEMS,
    MAX_ENV_ITEMS,
    MAX_ENV_VALUE_LENGTH,
    MAX_GPU_INDEX,
)
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id

pytestmark = pytest.mark.unit

WORK = Path("/tmp/mf-work").resolve()
LIMITS = RunLimits(memory_bytes=64 << 20, pids=64, timeout_s=10.0)


def _spec(**overrides: Any) -> RunSpec:
    fields: dict[str, Any] = {
        "attempt_id": new_attempt_id(),
        "argv": ("python", "main.py"),
        "workdir": WORK,
        "env": {"PATH": "/usr/bin"},
        "limits": LIMITS,
    }
    fields.update(overrides)
    return RunSpec(**fields)


# ------------------------------------------------------------------ RunSpec
def test_a_valid_spec_is_frozen_and_normalised() -> None:
    spec = _spec(argv=["python", "x.py"], gpu_indices=[1, 0], setup_argv=["pip", "i"])
    assert spec.argv == ("python", "x.py")
    assert spec.gpu_indices == (1, 0)
    assert spec.setup_argv == ("pip", "i")
    assert spec.deps_dir == WORK / ".meshforge" / "deps"
    with pytest.raises(TypeError):
        spec.env["X"] = "1"  # type: ignore[index]


@pytest.mark.parametrize(
    "overrides",
    [
        {"attempt_id": "not-an-attempt"},
        {"attempt_id": "job_" + "0" * 26},
        {"argv": ()},
        {"argv": ("",)},
        {"argv": ("python", "a\x00b")},
        {"setup_argv": ()},
        {"workdir": Path("relative/dir")},
        {"env": {"1BAD": "x"}},
        {"env": {"A=B": "x"}},
        {"env": {"OK": "nul\x00"}},
        {"gpu_indices": (-1,)},
        {"gpu_indices": (0, 0)},
        {"grace_s": 0.0},
        {"grace_s": 30.001},
        {"image": "  "},
    ],
)
def test_invalid_specs_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        _spec(**overrides)


def test_argv_bounds_max_ok_and_max_plus_one_refused() -> None:
    _spec(argv=("x",) * MAX_ARGV_ITEMS)
    with pytest.raises(ValueError, match="items"):
        _spec(argv=("x",) * (MAX_ARGV_ITEMS + 1))
    _spec(argv=("p", "a" * MAX_ARG_LENGTH))
    with pytest.raises(ValueError, match="over"):
        _spec(argv=("p", "a" * (MAX_ARG_LENGTH + 1)))


def test_env_bounds_max_ok_and_max_plus_one_refused() -> None:
    _spec(env={f"V{i}": "" for i in range(MAX_ENV_ITEMS)})
    with pytest.raises(ValueError, match="variables"):
        _spec(env={f"V{i}": "" for i in range(MAX_ENV_ITEMS + 1)})
    _spec(env={"V": "a" * MAX_ENV_VALUE_LENGTH})
    with pytest.raises(ValueError, match="over"):
        _spec(env={"V": "a" * (MAX_ENV_VALUE_LENGTH + 1)})
    _spec(env={"A" * 128: ""})
    with pytest.raises(ValueError):
        _spec(env={"A" * 129: ""})


def test_gpu_index_bound() -> None:
    _spec(gpu_indices=(MAX_GPU_INDEX,))
    with pytest.raises(ValueError):
        _spec(gpu_indices=(MAX_GPU_INDEX + 1,))


@pytest.mark.parametrize(
    "limits",
    [
        {"memory_bytes": (6 << 20) - 1, "pids": 1, "timeout_s": 1.0},
        {"memory_bytes": 6 << 20, "pids": 0, "timeout_s": 1.0},
        {"memory_bytes": 6 << 20, "pids": 1, "timeout_s": 0.0},
        {"memory_bytes": 6 << 20, "pids": 1, "timeout_s": 1.0, "cpus": 0.0},
    ],
)
def test_invalid_limits_are_refused(limits: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RunLimits(**limits)


def test_result_detail_is_clipped_and_printable() -> None:
    result = RunResult(exit_code=1, detail="\x1b[31mred\x00" + "x" * 1000)
    assert "\x1b" not in result.detail and "\x00" not in result.detail
    assert len(result.detail) == 500
    assert result.phases == {"env": 0.0, "run": 0.0}


# --------------------------------------------------------------- classify
@pytest.mark.parametrize(
    ("exit_code", "sig", "timed_out", "oom", "expected"),
    [
        (0, None, False, False, None),
        (1, None, False, False, FailureType.USER_ERROR),
        (None, 9, False, False, FailureType.USER_ERROR),
        (137, None, False, True, FailureType.OOM),
        (None, 9, True, False, FailureType.DEADLINE_EXCEEDED),
        (0, None, True, False, FailureType.DEADLINE_EXCEEDED),
        (None, 9, True, True, FailureType.DEADLINE_EXCEEDED),
    ],
)
def test_classify_follows_architecture_13(
    exit_code: int | None,
    sig: int | None,
    timed_out: bool,
    oom: bool,
    expected: FailureType | None,
) -> None:
    assert (
        classify(exit_code=exit_code, signal=sig, timed_out=timed_out, oom_killed=oom)
        is expected
    )


# ---------------------------------------------------------------- observer
def test_observer_phase_timings_on_an_injected_clock() -> None:
    now = [100.0]
    obs = RunObserver(clock=lambda: now[0])
    obs.enter_env()
    now[0] = 102.5
    obs.enter_run()
    assert obs.env_s == 2.5 and obs.phase is Phase.RUN
    now[0] = 110.0
    obs.exit_code = None
    obs.finish()
    assert obs.run_s == 7.5
    assert str(obs.phase) == Phase.DONE.value
    result = obs.cancelled_result()
    assert result.cancelled and result.failure_type is FailureType.CANCELLED
    assert (result.env_s, result.run_s) == (2.5, 7.5)


def test_observer_cancelled_during_env() -> None:
    now = [0.0]
    obs = RunObserver(clock=lambda: now[0])
    obs.enter_env()
    now[0] = 4.0
    obs.finish()
    assert (obs.env_s, obs.run_s) == (4.0, 0.0)


# ------------------------------------------------------------ capabilities
DOCKER = RuntimeAvailability("docker", True, gpu_capable=True, sandboxed=True)
DOCKER_CPU = RuntimeAvailability("docker", True, sandboxed=True)
DOCKER_DOWN = RuntimeAvailability("docker", False, reason="docker_daemon_unreachable")
SUBPROC = RuntimeAvailability("subprocess", True)
SUBPROC_GPU = RuntimeAvailability("subprocess", True, gpu_capable=True)


def test_disabled_offers_nothing_even_with_docker() -> None:
    offer = execution_offer(
        enabled=False, allow_unsandboxed=True, docker=DOCKER, subprocess=SUBPROC
    )
    assert offer.capabilities == ()
    assert choose_runtime(offer, GpuNeed.NONE) is None


def test_subprocess_needs_the_second_opt_in() -> None:
    offer = execution_offer(
        enabled=True, allow_unsandboxed=False, docker=DOCKER_DOWN, subprocess=SUBPROC
    )
    assert offer.capabilities == ()
    assert choose_runtime(offer, GpuNeed.NONE) is None
    offer = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=DOCKER_DOWN, subprocess=SUBPROC
    )
    assert offer.capabilities == (EXEC_CAPABILITY, EXEC_SUBPROCESS_CAPABILITY)
    assert choose_runtime(offer, GpuNeed.NONE) is SUBPROC


def test_docker_is_preferred_and_gpu_is_advertised_only_when_capable() -> None:
    offer = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=DOCKER_CPU, subprocess=SUBPROC_GPU
    )
    assert offer.capabilities == (
        EXEC_CAPABILITY,
        EXEC_DOCKER_CAPABILITY,
        EXEC_GPU_CAPABILITY,
        EXEC_SUBPROCESS_CAPABILITY,
    )
    assert choose_runtime(offer, GpuNeed.NONE) is DOCKER_CPU
    assert choose_runtime(offer, GpuNeed.REQUIRED) is SUBPROC_GPU
    assert choose_runtime(offer, GpuNeed.PREFERRED) is SUBPROC_GPU
    cpu_only = execution_offer(
        enabled=True, allow_unsandboxed=False, docker=DOCKER_CPU, subprocess=None
    )
    assert EXEC_GPU_CAPABILITY not in cpu_only.capabilities
    assert choose_runtime(cpu_only, GpuNeed.REQUIRED) is None
    assert choose_runtime(cpu_only, GpuNeed.PREFERRED) is DOCKER_CPU
