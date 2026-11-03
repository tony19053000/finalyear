"""Capability tags both sides of the wire must spell the same way.

``HardwareSummary.capabilities`` is a free-form list (``agent.proto``); most tags
are only *read* by the planner. A tag whose meaning the controller acts on lives
here, so the agent (which sends it) and the controller (which interprets it)
share one constant without importing each other.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "EXEC_CAPABILITY",
    "EXEC_DOCKER_CAPABILITY",
    "EXEC_GPU_CAPABILITY",
    "EXEC_SPLIT_CAPABILITY",
    "EXEC_SUBPROCESS_CAPABILITY",
    "HARDWARE_SCORES_CAPABILITY",
]

HARDWARE_SCORES_CAPABILITY: Final = "hardware-scores"
"""The agent measures hardware scores (roadmap 0.3.4): ``[agent.scores] enabled``
and hardware discovery succeeded. A node with this tag and no stored scores is
``pending`` in the REST ``scores.status``; a node without it is ``none`` - it will
not produce scores unless its configuration changes (brief §5g D37/D38)."""

EXEC_CAPABILITY: Final = "exec"
"""The agent **can** run jobs: at least one runtime was found (D45/D50). The
controller never places or dispatches a job on a node without it. Whether the
owner **allows** jobs is the live policy (``accept_jobs``; V1.0 G8 F5, D428),
not this tag: an agent from V1.0 on advertises it whatever ``accept_jobs`` says,
one from before only when ``accept_jobs`` was on at its start."""
EXEC_DOCKER_CAPABILITY: Final = "exec-docker"
"""A usable Docker runtime (D51)."""
EXEC_SUBPROCESS_CAPABILITY: Final = "exec-subprocess"
"""The unsandboxed subprocess runtime, only with ``allow_unsandboxed`` (D45)."""
EXEC_GPU_CAPABILITY: Final = "exec-gpu"
"""A runtime can hand an NVIDIA GPU to a job; required for ``resources.gpu:
required`` placement (D50)."""
EXEC_SPLIT_CAPABILITY: Final = "exec-split"
"""V0.10 G4 (D263): the agent runs Mode 3 lanes (``StartAttempt.lane``: chunk
pull, per-chunk process, chunk results). The controller places a split job's
lanes only on nodes with this tag - an older agent would run a lane as one
whole attempt."""
