# ruff: noqa: RUF001, S311, S112, PT017
# (adversarial literals / world-writable modes / seeded fuzz are the point of these tests)
"""V0.7 G5 tester round 1 (unit): attacks on item 0 and the owner-policy intake.

Derived from brief section 5e and rulings D142, D143, D158, D159, not from the
implementation:

* T-5 login names - EVERY non-ASCII code point (not a hand-picked few) must be
  an unknown user, never an alias of an ASCII name;
* N1 backoff key - IPv6 /64 spellings, IPv4-mapped IPv6, zone ids; the throttle
  table stays bounded under a flood of distinct names;
* T-4 sub-cap - exact at the boundary under thread contention, and after a
  restart of the store;
* D142 intake - random corruption of a valid policy message is either valid as
  a whole or refused (never partly trusted); oversized / hostile fields;
* D143 behaviour change - `limits.network: default` means none everywhere;
* D143 sandbox assertion gaps found by the tester (strict xfail).
"""

from __future__ import annotations

import copy
import ipaddress
import os
import random
import threading
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")

from pydantic import SecretStr

from meshforge.agent.execution.sandbox_check import (
    ExpectedMount,
    SandboxViolationError,
    check_host_config,
)
from meshforge.common import owner_policy as op
from meshforge.common.cas import MIN_CHUNK_SIZE, CasError, CasStore
from meshforge.common.errors import FailureType
from meshforge.common.hashing import sha256_bytes
from meshforge.common.manifest import LimitsSpec
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.auth.login import (
    LoginPolicy,
    LoginRefusedError,
    LoginService,
    login_key,
)
from meshforge.controller.auth.passwords import PasswordHasher
from meshforge.controller.enrollment.listener import ip_rate_key
from meshforge.controller.session.policies import PolicyIntake

pytestmark = pytest.mark.unit

CS = MIN_CHUNK_SIZE


# ------------------------------------------------------------------ T-5 sweep


def test_no_non_ascii_code_point_aliases_an_ascii_login_name() -> None:
    """Every code point above 0x7F, glued to a name, is an unknown user.

    str.strip() removes Unicode whitespace; those code points reduce to the
    plain name by design (the right password is still needed) and are listed
    separately below. Everything else must NOT resolve to an ASCII user name,
    whether it lower-cases, NFKC-normalises or case-folds into ASCII."""
    aliased: list[int] = []
    for cp in range(0x80, 0x110000):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        ch = chr(cp)
        if ch.isspace() or ch.strip() == "":
            continue
        for text in ("kevin" + ch, ch + "kevin", "kev" + ch + "in"):
            if login_key(text)[1] is not None:
                aliased.append(cp)
                break
    assert aliased == [], [hex(c) for c in aliased[:20]]


def test_lone_look_alike_letters_are_not_users() -> None:
    # Single characters whose lower()/NFKC/casefold collapse to an ASCII letter.
    hits = []
    for cp in range(0x80, 0x30000):
        ch = chr(cp)
        if ch.isspace():
            continue
        folded = {ch.lower(), unicodedata.normalize("NFKC", ch), ch.casefold()}
        if any(f and f.isascii() for f in folded) and login_key(ch)[1] is not None:
            hits.append(hex(cp))
    assert hits == []


def test_unicode_whitespace_padding_is_stripped_but_still_one_name() -> None:
    """Informational: padding by Unicode whitespace reduces to the same name AND
    the same throttle counter (so it is no way around the backoff)."""
    for pad in (" ", " ", "　", "\x0b", "\x1c"):
        assert login_key("kevin" + pad) == ("kevin", "kevin"), hex(ord(pad))


def test_a_nul_in_an_ascii_name_is_kept_apart_from_the_real_name() -> None:
    throttle, user = login_key("kevin\x00")
    # A NUL name must not alias "kevin" (the database cannot even store it).
    assert throttle != "kevin" and user != "kevin"


