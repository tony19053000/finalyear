"""Job execution runtimes (V0.4, brief D51, ADR-0004): the :class:`Runtime`
contract, the Docker sandbox, the unsandboxed subprocess fallback, and the
probe/selection/capability logic. The executor that uses them is G4's.

Import the concrete runtimes from their modules
(:mod:`.docker_runtime`, :mod:`.subprocess_runtime`); this package only
re-exports the contract so importing it never pulls in the Docker SDK.
"""

from meshforge.agent.execution.runtime import (
    DEFAULT_GRACE_S,
    DEPS_SUBDIR,
    OutputCallback,
    Phase,
    RunLimits,
    RunObserver,
    RunResult,
    RunSpec,
    Runtime,
    RuntimeAvailability,
    Stream,
    classify,
)
from meshforge.agent.execution.selection import (
    EXEC_CAPABILITY,
    EXEC_DOCKER_CAPABILITY,
    EXEC_GPU_CAPABILITY,
    EXEC_SUBPROCESS_CAPABILITY,
    ExecutionOffer,
    GpuNeed,
    choose_runtime,
    execution_offer,
)

__all__ = [
    "DEFAULT_GRACE_S",
    "DEPS_SUBDIR",
    "EXEC_CAPABILITY",
    "EXEC_DOCKER_CAPABILITY",
    "EXEC_GPU_CAPABILITY",
    "EXEC_SUBPROCESS_CAPABILITY",
    "ExecutionOffer",
    "GpuNeed",
    "OutputCallback",
    "Phase",
    "RunLimits",
    "RunObserver",
    "RunResult",
    "RunSpec",
    "Runtime",
    "RuntimeAvailability",
    "Stream",
    "choose_runtime",
    "classify",
    "execution_offer",
]
