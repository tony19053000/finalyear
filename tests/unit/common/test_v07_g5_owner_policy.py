"""V0.7 G5 (roadmap 0.7.8, D142): the owner-policy rule both sides apply.

``validate`` / ``from_proto`` refuse a policy as a whole (never partially);
``refusal`` gives every code, in a fixed order, and allows what is inside the
limits.
"""

from __future__ import annotations

import dataclasses

import pytest

from meshforge.common import owner_policy as op
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = [pytest.mark.unit, pytest.mark.security]

GIB = 1024**3
OPEN = op.OPEN_POLICY


def _open(**kw: object) -> op.OwnerPolicy:
    return dataclasses.replace(OPEN, **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------ validate


@pytest.mark.parametrize(
    "bad",
    [
        {"max_ram_bytes": op.MIN_RAM_BYTES - 1},
        {"max_ram_bytes": op.MAX_RAM_BYTES + 1},
        {"max_cpu_percent": 0},
        {"max_cpu_percent": 101},
        {"max_vram_bytes": 0},
        {"max_vram_bytes": op.MAX_VRAM_BYTES + 1},
        {"allowed_users": ("Alice",)},
        {"allowed_users": ("",)},
        {"allowed_users": ("al ice",)},
        {"allowed_users": ("a" * 65,)},
        {"allowed_users": ("k" + chr(0x212A),)},
        {"allowed_users": ("alice", "alice")},
        {"allowed_users": tuple(f"u{i}" for i in range(op.MAX_ALLOWED_USERS + 1))},
        {"not_enforced": ("bad",)},
        {"not_enforced": tuple(f"f:x{i}" for i in range(op.MAX_NOT_ENFORCED + 1))},
        {"revision": -1},
        {"accept_jobs": 1},
    ],
)
def test_an_out_of_range_field_refuses_the_whole_policy(bad: dict[str, object]) -> None:
    with pytest.raises(op.PolicyError):
        op.validate(_open(**bad))


def test_valid_policies_pass_unchanged() -> None:
    policy = _open(
        max_ram_bytes=8 * GIB,
        max_cpu_percent=50,
        max_vram_bytes=4 * GIB,
        allowed_users=("alice", "bob.smith", "c_3-d"),
        not_enforced=("max_vram_bytes:no_runtime_limit",),
        revision=3,
    )
    assert op.validate(policy) is policy
    assert op.validate(op.DENY_ALL) is op.DENY_ALL


# ------------------------------------------------------------------ the wire


def test_proto_round_trip_keeps_absent_limits_absent() -> None:
    policy = _open(max_ram_bytes=8 * GIB, allowed_users=("alice",), revision=7)
    msg = op.to_proto(policy)
    assert msg.HasField("max_ram_bytes")
    assert not msg.HasField("max_cpu_percent") and not msg.HasField("max_vram_bytes")
    again = agent_pb2.OwnerPolicy.FromString(msg.SerializeToString())
    assert op.from_proto(again) == policy


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.allowed_users.append("NOT-A-NAME"),
        lambda m: setattr(m, "max_cpu_percent", 0),
        lambda m: setattr(m, "max_cpu_percent", 1000),
        lambda m: setattr(m, "max_ram_bytes", 1),
        lambda m: m.not_enforced.append("x" * 100),
        lambda m: m.allowed_users.extend(f"u{i}" for i in range(65)),
    ],
)
def test_a_malformed_wire_policy_is_refused_as_a_whole(mutate: object) -> None:
    msg = op.to_proto(_open(allowed_users=("alice",)))
    mutate(msg)  # type: ignore[operator]
    with pytest.raises(op.PolicyError):
        op.from_proto(msg)


def test_an_oversized_wire_policy_is_refused_before_parsing_names() -> None:
    msg = op.to_proto(OPEN)
    msg.not_enforced.extend(["a" * 4000, "b" * 4000, "c" * 4000])
    with pytest.raises(op.PolicyError, match="larger than"):
        op.from_proto(msg)


# ------------------------------------------------------------------ refusal

ASK = op.JobAsk(owner_name="alice", node_cpu_threads=8)