# ------------------------------------------------------------------ N1 sources


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("2001:db8:1:2::1", "2001:0db8:0001:0002:ffff:ffff:ffff:ffff"),
        ("2001:DB8:1:2::1", "2001:db8:1:2::1"),
        ("[2001:db8:1:2::1]", "2001:db8:1:2::2"),
        ("fe80::1%eth0", "fe80::2%wlan0"),
        ("::ffff:192.0.2.7", "192.0.2.7"),
        ("::FFFF:c000:207", "192.0.2.7"),
    ],
)
def test_source_spellings_share_a_bucket(a: str, b: str) -> None:
    assert ip_rate_key(a) == ip_rate_key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("2001:db8:1:2::1", "2001:db8:1:3::1"),
        ("192.0.2.7", "192.0.2.8"),
        ("::ffff:192.0.2.7", "::ffff:192.0.2.8"),
    ],
)
def test_distinct_sources_get_distinct_buckets(a: str, b: str) -> None:
    assert ip_rate_key(a) != ip_rate_key(b)


@pytest.mark.parametrize("junk", [None, "", "not-an-ip", "1.2.3.4:80", "999.1.1.1"])
def test_unparseable_hosts_share_the_one_unknown_bucket(junk: str | None) -> None:
    assert ip_rate_key(junk) == ip_rate_key(None)


def test_every_address_of_a_slash_64_maps_to_one_bucket() -> None:
    rng = random.Random(7)
    base = int(ipaddress.IPv6Address("2001:db8:aaaa:bbbb::"))
    keys = {
        ip_rate_key(str(ipaddress.IPv6Address(base | rng.getrandbits(64))))
        for _ in range(500)
    }
    assert len(keys) == 1


