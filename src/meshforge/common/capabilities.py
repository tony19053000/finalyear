"""Capability tags both sides of the wire must spell the same way.

``HardwareSummary.capabilities`` is a free-form list (``agent.proto``); most tags
are only *read* by the planner. A tag whose meaning the controller acts on lives
here, so the agent (which sends it) and the controller (which interprets it)
share one constant without importing each other.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

__all__ = [
    "ENV_POLICY_PREFIX",
    "EXEC_CAPABILITY",
    "EXEC_DOCKER_CAPABILITY",
    "EXEC_GPU_CAPABILITY",
    "EXEC_SPLIT_CAPABILITY",
    "EXEC_SUBPROCESS_CAPABILITY",
    "EXEC_TORCH_CAPABILITY",
    "HARDWARE_SCORES_CAPABILITY",
    "TORCH_BASES",
    "env_policy_of",
    "env_policy_tag",
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
EXEC_TORCH_CAPABILITY: Final = "exec-torch"
"""V1.0 G9-B (D432): the interpreter the subprocess runtime runs jobs with (the
agent's own) has PyTorch installed - probed with ``importlib.util.find_spec``,
never imported. A :data:`TORCH_BASES` job is placed on a node only with
``exec-docker`` (the image has torch) or ``exec-subprocess`` + this tag: on a
Windows laptop without Docker the bundled interpreter has no torch, and the job
would end ``user_error`` (``ModuleNotFoundError``) instead of never going there.
The rule applies only to a node that also sends :data:`ENV_POLICY_PREFIX` (the
G9-B marker): an agent from before V1.0 G9-B, or a snapshot recorded before it,
keeps the V0.6 rule, so stored decisions replay unchanged."""
TORCH_BASES: Final = frozenset({"cuda-pytorch"})
"""Manifest ``env.base`` values that promise PyTorch to the job (D432)."""
EXEC_SPLIT_CAPABILITY: Final = "exec-split"
"""V0.10 G4 (D263): the agent runs Mode 3 lanes (``StartAttempt.lane``: chunk
pull, per-chunk process, chunk results). The controller places a split job's
lanes only on nodes with this tag - an older agent would run a lane as one
whole attempt."""

ENV_POLICY_PREFIX: Final = "env-policy:"
"""V1.0 G9-B (D433): ``env-policy:<12 hex>`` - a short hash of the node owner's
``[agent.envs]`` settings that change what an environment build can do
(wheels-only, package indexes, wheelhouse, size limits...). The controller
keeps it with every known-bad environment (D116) and forgets the entry once
the node advertises another one: the owner changed the policy, so the build
may now succeed. A node without the tag (an agent from before G9-B) keeps the
old rule (the entry lives its TTL)."""
_ENV_POLICY_RE: Final = re.compile(r"[0-9a-f]{12}")


def env_policy_tag(fingerprint: str) -> str:
    """The capability tag for a 12-hex fingerprint (ValueError otherwise)."""
    if not _ENV_POLICY_RE.fullmatch(fingerprint):
        raise ValueError("an env-policy fingerprint is 12 lowercase hex digits")
    return ENV_POLICY_PREFIX + fingerprint


def env_policy_of(capabilities: Iterable[str]) -> str | None:
    """The fingerprint a node advertises, or None (no tag, a malformed one, or
    two different ones - a peer value is never trusted half-way)."""
    found = {
        tag.removeprefix(ENV_POLICY_PREFIX)
        for tag in capabilities
        if tag.startswith(ENV_POLICY_PREFIX)
    }
    if len(found) != 1:
        return None
    (value,) = found
    return value if _ENV_POLICY_RE.fullmatch(value) else None
