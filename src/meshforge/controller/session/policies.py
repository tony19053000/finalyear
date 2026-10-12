"""The owner policy a node advertises (V0.7 G5, roadmap 0.7.8, D142).

``Hello.policy`` and the ``PolicyUpdate`` report carry the node owner's
:class:`~meshforge.common.owner_policy.OwnerPolicy`; it is kept **in memory** on
the node's :class:`~.live.LiveSession` (like the live resource sample - the
policy only matters while the node is connected, and every Hello carries it
again). The planner reads it from there (``controller/jobs/planning.py``).

* absent in Hello (an agent from before G5) -> ``None``: the planner assumes
  :data:`~meshforge.common.owner_policy.OPEN_POLICY`; that agent still enforces
  its own opt-ins;
* present and valid -> stored as is;
* present but malformed or oversized -> refused **as a whole** (logged
  ``session.policy_refused`` with the reason, counted): the node is treated as
  accepting nothing (``DENY_ALL``, fail closed) until a valid policy arrives -
  never trusted partially, never a reason to close the stream.

A ``PolicyUpdate`` also carries the slot limits (``max_cpu_jobs`` /
``max_gpu_jobs``, the same bound as Hello's); they override the Hello's for the
rest of the session.
"""

from __future__ import annotations

from typing import Final

from meshforge.common import owner_policy
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.session.live import LiveSession
from meshforge.controller.store.models.node import MAX_SLOTS

__all__ = ["PolicyIntake", "hello_policy"]

log = get_logger(__name__)

_MISSING: Final = "missing"


def hello_policy(
    hello: agent_pb2.Hello,
) -> tuple[owner_policy.OwnerPolicy | None, str | None]:
    """``(policy, refusal)``: None/None when absent; ``DENY_ALL`` + the reason
    when present but not valid as a whole."""
    if not hello.HasField("policy"):
        return None, None
    try:
        return owner_policy.from_proto(hello.policy), None
    except owner_policy.PolicyError as exc:
        return owner_policy.DENY_ALL, exc.reason


class PolicyIntake:
    """Applies Hello / PolicyUpdate policies to live sessions; counts refusals."""

    def __init__(self) -> None:
        self.refused_total = 0
        self.updates_total = 0

    def on_hello(self, live: LiveSession, hello: agent_pb2.Hello) -> None:
        policy, refusal = hello_policy(hello)
        live.policy = policy
        live.policy_refused = refusal
        if refusal is not None:
            self._refused(live, refusal, where="hello")

    def on_update(self, live: LiveSession, update: agent_pb2.PolicyUpdate) -> None:
        """Replace the session's policy (and slots) - or fail closed."""
        self.updates_total += 1
        refusal: str | None = None
        policy = owner_policy.DENY_ALL
        if not update.HasField("policy"):
            refusal = _MISSING
        else:
            try:
                policy = owner_policy.from_proto(update.policy)
            except owner_policy.PolicyError as exc:
                refusal = exc.reason
        cpu = update.max_cpu_jobs if update.HasField("max_cpu_jobs") else None
        gpu = update.max_gpu_jobs if update.HasField("max_gpu_jobs") else None
        if (cpu is not None and cpu > MAX_SLOTS) or (
            gpu is not None and gpu > MAX_SLOTS
        ):
            refusal = refusal or "slots_out_of_range"
            policy = owner_policy.DENY_ALL
        if refusal is not None:
            live.policy = owner_policy.DENY_ALL
            live.policy_refused = refusal
            live.policy_slots = (0, 0)
            self._refused(live, refusal, where="update")
            return
        live.policy = policy
        live.policy_refused = None
        live.policy_slots = (cpu, gpu) if policy.accept_jobs else (0, 0)
        log.info(
            "session.policy_updated",
            node_id=live.node_id,
            session_id=live.session_id,
            revision=policy.revision,
            accept_jobs=policy.accept_jobs,
            cpu_slots=cpu,
            gpu_slots=gpu,
        )

    def _refused(self, live: LiveSession, reason: str, *, where: str) -> None:
        self.refused_total += 1
        log.warning(
            "session.policy_refused",
            node_id=live.node_id,
            session_id=live.session_id,
            where=where,
            reason=reason,
            refused_total=self.refused_total,
        )