class _Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Store:
    def __init__(self) -> None:
        self.lookups = 0

    async def login_record(self, name: str) -> None:
        self.lookups += 1

    async def record_login_failure(self, **kw: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_a_flood_of_distinct_names_keeps_the_throttle_table_bounded() -> None:
    policy = LoginPolicy(
        rate_per_minute=1e9, rate_burst=10**9, free_failures=1, max_tracked=32
    )
    service = LoginService(
        _Store(),  # type: ignore[arg-type]
        policy=policy,
        hasher=PasswordHasher(),
        clock=_Clock(),
    )
    for i in range(60):
        # non-ASCII (own counter) and ASCII-unknown names, rotating sources
        for name in (f"K{i}", f"nobody{i}"):
            with pytest.raises(LoginRefusedError):
                await service.login(
                    name,
                    SecretStr("x"),
                    host=f"10.{i % 250}.{i // 250}.1",
                    source=None,
                    token_name="t",
                )
    assert len(service.throttle._names) <= 32


# ------------------------------------------------------------------ T-4 sub-cap


def _blob(i: int) -> tuple[Any, bytes]:
    data = os.urandom(2 * CS) + i.to_bytes(4, "big")
    return sha256_bytes(data), data


def test_the_sub_cap_is_exact_under_thread_contention(tmp_path: Path) -> None:
    store = CasStore(
        tmp_path / "cas",
        max_blob_bytes=1 << 30,
        max_resumable=64,
        max_resumable_per_owner=16,
    )
    store.prepare()
    blobs = [_blob(i) for i in range(48)]
    start = threading.Barrier(48)
    outcome: list[tuple[str, str]] = []
    lock = threading.Lock()

    def open_one(owner: str, index: int) -> None:
        digest, data = blobs[index % len(blobs)]
        start.wait()
        try:
            store.resumable(digest, len(data), CS, owner=owner)
            result = "ok"
        except CasError as exc:
            assert exc.failure_type is FailureType.RESOURCE_BUSY, exc
            result = "busy"
        with lock:
            outcome.append((owner, result))

    threads = [
        threading.Thread(target=open_one, args=("user:bob", i)) for i in range(40)
    ] + [threading.Thread(target=open_one, args=("user:amy", i)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    bob_ok = sum(1 for o, r in outcome if o == "user:bob" and r == "ok")
    amy_ok = sum(1 for o, r in outcome if o == "user:amy" and r == "ok")
    assert bob_ok == 16, outcome  # exactly the sub-cap, never 17
    assert amy_ok == 8  # another owner is not starved by bob
    assert store.resumes_of("user:bob") == 16
    assert store.resumes_in_progress() == 24


def test_the_sub_cap_survives_a_restart_of_the_store(tmp_path: Path) -> None:
    first = CasStore(
        tmp_path / "cas", max_blob_bytes=1 << 30, max_resumable_per_owner=3
    )
    first.prepare()
    blobs = [_blob(i) for i in range(5)]
    for digest, data in blobs[:3]:
        first.resumable(digest, len(data), CS, owner="user:bob")
    reopened = CasStore(
        tmp_path / "cas", max_blob_bytes=1 << 30, max_resumable_per_owner=3
    )
    reopened.prepare()
    assert reopened.resumes_of("user:bob") == 3
    digest, data = blobs[3]
    with pytest.raises(CasError) as info:
        reopened.resumable(digest, len(data), CS, owner="user:bob")
    assert info.value.failure_type is FailureType.RESOURCE_BUSY


# ------------------------------------------------------------------ D142 intake


def _live() -> Any:
    return SimpleNamespace(
        node_id="node_x",
        session_id="s",
        policy=None,
        policy_refused=None,
        policy_slots=None,
    )


def _valid() -> agent_pb2.OwnerPolicy:
    return op.to_proto(
        op.OwnerPolicy(
            accept_jobs=True,
            allow_unsandboxed=True,
            max_ram_bytes=2 << 30,
            max_cpu_percent=50,
            max_vram_bytes=1 << 30,
            allowed_users=("alice", "bob"),
            not_enforced=("max_vram_bytes:no_runtime_limit",),
            revision=3,
        )
    )


def test_random_corruption_is_valid_as_a_whole_or_refused() -> None:
    """Flip bytes of a valid wire policy 5000 times: the intake never raises
    anything but a typed refusal, and an accepted result always re-validates."""
    rng = random.Random(1234)
    wire = _valid().SerializeToString()
    accepted = refused = 0
    for _ in range(5000):
        data = bytearray(wire)
        for _ in range(rng.randint(1, 4)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        msg = agent_pb2.OwnerPolicy()
        try:
            msg.ParseFromString(bytes(data))
        except Exception:  # noqa: BLE001 - not a protobuf at all: the gRPC layer drops it
            continue
        try:
            policy = op.from_proto(msg)
        except op.PolicyError:
            refused += 1
            live = _live()
            PolicyIntake().on_update(live, agent_pb2.PolicyUpdate(policy=msg))
            assert live.policy == op.DENY_ALL and live.policy_slots == (0, 0)
            continue
        accepted += 1
        assert op.validate(policy) is policy
    assert refused > 100 and accepted > 100  # the corpus exercised both outcomes


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.allowed_users.append("A" * 64),
        lambda m: m.allowed_users.append("ali\x00ce"),
        lambda m: m.allowed_users.append("аlice"),  # Cyrillic a
        lambda m: m.allowed_users.append("alice"),  # duplicate
        lambda m: m.allowed_users.extend(f"u{i}" for i in range(64)),
        lambda m: m.not_enforced.append("x" * 9000),
        lambda m: m.not_enforced.extend(["a_b:c"] * 17),
        lambda m: setattr(m, "max_cpu_percent", 0),
        lambda m: setattr(m, "max_cpu_percent", 10**9),
        lambda m: setattr(m, "max_ram_bytes", 2**64 - 1),
        lambda m: setattr(m, "max_ram_bytes", 1),
        lambda m: setattr(m, "max_vram_bytes", 0),
        lambda m: setattr(m, "revision", 2**63),
        lambda m: setattr(m, "revision", 2**64 - 1),
    ],
)
def test_a_hostile_field_refuses_the_whole_policy(mutate: Any) -> None:
    msg = copy.deepcopy(_valid())
    mutate(msg)
    live = _live()
    intake = PolicyIntake()
    intake.on_update(live, agent_pb2.PolicyUpdate(policy=msg))
    assert live.policy == op.DENY_ALL, "a malformed policy was partly trusted"
    assert live.policy_refused
    hello_live = _live()
    intake.on_hello(hello_live, agent_pb2.Hello(policy=msg))
    assert hello_live.policy == op.DENY_ALL and hello_live.policy_refused


def test_a_padded_policy_message_is_refused_or_ignored_never_widened() -> None:
    """Unknown fields appended to the message must not change what is accepted."""
    msg = _valid()
    padded = agent_pb2.OwnerPolicy()
    padded.ParseFromString(
        msg.SerializeToString() + b"\x9a\x06" + b"\xa0\x9c\x01" + b"A" * 20_000
    )
    live = _live()
    PolicyIntake().on_update(live, agent_pb2.PolicyUpdate(policy=padded))
    assert live.policy in (op.DENY_ALL, op.from_proto(msg))
    if live.policy != op.DENY_ALL:
        assert live.policy.allowed_users == ("alice", "bob")


def test_an_update_without_a_policy_or_with_absurd_slots_fails_closed() -> None:
    intake = PolicyIntake()
    live = _live()
    intake.on_update(live, agent_pb2.PolicyUpdate())
    assert live.policy == op.DENY_ALL
    live = _live()
    intake.on_update(
        live,
        agent_pb2.PolicyUpdate(policy=_valid(), max_cpu_jobs=10**6, max_gpu_jobs=0),
    )
    assert live.policy == op.DENY_ALL and live.policy_slots == (0, 0)


def test_a_bad_update_after_a_good_one_closes_the_node() -> None:
    intake = PolicyIntake()
    live = _live()
    intake.on_update(live, agent_pb2.PolicyUpdate(policy=_valid(), max_cpu_jobs=1))
    assert live.policy.accept_jobs and live.policy.allowed_users == ("alice", "bob")
    bad = _valid()
    bad.max_cpu_percent = 500
    intake.on_update(live, agent_pb2.PolicyUpdate(policy=bad, max_cpu_jobs=1))
    assert live.policy == op.DENY_ALL and live.policy_slots == (0, 0)
    intake.on_update(live, agent_pb2.PolicyUpdate(policy=_valid(), max_cpu_jobs=1))
    assert live.policy.accept_jobs  # a valid one recovers it


# ------------------------------------------------------------------ D143 behaviour change


def test_limits_network_default_means_none_and_only_on_wants_a_network() -> None:
    assert LimitsSpec().network == "default"
    assert LimitsSpec().wants_network is False
    assert LimitsSpec(network="none").wants_network is False
    assert LimitsSpec(network="on").wants_network is True
    with pytest.raises(ValueError):
        LimitsSpec(network="true")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        LimitsSpec(network="ON")  # type: ignore[arg-type]


# ------------------------------------------------------------------ D143 sandbox assertion gaps

WORK = "/home/me/.local/share/meshforge/work/att_x"
GOOD: dict[str, Any] = {
    "Binds": None,
    "NetworkMode": "none",
    "CapAdd": None,
    "CapDrop": ["ALL"],
    "IpcMode": "private",
    "PidMode": "",
    "Privileged": False,
    "ReadonlyRootfs": True,
    "SecurityOpt": ["no-new-privileges:true"],
    "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=512m"},
    "UTSMode": "",
    "UsernsMode": "",
    "Memory": 67108864,
    "PidsLimit": 64,
    "Devices": [],
    "Mounts": [{"Type": "bind", "Source": WORK, "Target": "/work"}],
    "MaskedPaths": ["/proc/acpi", "/proc/kcore"],
    "ReadonlyPaths": ["/proc/bus", "/proc/sys", "/proc/sysrq-trigger"],
    "VolumesFrom": None,
}


def _must_refuse(host: dict[str, Any], *, network: bool) -> None:
    with pytest.raises(SandboxViolationError):
        check_host_config(
            host,
            role="run",
            mounts=[ExpectedMount(WORK, "/work", False)],
            network_allowed=network,
        )


def test_a_custom_allow_all_seccomp_profile_is_refused() -> None:
    host = copy.deepcopy(GOOD)
    host["SecurityOpt"] = [
        "no-new-privileges:true",
        'seccomp={"defaultAction":"SCMP_ACT_ALLOW"}',
    ]
    _must_refuse(host, network=False)


def test_host_network_mode_is_refused_even_when_a_network_is_allowed() -> None:
    host = copy.deepcopy(GOOD)
    host["NetworkMode"] = "host"
    _must_refuse(host, network=True)


@pytest.mark.parametrize("key", ["PidMode", "IpcMode"])
def test_joining_another_containers_namespace_is_refused(key: str) -> None:
    host = copy.deepcopy(GOOD)
    host[key] = "container:deadbeef"
    _must_refuse(host, network=False)


# Fields the container is created with (docker_runtime._create_kwargs) or that
# widen the box, which the create-time assertion does not read at all.
_UNCHECKED = [
    pytest.param({"MemorySwap": -1}, id="memory_swap_unlimited"),
    pytest.param({"Memory": 1 << 50}, id="memory_limit_absurd"),
    pytest.param({"CgroupnsMode": "host"}, id="cgroupns_host"),
    pytest.param({"Sysctls": {"net.ipv4.ip_forward": "1"}}, id="sysctls"),
    pytest.param({"Tmpfs": {"/tmp": "rw,exec,size=900g"}}, id="tmpfs_absurd_size"),
    pytest.param(
        {"Tmpfs": {"/tmp": "rw,size=16m", "/mnt/x": "rw,exec"}}, id="extra_tmpfs"
    ),
    pytest.param({"OomKillDisable": True}, id="oom_kill_disabled"),
    pytest.param({"PortBindings": {"80/tcp": [{"HostPort": "8080"}]}}, id="ports"),
    pytest.param({"GroupAdd": ["0"]}, id="group_add_root"),
]


@pytest.mark.security
@pytest.mark.parametrize("change", _UNCHECKED)
def test_a_tampered_field_the_assertion_does_not_read_is_refused(
    change: dict[str, Any],
) -> None:
    host = copy.deepcopy(GOOD)
    host.update(change)
    _must_refuse(host, network=False)


# ------------------------------------------------------------------ T-4 boundaries


def test_sub_cap_boundary_n_reopen_n_plus_1_and_release(tmp_path: Path) -> None:
    store = CasStore(
        tmp_path / "cas",
        max_blob_bytes=1 << 30,
        max_resumable=64,
        max_resumable_per_owner=3,
    )
    store.prepare()
    blobs = [_blob(i) for i in range(6)]
    parts = [store.resumable(d, len(b), CS, owner="user:bob") for d, b in blobs[:3]]
    # N: re-opening one the owner already has is a continue, not a new slot
    again = store.resumable(blobs[0][0], len(blobs[0][1]), CS, owner="user:bob")
    assert again.digest == blobs[0][0]
    assert store.resumes_of("user:bob") == 3
    # N+1 distinct: busy, and nothing was created for it
    with pytest.raises(CasError) as info:
        store.resumable(blobs[3][0], len(blobs[3][1]), CS, owner="user:bob")
    assert info.value.failure_type is FailureType.RESOURCE_BUSY
    assert store.resumes_of("user:bob") == 3
    # another owner is unaffected
    store.resumable(blobs[3][0], len(blobs[3][1]), CS, owner="user:amy")
    # a different owner string with the same digest never shares bob's count
    assert store.resumes_of("user:amy") == 1
    # releasing one frees exactly one slot
    parts[0].discard()
    store.resumable(blobs[4][0], len(blobs[4][1]), CS, owner="user:bob")
    assert store.resumes_of("user:bob") == 3


def test_owner_none_only_meets_the_global_cap(tmp_path: Path) -> None:
    store = CasStore(
        tmp_path / "cas",
        max_blob_bytes=1 << 30,
        max_resumable=6,
        max_resumable_per_owner=2,
    )
    store.prepare()
    blobs = [_blob(i) for i in range(8)]
    for d, b in blobs[:6]:
        store.resumable(d, len(b), CS, owner=None)  # 6 > per-owner 2: fine
    with pytest.raises(CasError) as info:
        store.resumable(blobs[6][0], len(blobs[6][1]), CS, owner=None)
    assert info.value.failure_type is FailureType.RESOURCE_BUSY
    with pytest.raises(CasError):  # the global cap still binds named owners
        store.resumable(blobs[7][0], len(blobs[7][1]), CS, owner="user:amy")


# ------------------------------------------------------------------ D145b window


@pytest.mark.parametrize(
    "owner", ["", "node_other", 5, ["node_x"], {"id": "node_x"}, False, 0]
)
def test_a_score_document_bound_to_anything_but_this_node_is_refused(
    owner: Any,
) -> None:
    from meshforge.controller.planner.inputs import bound_to

    assert bound_to({"node_id": owner}, "node_x") is False


def test_only_an_absent_or_null_binding_is_the_pre_g5_window() -> None:
    from meshforge.controller.planner.inputs import bound_to

    assert bound_to({}, "node_x") is True
    assert bound_to({"node_id": None}, "node_x") is True
    assert bound_to({"node_id": "node_x"}, "node_x") is True
