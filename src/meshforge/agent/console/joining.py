"""Join from the console: the **same** :func:`meshforge.agent.enrollment.join`
as ``meshforge-agent join``, with its errors said in plain words (D392).

:func:`explain` maps a :class:`~meshforge.agent.enrollment.JoinError` (and the
controller's refusal code, when it gave one) to a :class:`JoinProblem`: a stable
``code`` the page can test, one sentence for the owner, and what to do. The
technical message stays in the agent's log, never the code or its token.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final

from meshforge.agent.enrollment import JoinError, JoinReason, JoinResult
from meshforge.agent.enrollment import join as run_join
from meshforge.agent.identity import load_or_create
from meshforge.agent.settings import AgentSettings
from meshforge.common.errors import MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.common.netaddr import is_lan_address

__all__ = ["JoinProblem", "JoinProblemError", "explain", "join_from_console"]

log = get_logger(__name__)

JOIN_TIMEOUT_S: Final = 15.0


@dataclass(frozen=True, slots=True)
class JoinProblem:
    code: str
    """``expired`` | ``used`` | ``revoked_code`` | ``bad_code`` | ``ca_mismatch``
    | ``unreachable`` | ``not_a_controller`` | ``already_joined`` |
    ``node_revoked`` | ``busy`` | ``refused`` | ``storage`` | ``identity``."""
    message: str
    fix: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "fix": self.fix}


class JoinProblemError(Exception):
    """A join that did not happen, already explained."""

    def __init__(self, problem: JoinProblem) -> None:
        super().__init__(problem.message)
        self.problem = problem


_ASK_NEW: Final = "Ask the fabric admin for a new code (Invites page) and try again."
_BY_CONTROLLER: Final = {
    "token_expired": JoinProblem("expired", "This code has expired.", _ASK_NEW),
    "token_used": JoinProblem(
        "used", "This code was already used to join a laptop.", _ASK_NEW
    ),
    "token_revoked": JoinProblem(
        "revoked_code", "The fabric admin cancelled this code.", _ASK_NEW
    ),
    "token_unknown": JoinProblem(
        "bad_code", "The controller does not know this code.", _ASK_NEW
    ),
    "node_revoked": JoinProblem(
        "node_revoked",
        "This laptop was removed from the fabric by its admin.",
        "Leave the fabric here (Settings > Leave), then join again with a new code.",
    ),
    "rate_limited": JoinProblem(
        "busy",
        "The controller is busy with other joins right now.",
        "Wait a minute and try again.",
    ),
    "unavailable": JoinProblem(
        "busy",
        "The controller cannot accept joins right now.",
        "Wait a minute and try again; if it keeps failing, ask the fabric admin.",
    ),
}


def explain(exc: JoinError, *, address: str | None = None) -> JoinProblem:
    """The owner's words for ``exc`` (module docstring)."""
    where = f" at {address}" if address else ""
    reason = exc.reason
    if reason is JoinReason.REFUSED and exc.controller_reason in _BY_CONTROLLER:
        return _BY_CONTROLLER[exc.controller_reason]
    if reason is JoinReason.BAD_CODE:
        return JoinProblem(
            "bad_code",
            "That is not a complete MeshForge join code.",
            "Check for a missing or mistyped part (codes start with mf1-), or scan "
            "the QR code instead.",
        )
    if reason is JoinReason.ALREADY_ENROLLED:
        return JoinProblem(
            "already_joined",
            "This laptop has already joined a fabric.",
            "To join another one, leave the current fabric first (Settings > Leave).",
        )
    if reason is JoinReason.UNREACHABLE:
        return JoinProblem(
            "unreachable",
            f"Cannot reach the controller{where}.",
            "Check that the controller laptop is on and on the same network. If its "
            "address changed, pick it from the list of controllers found on this "
            "network.",
        )
    if reason is JoinReason.TLS_ERROR:
        return JoinProblem(
            "not_a_controller",
            f"Something answered{where}, but it is not a MeshForge controller's join "
            "port.",
            "Pick the controller from the list found on this network, or ask the "
            "admin for a new code.",
        )
    if reason is JoinReason.PIN_MISMATCH:
        return JoinProblem(
            "ca_mismatch",
            f"The controller{where} is not the fabric this code belongs to (its "
            "certificate does not match the code). Nothing secret was sent.",
            "Make sure you picked the right controller, or ask for a new code.",
        )
    if reason is JoinReason.STORAGE:
        return JoinProblem(
            "storage",
            "Joined, but this laptop could not save its certificate.",
            "Check the free disk space and the agent's log, then join again with a "
            "new code.",
        )
    return JoinProblem(
        "refused",
        "The controller refused to let this laptop join.",
        "Ask the fabric admin to check the controller's log, or try a new code.",
    )


async def join_from_console(
    settings: AgentSettings, code: str, *, host: str | None = None
) -> JoinResult:
    """Join (blocking work in a thread); :class:`JoinProblemError` on failure.
    ``host`` must be a LAN address (the console offers only discovered ones)."""
    if host is not None and not is_lan_address(host.strip().strip("[]")):
        raise JoinProblemError(
            JoinProblem(
                "unreachable",
                "That controller address is not on this network.",
                "Pick a controller from the list, or leave the choice empty to use "
                "the address in the code.",
            )
        )

    def work() -> JoinResult:
        identity = load_or_create(
            settings.identity_dir, lock_timeout_s=settings.agent.identity_lock_timeout_s
        )
        return run_join(identity, code, timeout_s=JOIN_TIMEOUT_S, host=host)

    try:
        return await asyncio.to_thread(work)
    except JoinError as exc:
        problem = explain(exc, address=host)
        log.warning(
            "agent.console.join_failed",
            reason=exc.reason.value,
            controller_reason=exc.controller_reason,
            problem=problem.code,
            error=exc.message,
        )
        raise JoinProblemError(problem) from None
    except MeshForgeError as exc:  # identity unreadable, pending migration, lock
        log.warning(
            "agent.console.join_failed",
            reason="identity",
            failure_type=exc.failure_type.value,
            error=exc.message,
        )
        raise JoinProblemError(
            JoinProblem(
                "identity",
                f"This laptop's identity cannot be used: {exc.message}",
                "See the Logs page; the fix is in the message above.",
            )
        ) from None