@pytest.mark.parametrize(
    ("policy", "ask", "code"),
    [
        (op.DENY_ALL, ASK, op.PolicyCode.NOT_ACCEPTING),
        (_open(allowed_users=("bob",)), ASK, op.PolicyCode.USER_NOT_ALLOWED),
        (
            _open(allowed_users=("bob",)),
            dataclasses.replace(ASK, owner_name=None),
            op.PolicyCode.USER_NOT_ALLOWED,
        ),
        (
            _open(allow_unsandboxed=False),
            dataclasses.replace(ASK, sandboxed_runtime=False),
            op.PolicyCode.UNSANDBOXED,
        ),
        (
            _open(allow_network=False),
            dataclasses.replace(ASK, network=True),
            op.PolicyCode.NETWORK,
        ),
        (
            _open(max_vram_bytes=GIB),
            dataclasses.replace(ASK, gpu_required=True, vram_bytes=GIB + 1),
            op.PolicyCode.VRAM,
        ),
        (
            _open(max_ram_bytes=GIB),
            dataclasses.replace(ASK, ram_bytes=GIB + 1),
            op.PolicyCode.RAM,
        ),
        (
            _open(max_cpu_percent=50),
            dataclasses.replace(ASK, cpus=4.01),
            op.PolicyCode.CPU,
        ),
    ],
)
def test_every_code(
    policy: op.OwnerPolicy, ask: op.JobAsk, code: op.PolicyCode
) -> None:
    refused = op.refusal(policy, ask)
    assert refused is not None and refused.code is code and refused.text


def test_inside_every_limit_is_allowed() -> None:
    policy = _open(
        max_ram_bytes=GIB,
        max_cpu_percent=50,
        max_vram_bytes=GIB,
        allowed_users=("alice",),
        allow_unsandboxed=False,
        allow_network=False,
    )
    ask = op.JobAsk(
        owner_name="alice",
        gpu_required=True,
        vram_bytes=GIB,
        ram_bytes=GIB,
        cpus=4.0,
        network=False,
        sandboxed_runtime=True,
        node_cpu_threads=8,
    )
    assert op.refusal(policy, ask) is None
    # Unknown values are never held against the job.
    assert op.refusal(policy, dataclasses.replace(ask, node_cpu_threads=None)) is None
    assert op.refusal(policy, dataclasses.replace(ask, ram_bytes=None)) is None
    # VRAM only binds a GPU-required job.
    assert (
        op.refusal(
            policy, dataclasses.replace(ask, gpu_required=False, vram_bytes=GIB * 9)
        )
        is None
    )


def test_order_is_accepting_user_sandbox_network_vram_ram_cpu() -> None:
    everything = op.JobAsk(
        owner_name="x",
        gpu_required=True,
        vram_bytes=9 * GIB,
        ram_bytes=9 * GIB,
        cpus=99.0,
        network=True,
        sandboxed_runtime=False,
        node_cpu_threads=8,
    )
    policy = op.OwnerPolicy(
        accept_jobs=True,
        max_ram_bytes=GIB,
        max_cpu_percent=10,
        max_vram_bytes=GIB,
        allowed_users=("alice",),
    )
    seen = []
    for step in (
        {"accept_jobs": False},
        {"allowed_users": ()},
        {"allow_unsandboxed": True},
        {"allow_network": True},
        {"max_vram_bytes": None},
        {"max_ram_bytes": None},
        {"max_cpu_percent": None},
    ):
        refused = op.refusal(policy, everything)
        seen.append(None if refused is None else refused.code)
        if step != {"accept_jobs": False}:
            policy = dataclasses.replace(policy, **step)  # type: ignore[arg-type]
    refused = op.refusal(policy, everything)
    seen.append(None if refused is None else refused.code)
    assert seen == [
        op.PolicyCode.USER_NOT_ALLOWED,
        op.PolicyCode.USER_NOT_ALLOWED,
        op.PolicyCode.UNSANDBOXED,
        op.PolicyCode.NETWORK,
        op.PolicyCode.VRAM,
        op.PolicyCode.RAM,
        op.PolicyCode.CPU,
        None,
    ]


def test_allowed_cpus() -> None:
    assert op.allowed_cpus(_open(max_cpu_percent=50), 8) == 4.0
    assert op.allowed_cpus(_open(max_cpu_percent=1), 4) == 0.04
    assert op.allowed_cpus(_open(max_cpu_percent=50), None) is None
    assert op.allowed_cpus(OPEN, 8) is None


def test_codes_are_the_planner_reject_codes() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.planner.types import STATIC_CODES, RejectCode

    for code in op.PolicyCode:
        assert RejectCode(code.value) in STATIC_CODES
