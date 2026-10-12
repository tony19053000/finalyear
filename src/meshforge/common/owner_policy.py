"""The node owner's policy (V0.7 G5, roadmap 0.7.8, D142) - one rule, two checkers.

The **agent is authoritative**: its owner sets ``[agent.policy]`` (and changes it
live with ``meshforge-agent policy set``). The agent advertises the policy in
``Hello`` and in a ``PolicyUpdate`` report; the controller's planner rejects a node
whose policy refuses the job (codes below, shown in ``plan_decision`` and ``compute
explain``) and the agent **re-checks every StartAttempt** against its *current*
policy - defence in depth: the owner wins even if the controller is wrong or out of
date. Both sides call :func:`refusal`, so the rule exists once.

Fields (all optional limits: ``None`` = no owner limit):

* ``accept_jobs`` - run jobs at all (replaces ``[agent.execution] enabled``);
* ``accept_gpu_sessions`` - Remote-GPU sessions (stored and advertised now; used
  from V0.11);
* ``max_ram_bytes`` / ``max_cpu_percent`` / ``max_vram_bytes`` - the most one job
  may ask for (RAM; share of the node's CPU threads; VRAM of a GPU job). They are
  also the runtime limits the agent applies (Docker ``--memory``/``--cpus``,
  rlimits) - where an OS cannot enforce one, the agent says so in
  ``not_enforced`` instead of claiming it;
* ``allow_unsandboxed`` - the subprocess/venv runtimes (no sandbox) may run jobs;
* ``allow_network`` - a job whose manifest asks ``limits.network: on`` may get a
  network; without it no job gets one (D143);
* ``allowed_users`` - user names whose jobs this node runs; empty = everyone. A
  job without a known owner (the legacy fabric token) never matches a non-empty
  list (fail closed).

Validation (:func:`validate`) is strict and total: a policy from the network or a
file is either valid as a whole or refused - never trusted partially.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = [
    "DENY_ALL",
    "MAX_ALLOWED_USERS",
    "MAX_NOT_ENFORCED",
    "MAX_POLICY_BYTES",
    "OPEN_POLICY",
    "USER_NAME_RE",
    "JobAsk",
    "OwnerPolicy",
    "PolicyCode",
    "PolicyError",
    "PolicyRefusal",
    "allowed_cpus",
    "from_proto",
    "refusal",
    "to_proto",
    "validate",
]

MAX_ALLOWED_USERS: Final = 64
MAX_NOT_ENFORCED: Final = 16
MAX_POLICY_BYTES: Final = 8 * 1024
"""Largest serialized ``OwnerPolicy`` the controller reads (64 names x 64 chars
fit with room to spare)."""
MAX_RAM_BYTES: Final = 1 << 50
MIN_RAM_BYTES: Final = 64 * 1024 * 1024
MAX_VRAM_BYTES: Final = 1 << 44
USER_NAME_RE: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
"""The user-name rule of the controller's ``app_user`` table (D141, D158)."""
_NOT_ENFORCED_RE: Final = re.compile(r"[a-z_]{1,32}:[a-z0-9_-]{1,32}")
"""``<field>:<why>`` - e.g. ``max_vram_bytes:no_runtime_limit``."""


class PolicyCode(StrEnum):
    """Why an owner's policy refuses a job. Stored in ``plan_decision`` and in
    an agent's ``policy_denied:<code>`` refusal: never rename, only add."""

    NOT_ACCEPTING = "policy_not_accepting"
    USER_NOT_ALLOWED = "policy_user_not_allowed"
    RAM = "policy_ram"
    CPU = "policy_cpu"
    VRAM = "policy_vram"
    UNSANDBOXED = "policy_unsandboxed"
    NETWORK = "policy_network"


class PolicyError(MeshForgeError):
    """A policy that is not valid as a whole (``reason`` names the first fault)."""

    default_failure_type = FailureType.INVALID_INPUT

    def __init__(self, reason: str) -> None:
        super().__init__(f"owner policy refused: {reason}", details={"reason": reason})
        self.reason = reason


