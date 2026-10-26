"""Typed agent-session failures (ARCHITECTURE §13; roadmap 0.3.8).

Two of them end the process:

* :class:`SupersededError` - the controller admitted a *newer* stream for this
  ``node_id`` (ADR-0002 Updates §1). Another instance of this node is running;
  this one must not fight it. Exit ``CONFLICT``.
* :class:`AgentContractError` - the controller told us our own Hello is
  unacceptable (unsupported protocol version, malformed Hello). Retrying will not
  help; exit ``CONFIG_ERROR``.
* :class:`NodeRevokedError` (an ``AgentContractError``) - an admin revoked this
  node (V0.7 D138). Same exit code, but an expected stop: ``run_agent`` logs it
  as ``agent.revoked`` with no traceback (V0.8 G5).
* :class:`VersionUnsupportedError` (an ``AgentContractError``) - the controller
  does not accept this agent's version (V0.17 G5, Q37): too old ("upgrade the
  agent") or newer than the controller ("upgrade the controller first").

Everything else on the stream is transient and handled by the reconnect loop
(:mod:`meshforge.agent.session.policy`).
"""

from __future__ import annotations

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError

__all__ = [
    "AgentContractError",
    "AgentSessionError",
    "NodeRevokedError",
    "SupersededError",
    "VersionUnsupportedError",
]


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


class NodeRevokedError(AgentContractError):
    """An admin revoked this node: the controller cut its stream for good.

    The message is the controller's own detail, which says how to come back
    (``meshforge-agent identity reset --yes`` + a new join code)."""

    def __init__(self, node_id: str, detail: str) -> None:
        super().__init__(
            detail.strip()
            or "node_revoked: this node was revoked from the fabric; it can come "
            "back only under a new identity",
            details={"node_id": node_id, "reason": "node_revoked"},
        )


class VersionUnsupportedError(AgentContractError):
    """The controller refused this agent's version (``version_unsupported``).

    The message is the controller's reason (both versions and the fix); the
    agent stops instead of retrying - only an upgrade can change the answer."""

    def __init__(self, node_id: str, reason: str) -> None:
        super().__init__(
            reason.strip()
            or "version_unsupported: the controller does not accept this agent's version",
            details={"node_id": node_id, "reason": "version_unsupported"},
        )
