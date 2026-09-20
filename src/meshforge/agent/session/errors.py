"""Typed agent-session failures (ARCHITECTURE §13; roadmap 0.3.8).

Two of them end the process:

* :class:`SupersededError` - the controller admitted a *newer* stream for this
  ``node_id`` (ADR-0002 Updates §1). Another instance of this node is running;
  this one must not fight it. Exit ``CONFLICT``.
* :class:`AgentContractError` - the controller told us our own Hello is
  unacceptable (unsupported protocol version, malformed Hello). Retrying will not
  help; exit ``CONFIG_ERROR``.

Everything else on the stream is transient and handled by the reconnect loop
(:mod:`meshforge.agent.session.policy`).
"""

from __future__ import annotations

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError

__all__ = ["AgentContractError", "AgentSessionError", "SupersededError"]


class AgentSessionError(MeshForgeError):
    """Base of agent-session failures."""


class SupersededError(AgentSessionError):
    """A newer session for this ``node_id`` replaced ours."""

    default_failure_type = FailureType.CONFLICT
    exit_code = ExitCode.CONFLICT

    def __init__(self, node_id: str, session_id: str) -> None:
        super().__init__(
            f"another meshforge-agent with node_id {node_id} connected to the "
            "controller and superseded this one; stopping instead of fighting it. "
            "Stop the other instance, or give this one its own data_dir "
            "(two agents must never share an identity directory).",
            details={"node_id": node_id, "session_id": session_id},
        )


class AgentContractError(AgentSessionError):
    """The controller rejected our Hello for a reason a retry cannot fix."""

    default_failure_type = FailureType.CONFIG_ERROR
    exit_code = ExitCode.CONFIG_ERROR