@dataclass(frozen=True, slots=True)
class OwnerPolicy:
    """See module docstring. Build it through :func:`validate` (or
    :func:`from_proto`) - the constructor itself checks nothing."""

    accept_jobs: bool = False
    accept_gpu_sessions: bool = False
    max_ram_bytes: int | None = None
    max_cpu_percent: int | None = None
    max_vram_bytes: int | None = None
    allow_unsandboxed: bool = False
    allow_network: bool = False
    allowed_users: tuple[str, ...] = ()
    not_enforced: tuple[str, ...] = ()
    """``<field>:<why>`` caps the agent cannot enforce by a runtime limit on this
    OS/runtime (D142 item 5: say it, never claim it)."""
    revision: int = 0
    """Agent-local counter, +1 per live change (for logs; 0 = from Hello)."""

    def as_dict(self) -> dict[str, object]:
        return {
            "accept_jobs": self.accept_jobs,
            "accept_gpu_sessions": self.accept_gpu_sessions,
            "max_ram_bytes": self.max_ram_bytes,
            "max_cpu_percent": self.max_cpu_percent,
            "max_vram_bytes": self.max_vram_bytes,
            "allow_unsandboxed": self.allow_unsandboxed,
            "allow_network": self.allow_network,
            "allowed_users": list(self.allowed_users),
            "not_enforced": list(self.not_enforced),
            "revision": self.revision,
        }


DENY_ALL: Final = OwnerPolicy()
"""Fail-closed policy: nothing accepted (used for a malformed one)."""
OPEN_POLICY: Final = OwnerPolicy(
    accept_jobs=True, allow_unsandboxed=True, allow_network=True
)
"""What a node that advertises no policy (an agent from before V0.7 G5) is
taken to allow - its own agent still enforces whatever it enforces."""


def _check_users(users: Iterable[str]) -> tuple[str, ...]:
    names = tuple(users)
    if len(names) > MAX_ALLOWED_USERS:
        raise PolicyError(f"allowed_users has more than {MAX_ALLOWED_USERS} names")
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str) or USER_NAME_RE.fullmatch(name) is None:
            raise PolicyError(
                "allowed_users holds a name that is not a user name (1-64 of "
                "a-z 0-9 . _ -, starting with a letter or digit)"
            )
        if name in seen:
            raise PolicyError("allowed_users names a user twice")
        seen.add(name)
    return names


def validate(policy: OwnerPolicy) -> OwnerPolicy:
    """``policy`` itself when every field is in range; :class:`PolicyError` else."""
    for flag in (
        policy.accept_jobs,
        policy.accept_gpu_sessions,
        policy.allow_unsandboxed,
        policy.allow_network,
    ):
        if not isinstance(flag, bool):
            raise PolicyError("a yes/no field is not a boolean")
    ram = policy.max_ram_bytes
    if ram is not None and not (
        isinstance(ram, int) and MIN_RAM_BYTES <= ram <= MAX_RAM_BYTES
    ):
        raise PolicyError(
            f"max_ram_bytes must be {MIN_RAM_BYTES}..{MAX_RAM_BYTES} bytes"
        )
    pct = policy.max_cpu_percent
    if pct is not None and not (isinstance(pct, int) and 1 <= pct <= 100):
        raise PolicyError("max_cpu_percent must be 1..100")
    vram = policy.max_vram_bytes
    if vram is not None and not (isinstance(vram, int) and 1 <= vram <= MAX_VRAM_BYTES):
        raise PolicyError(f"max_vram_bytes must be 1..{MAX_VRAM_BYTES} bytes")
    _check_users(policy.allowed_users)
    if len(policy.not_enforced) > MAX_NOT_ENFORCED or any(
        not isinstance(item, str) or _NOT_ENFORCED_RE.fullmatch(item) is None
        for item in policy.not_enforced
    ):
        raise PolicyError("not_enforced is malformed")
    if not isinstance(policy.revision, int) or not 0 <= policy.revision < 1 << 63:
        raise PolicyError("revision out of range")
    return policy


def to_proto(policy: OwnerPolicy) -> agent_pb2.OwnerPolicy:
    msg = agent_pb2.OwnerPolicy(
        accept_jobs=policy.accept_jobs,
        accept_gpu_sessions=policy.accept_gpu_sessions,
        allow_unsandboxed=policy.allow_unsandboxed,
        allow_network=policy.allow_network,
        allowed_users=list(policy.allowed_users),
        not_enforced=list(policy.not_enforced),
        revision=policy.revision,
    )
    if policy.max_ram_bytes is not None:
        msg.max_ram_bytes = policy.max_ram_bytes
    if policy.max_cpu_percent is not None:
        msg.max_cpu_percent = policy.max_cpu_percent
    if policy.max_vram_bytes is not None:
        msg.max_vram_bytes = policy.max_vram_bytes
    return msg


def from_proto(msg: agent_pb2.OwnerPolicy) -> OwnerPolicy:
    """The validated policy of ``msg`` (:class:`PolicyError` if any part is bad:
    the whole policy is refused, never a part of it)."""
    if msg.ByteSize() > MAX_POLICY_BYTES:
        raise PolicyError(f"the policy is larger than {MAX_POLICY_BYTES} bytes")
    if len(msg.allowed_users) > MAX_ALLOWED_USERS:
        raise PolicyError(f"allowed_users has more than {MAX_ALLOWED_USERS} names")
    return validate(
        OwnerPolicy(
            accept_jobs=msg.accept_jobs,
            accept_gpu_sessions=msg.accept_gpu_sessions,
            max_ram_bytes=msg.max_ram_bytes if msg.HasField("max_ram_bytes") else None,
            max_cpu_percent=msg.max_cpu_percent
            if msg.HasField("max_cpu_percent")
            else None,
            max_vram_bytes=msg.max_vram_bytes
            if msg.HasField("max_vram_bytes")
            else None,
            allow_unsandboxed=msg.allow_unsandboxed,
            allow_network=msg.allow_network,
            allowed_users=tuple(msg.allowed_users),
            not_enforced=tuple(msg.not_enforced),
            revision=int(msg.revision),
        )
    )


@dataclass(frozen=True, slots=True)
class JobAsk:
    """What one job asks of one node, as both checkers know it."""

    owner_name: str | None
    """The job owner's user name; None = unknown (legacy fabric-token job)."""
    gpu_required: bool = False
    vram_bytes: int | None = None
    ram_bytes: int | None = None
    cpus: float | None = None
    network: bool = False
    """The manifest asks ``limits.network: on``."""
    sandboxed_runtime: bool = True
    """False = this node could run the job only on an unsandboxed runtime."""
    node_cpu_threads: int | None = None


@dataclass(frozen=True, slots=True)
class PolicyRefusal:
    code: PolicyCode
    text: str
    """Short, ours, safe to print (never a value from the network)."""


_GIB: Final = 1024**3


def allowed_cpus(policy: OwnerPolicy, cpu_threads: int | None) -> float | None:
    """The most CPUs (cores) one job may use under ``max_cpu_percent``
    (None = no owner limit, or the thread count is unknown)."""
    if policy.max_cpu_percent is None or not cpu_threads:
        return None
    return max(0.01, cpu_threads * policy.max_cpu_percent / 100.0)


def refusal(policy: OwnerPolicy, ask: JobAsk) -> PolicyRefusal | None:
    """The first reason ``policy`` refuses ``ask`` (None = allowed). Order:
    accepting, user, sandbox, network, VRAM, RAM, CPU."""
    if not policy.accept_jobs:
        return PolicyRefusal(
            PolicyCode.NOT_ACCEPTING,
            "the node's owner does not accept jobs now ([agent.policy] accept_jobs)",
        )
    if policy.allowed_users and (
        ask.owner_name is None or ask.owner_name not in policy.allowed_users
    ):
        return PolicyRefusal(
            PolicyCode.USER_NOT_ALLOWED,
            "the node's owner accepts jobs only from listed users "
            "([agent.policy] allowed_users)",
        )
    if not ask.sandboxed_runtime and not policy.allow_unsandboxed:
        return PolicyRefusal(
            PolicyCode.UNSANDBOXED,
            "the node could run this job only without a sandbox and its owner "
            "does not allow that ([agent.policy] allow_unsandboxed)",
        )
    if ask.network and not policy.allow_network:
        return PolicyRefusal(
            PolicyCode.NETWORK,
            "the job asks for a network and the node's owner does not allow one "
            "([agent.policy] allow_network)",
        )
    if (
        ask.gpu_required
        and ask.vram_bytes is not None
        and policy.max_vram_bytes is not None
        and ask.vram_bytes > policy.max_vram_bytes
    ):
        return PolicyRefusal(
            PolicyCode.VRAM,
            f"the job needs {ask.vram_bytes / _GIB:g} GB of VRAM; the node's owner "
            f"allows {policy.max_vram_bytes / _GIB:.1f} GB per job",
        )
    if (
        ask.ram_bytes is not None
        and policy.max_ram_bytes is not None
        and ask.ram_bytes > policy.max_ram_bytes
    ):
        return PolicyRefusal(
            PolicyCode.RAM,
            f"the job needs {ask.ram_bytes / _GIB:g} GB of RAM; the node's owner "
            f"allows {policy.max_ram_bytes / _GIB:.1f} GB per job",
        )
    cap = allowed_cpus(policy, ask.node_cpu_threads)
    if ask.cpus is not None and cap is not None and ask.cpus > cap:
        return PolicyRefusal(
            PolicyCode.CPU,
            f"the job needs {ask.cpus:g} CPUs; the node's owner allows {cap:g} "
            f"({policy.max_cpu_percent}% of its threads) per job",
        )
    return None
