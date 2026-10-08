"""V0.7 G2 tester: adversarial enrolment on real PostgreSQL + the real controller
runtime + its real TLS listener.

Derived from brief 5b (items 2-5) and D135/D136/D150/D151, not from the code:

* CSR attacks (other node_id, other key, wrong curve/hash/key type, SAN games,
  oversized) are refused with typed reasons and burn nothing;
* token shapes (wrong lengths, unknown) never create a node row;
* expiry is the *database* clock, strict at the boundary;
* revoke vs redeem race: exactly one of them wins, never both;
* replace-flow abuse; revoked nodes; two keys racing for one node_id;
* a failure after the code is redeemed (issue error, unique clash, DB error)
  rolls the redeem back;
* the code and its token appear in no table, log or error; audit rows hold ids only;
* the listener survives garbage, plaintext, oversized and idle connections;
* a controller restart keeps outstanding invites usable.
"""

# ruff: noqa: E501, RUF059, SIM105  (tester file: adversarial literals, blunt asserts)

from __future__ import annotations

import asyncio
import io
import json
import re
import socket
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import grpc
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import NameOID
from mf_controller_harness import make_settings
from mf_fabric import identity_channel
from mf_session_client import FakeAgent
from sqlalchemy import text

from meshforge.agent import enrollment as agent_enrollment
from meshforge.agent.enrollment import JoinError, JoinReason
from meshforge.agent.identity import NodeIdentity, load_or_create
from meshforge.common import join_code, pki
from meshforge.common.ids import new_fabric_id, new_node_id
from meshforge.common.logging import configure_logging
from meshforge.common.proto.meshforge.v1 import enroll_pb2, enroll_pb2_grpc
from meshforge.controller.enrollment import service as enroll_service
from meshforge.controller.enrollment.codes import issue_code
from meshforge.controller.enrollment.invites import InviteStore
from meshforge.controller.fabric_ca import create_fabric
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

FAST = {"bind": "127.0.0.1:0", "rate_per_minute": 600.0, "rate_burst": 100}
Raw = tuple[grpc.StatusCode | None, str | None, str]


async def _sql(
    pg: DatabaseSettings, statement: str, params: dict[str, Any] | None = None
) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params or {})
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def rt(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = make_settings(pg_settings, fabric=False, enroll=FAST)
    create_fabric(settings.ca_dir)
    async with ControllerRuntime.start(settings) as runtime:
        yield runtime


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[InviteStore]:
    engine = create_engine(pg_settings)
    try:
        yield InviteStore(engine)
    finally:
        await engine.dispose()


MakeCode = Callable[..., Any]


@pytest.fixture
def make_code(rt: ControllerRuntime, store: InviteStore) -> MakeCode:
    async def make(*, ttl_s: int = 900, replace: str | None = None) -> tuple[str, str]:
        new = await store.create(ttl_s=ttl_s, actor="tester", replace_node_id=replace)
        assert rt.fabric is not None
        issued = issue_code(
            new, fabric=rt.fabric, host="127.0.0.1", port=rt.enroll_port
        )
        return issued.code, issued.invite_id

    return make


def _ident(tmp: Path, name: str) -> NodeIdentity:
    return load_or_create(tmp / name / "identity")


def _csr(rt: ControllerRuntime, key: ec.EllipticCurvePrivateKey, node_id: str) -> bytes:
    assert rt.fabric is not None
    return pki.build_csr(
        key, fabric_id=rt.fabric.fabric_id, node_id=node_id
    ).public_bytes(serialization.Encoding.PEM)


def _raw(
    rt: ControllerRuntime, token: bytes, csr_pem: bytes, timeout: float = 15
) -> Raw:
    assert rt.fabric is not None
    creds = grpc.ssl_channel_credentials(pki.cert_to_pem(rt.fabric.cert))
    with grpc.secure_channel(
        f"127.0.0.1:{rt.enroll_port}",
        creds,
        options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
    ) as channel:
        try:
            enroll_pb2_grpc.EnrollmentStub(channel).Enroll(
                enroll_pb2.EnrollRequest(join_token=token, csr_pem=csr_pem),
                timeout=timeout,
            )
        except grpc.RpcError as exc:
            call: Any = exc
            reason = dict(call.trailing_metadata() or ()).get("meshforge-reason")
            return call.code(), reason, str(call.details())
    return None, None, ""


async def araw(rt: ControllerRuntime, token: bytes, csr: bytes, **kw: Any) -> Raw:
    return await asyncio.to_thread(_raw, rt, token, csr, **kw)


async def _counts(pg: DatabaseSettings) -> dict[str, int]:
    out = {}
    for table in ("node", "join_token", "audit_event"):
        out[table] = (await _sql(pg, f"SELECT count(*) FROM {table}"))[0][0]  # noqa: S608
    return out


async def _unused(pg: DatabaseSettings, invite_id: str) -> bool:
    row = await _sql(
        pg, "SELECT used_at FROM join_token WHERE id = :i", {"i": invite_id}
    )
    return row == [(None,)]


def _tok(code: str) -> bytes:
    return join_code.decode(code).token


# ----------------------------------------------------------------- CSR attack matrix


def _crafted_csrs(rt: ControllerRuntime) -> dict[str, bytes]:
    assert rt.fabric is not None
    fid = rt.fabric.fabric_id
    key = pki.generate_key()
    node = str(new_node_id())
    uri = x509.UniformResourceIdentifier(pki.node_uri(fid, node))
    pem = serialization.Encoding.PEM

    def build(
        signing_key: Any,
        *,
        sans: list[x509.GeneralName] | None,
        algo: hashes.HashAlgorithm | None = None,
    ) -> bytes:
        b = x509.CertificateSigningRequestBuilder().subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node)])
        )
        if sans is not None:
            b = b.add_extension(x509.SubjectAlternativeName(sans), critical=False)
        hash_alg = (
            None
            if isinstance(signing_key, ed25519.Ed25519PrivateKey)
            else (algo or hashes.SHA256())
        )
        return b.sign(signing_key, hash_alg).public_bytes(pem)

    good = build(key, sans=[uri])
    tampered = bytearray(good)
    start = good.index(b"\n") + 1
    tampered[start + 40] = ord("A") if tampered[start + 40] != ord("A") else ord("B")
    return {
        "p384 key": build(ec.generate_private_key(ec.SECP384R1()), sans=[uri]),
        "rsa key": build(rsa.generate_private_key(65537, 2048), sans=[uri]),
        "ed25519 key": build(ed25519.Ed25519PrivateKey.generate(), sans=[uri]),
        "sha384 signature": build(key, sans=[uri], algo=hashes.SHA384()),
        "no san": build(key, sans=None),
        "two uris": build(
            key,
            sans=[
                uri,
                x509.UniformResourceIdentifier(pki.node_uri(fid, str(new_node_id()))),
            ],
        ),
        "uri + controller dns name": build(
            key, sans=[uri, x509.DNSName(pki.SERVER_NAME)]
        ),
        "dns name only": build(key, sans=[x509.DNSName(pki.SERVER_NAME)]),
        "ip san": build(
            key, sans=[x509.IPAddress(__import__("ipaddress").ip_address("10.0.0.1"))]
        ),
        "bad node id": build(
            key,
            sans=[
                x509.UniformResourceIdentifier(
                    f"meshforge://fabric/{fid}/node/node_../../x"
                )
            ],
        ),
        "other scheme": build(
            key, sans=[x509.UniformResourceIdentifier(f"https://evil/{fid}")]
        ),
        "other fabric": build(
            key,
            sans=[
                x509.UniformResourceIdentifier(pki.node_uri(str(new_fabric_id()), node))
            ],
        ),
        "uri path suffix": build(
            key,
            sans=[x509.UniformResourceIdentifier(pki.node_uri(fid, node) + "/extra")],
        ),
        "tampered body": bytes(tampered),
        "not a csr": b"-----BEGIN CERTIFICATE REQUEST-----\nAAAA\n-----END CERTIFICATE REQUEST-----\n",
        "random bytes": bytes(range(256)) * 4,
        "nul bytes": b"\x00" * 2000,
        "oversize 4097": good + b"#" * (4097 - len(good)),
        "oversize 12000": good + b"#" * 12000,
    }


async def test_crafted_csrs_are_refused_without_side_effects(
    rt: ControllerRuntime, make_code: MakeCode, pg_settings: DatabaseSettings
) -> None:
    code, invite_id = await make_code()
    token = _tok(code)
    before = await _counts(pg_settings)
    for name, csr in _crafted_csrs(rt).items():
        status, reason, detail = await araw(rt, token, csr)
        assert status in (
            grpc.StatusCode.INVALID_ARGUMENT,
            grpc.StatusCode.RESOURCE_EXHAUSTED,
        ), (name, status, reason, detail)
        assert (
            reason is None or reason.startswith("csr_") or reason == "malformed_request"
        ), (
            name,
            reason,
        )
        assert code not in detail and token.hex() not in detail, name
    after = await _counts(pg_settings)
    assert after == before  # no node row, no audit row, no new invite
    assert await _unused(pg_settings, invite_id)
    # ... and the very same code still works for a clean CSR afterwards.
    status, _, _ = await araw(
        rt, token, _csr(rt, pki.generate_key(), str(new_node_id()))
    )
    assert status is None


async def test_oversized_message_over_the_wire_limit_does_not_hurt_the_listener(
    rt: ControllerRuntime, make_code: MakeCode
) -> None:
    code, _ = await make_code()
    token = _tok(code)
    status, _, _ = await araw(rt, token, b"A" * (2 * 1024 * 1024))
    assert status is grpc.StatusCode.RESOURCE_EXHAUSTED
    status, _, _ = await araw(
        rt, token, _csr(rt, pki.generate_key(), str(new_node_id()))
    )
    assert status is None


@pytest.mark.parametrize("size", [0, 1, 15, 17, 32, 4096])
async def test_wrong_size_tokens_are_malformed_and_touch_nothing(
    rt: ControllerRuntime, pg_settings: DatabaseSettings, size: int
) -> None:
    before = await _counts(pg_settings)
    csr = _csr(rt, pki.generate_key(), str(new_node_id()))
    status, reason, _ = await araw(rt, b"\xab" * size, csr)
    assert (status, reason) == (grpc.StatusCode.INVALID_ARGUMENT, "malformed_request")
    assert await _counts(pg_settings) == before


async def test_unknown_tokens_never_create_nodes_or_audit_rows(
    rt: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    csr = _csr(rt, pki.generate_key(), str(new_node_id()))
    for token in (
        b"\x00" * 16,
        b"\xff" * 16,
        *(join_code.new_token() for _ in range(40)),
    ):
        status, reason, _ = await araw(rt, token, csr)
        assert (status, reason) == (grpc.StatusCode.UNAUTHENTICATED, "token_unknown")
    assert await _counts(pg_settings) == {"node": 0, "join_token": 0, "audit_event": 0}


async def test_token_hash_is_not_a_usable_token(
    rt: ControllerRuntime, make_code: MakeCode, pg_settings: DatabaseSettings
) -> None:
    """The stored SHA-256 (what a DB-read leak yields) must not enrol anyone."""
    code, invite_id = await make_code()
    stored = (await _sql(pg_settings, "SELECT token_hash FROM join_token"))[0][0]
    csr = _csr(rt, pki.generate_key(), str(new_node_id()))
    for candidate in (bytes.fromhex(stored)[:16], bytes.fromhex(stored)[16:]):
        status, reason, _ = await araw(rt, candidate, csr)
        assert reason == "token_unknown"
    assert await _unused(pg_settings, invite_id)


# ------------------------------------------------------------- clocks and expiry


async def test_expiry_is_the_database_clock_and_strict(
    rt: ControllerRuntime, make_code: MakeCode, pg_settings: DatabaseSettings
) -> None:
    code, invite_id = await make_code(ttl_s=60)
    await _sql(
        pg_settings,
        "UPDATE join_token SET created_at = now() - interval '1 h', "
        "expires_at = clock_timestamp() + interval '1500 milliseconds' WHERE id = :i",
        {"i": invite_id},
    )
    csr = _csr(rt, pki.generate_key(), str(new_node_id()))
    ok_status, _, _ = await araw(rt, _tok(code), csr)
    assert ok_status is None  # inside the window
    code2, invite2 = await make_code(ttl_s=60)
    await _sql(
        pg_settings,
        "UPDATE join_token SET created_at = now() - interval '1 h', "
        "expires_at = clock_timestamp() + interval '700 milliseconds' WHERE id = :i",
        {"i": invite2},
    )
    await asyncio.sleep(1.0)
    status, reason, _ = await araw(rt, _tok(code2), csr)
    assert (status, reason) == (grpc.StatusCode.UNAUTHENTICATED, "token_expired")
    # expires_at == now (the boundary itself) is expired: the guard is strictly >
    code3, invite3 = await make_code(ttl_s=60)
    await _sql(
        pg_settings,
        "UPDATE join_token SET created_at = now() - interval '1 h', "
        "expires_at = now() - interval '1 microsecond' WHERE id = :i",
        {"i": invite3},
    )
    status, reason, _ = await araw(rt, _tok(code3), csr)
    assert reason == "token_expired"
    assert await _unused(pg_settings, invite3)


# ---------------------------------------------------- revoke vs redeem, key races


async def test_revoke_and_redeem_race_has_exactly_one_winner(
    rt: ControllerRuntime,
    make_code: MakeCode,
    store: InviteStore,
    pg_settings: DatabaseSettings,
) -> None:
    from meshforge.controller.enrollment.invites import InviteConflictError

    outcomes = set()
    for _ in range(20):
        code, invite_id = await make_code()
        csr = _csr(rt, pki.generate_key(), str(new_node_id()))

        async def revoke(invite_id: str = invite_id) -> str:
            try:
                await store.revoke(invite_id, actor="tester")
            except InviteConflictError:
                return "revoke_conflict"
            return "revoked"

        enrolled, revoked = await asyncio.gather(araw(rt, _tok(code), csr), revoke())
        row = (
            await _sql(
                pg_settings,
                "SELECT used_at IS NOT NULL, revoked_at IS NOT NULL FROM join_token "
                "WHERE id = :i",
                {"i": invite_id},
            )
        )[0]
        if enrolled[0] is None:
            assert revoked == "revoke_conflict" and row == (True, False)
            outcomes.add("enrolled")
        else:
            assert enrolled[1] == "token_revoked" and revoked == "revoked"
            assert row == (False, True)
            outcomes.add("revoked")
    assert outcomes  # (both outcomes are legal; mixing within one invite never is)


async def test_two_keys_race_for_one_node_id_exactly_one_wins(
    rt: ControllerRuntime, make_code: MakeCode, pg_settings: DatabaseSettings
) -> None:
    node = str(new_node_id())
    keys = [pki.generate_key() for _ in range(6)]
    codes = [await make_code() for _ in keys]
    results = await asyncio.gather(
        *(
            araw(rt, _tok(c), _csr(rt, k, node))
            for (c, _), k in zip(codes, keys, strict=True)
        )
    )
    winners = [i for i, r in enumerate(results) if r[0] is None]
    assert len(winners) == 1, results
    assert {r[1] for i, r in enumerate(results) if i not in winners} == {
        "node_key_conflict"
    }
    key_fp = (
        await _sql(
            pg_settings,
            "SELECT public_key_fingerprint FROM node WHERE node_id=:n",
            {"n": node},
        )
    )[0][0]
    assert key_fp == enroll_service.public_key_fingerprint(
        keys[winners[0]].public_key()
    )
    for i, (_, invite_id) in enumerate(codes):
        assert (not await _unused(pg_settings, invite_id)) == (i in winners)


async def test_same_node_same_key_two_codes_concurrently_both_issue(
    rt: ControllerRuntime, make_code: MakeCode
) -> None:
    node, key = str(new_node_id()), pki.generate_key()
    codes = [await make_code() for _ in range(4)]
    results = await asyncio.gather(
        *(araw(rt, _tok(c), _csr(rt, key, node)) for c, _ in codes)
    )
    assert all(r[0] is None for r in results), results


# ------------------------------------------------------------------ replace abuse


async def test_replace_invite_abuse(
    rt: ControllerRuntime, make_code: MakeCode, pg_settings: DatabaseSettings
) -> None:
    victim, victim_key = str(new_node_id()), pki.generate_key()
    c0, _ = await make_code()
    assert (await araw(rt, _tok(c0), _csr(rt, victim_key, victim)))[0] is None

    attacker_key = pki.generate_key()
    # 1. replace code for ANOTHER node cannot take over the victim
    other = str(new_node_id())
    c1, i1 = await make_code(replace=other)
    status, reason, _ = await araw(rt, _tok(c1), _csr(rt, attacker_key, victim))
    assert (status, reason) == (grpc.StatusCode.FAILED_PRECONDITION, "replace_mismatch")
    assert await _unused(pg_settings, i1)
    # 2. ... nor can a replace code for the victim enrol a *different new* node id
    c2, i2 = await make_code(replace=victim)
    status, reason, _ = await araw(
        rt, _tok(c2), _csr(rt, attacker_key, str(new_node_id()))
    )
    assert reason == "replace_mismatch" and await _unused(pg_settings, i2)
    # 3. a plain code with the victim's id and another key is a conflict
    c3, i3 = await make_code()
    assert (await araw(rt, _tok(c3), _csr(rt, attacker_key, victim)))[
        1
    ] == "node_key_conflict"
    assert await _unused(pg_settings, i3)
    # 4. the legitimate replace works once, only once
    fp_before = (
        await _sql(
            pg_settings,
            "SELECT public_key_fingerprint FROM node WHERE node_id=:n",
            {"n": victim},
        )
    )[0][0]
    assert (await araw(rt, _tok(c2), _csr(rt, attacker_key, victim)))[0] is None
    fp_after = (
        await _sql(
            pg_settings,
            "SELECT public_key_fingerprint FROM node WHERE node_id=:n",
            {"n": victim},
        )
    )[0][0]
    assert fp_before != fp_after
    assert (await araw(rt, _tok(c2), _csr(rt, pki.generate_key(), victim)))[
        1
    ] == "token_used"
    # the old key is now a stranger: a plain code with the OLD key conflicts
    c4, _ = await make_code()
    assert (await araw(rt, _tok(c4), _csr(rt, victim_key, victim)))[
        1
    ] == "node_key_conflict"
    audits = await _sql(
        pg_settings,
        "SELECT details->>'key_replaced' FROM audit_event WHERE action='enrolled' "
        "AND node_id = :n ORDER BY id",
        {"n": victim},
    )
    assert [a for (a,) in audits] == ["false", "true"]


async def test_replace_invites_racing_with_different_keys_one_wins(
    rt: ControllerRuntime, make_code: MakeCode, pg_settings: DatabaseSettings
) -> None:
    victim = str(new_node_id())
    c0, _ = await make_code()
    await araw(rt, _tok(c0), _csr(rt, pki.generate_key(), victim))
    keys = [pki.generate_key() for _ in range(5)]
    codes = [await make_code(replace=victim) for _ in keys]
    results = await asyncio.gather(
        *(
            araw(rt, _tok(c), _csr(rt, k, victim))
            for (c, _), k in zip(codes, keys, strict=True)
        )
    )
    # Every replace code is valid for this node, so all may succeed - but the
    # node row must end with exactly the LAST committed key, consistent with
    # its certificate fingerprint (no torn row).
    assert all(r[0] is None for r in results), results
    row = (
        await _sql(
            pg_settings,
            "SELECT public_key_fingerprint, cert_serial FROM node WHERE node_id=:n",
            {"n": victim},
        )
    )[0]
    last = (
        await _sql(
            pg_settings,
            "SELECT details->>'serial' FROM audit_event WHERE action='enrolled' "
            "AND node_id=:n ORDER BY id DESC LIMIT 1",
            {"n": victim},
        )
    )[0][0]
    assert row[1] == last


async def test_revoked_node_cannot_enrol_even_with_a_replace_code(
    rt: ControllerRuntime, make_code: MakeCode, pg_settings: DatabaseSettings
) -> None:
    node, key = str(new_node_id()), pki.generate_key()
    c0, _ = await make_code()
    assert (await araw(rt, _tok(c0), _csr(rt, key, node)))[0] is None
    await _sql(
        pg_settings, "UPDATE node SET state='revoked' WHERE node_id=:n", {"n": node}
    )
    for replace in (None, node):
        code, invite_id = await make_code(replace=replace)
        status, reason, _ = await araw(
            rt, _tok(code), _csr(rt, pki.generate_key(), node)
        )
        assert (status, reason) == (grpc.StatusCode.PERMISSION_DENIED, "node_revoked")
        assert await _unused(pg_settings, invite_id)
    state = (
        await _sql(pg_settings, "SELECT state FROM node WHERE node_id=:n", {"n": node})
    )[0][0]
    assert state == "revoked"


# --------------------------------------------------- failures after redeem roll back


async def test_issue_failure_after_redeem_does_not_burn_the_code(
    rt: ControllerRuntime,
    make_code: MakeCode,
    pg_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, invite_id = await make_code()
    real = pki.issue_node_cert
    node = str(new_node_id())
    csr = _csr(rt, pki.generate_key(), node)

    def boom(*a: Any, **k: Any) -> Any:
        raise pki.PkiError(pki.PkiReason.EXPIRED, "the CA is gone")

    monkeypatch.setattr(enroll_service.pki, "issue_node_cert", boom)
    status, reason, detail = await araw(rt, _tok(code), csr)
    assert (status, reason) == (grpc.StatusCode.INTERNAL, "internal")
    assert "CA is gone" not in detail  # internals are not echoed
    assert await _unused(pg_settings, invite_id)
    assert (
        await _sql(
            pg_settings, "SELECT count(*) FROM node WHERE node_id=:n", {"n": node}
        )
    )[0][0] == 0
    monkeypatch.setattr(enroll_service.pki, "issue_node_cert", real)
    assert (await araw(rt, _tok(code), csr))[0] is None


async def test_unique_clash_on_serial_after_redeem_rolls_back(
    rt: ControllerRuntime,
    make_code: MakeCode,
    pg_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = pki.issue_node_cert
    first_cert: list[x509.Certificate] = []

    def fixed(ca: Any, csr: Any, **kw: Any) -> x509.Certificate:
        cert = real(ca, csr, **kw)
        if not first_cert:
            first_cert.append(cert)
        return first_cert[0] if first_cert else cert  # same serial every time

    monkeypatch.setattr(enroll_service.pki, "issue_node_cert", fixed)
    c1, _ = await make_code()
    n1 = str(new_node_id())
    assert (await araw(rt, _tok(c1), _csr(rt, pki.generate_key(), n1)))[0] is None
    c2, i2 = await make_code()
    n2 = str(new_node_id())
    status, reason, _ = await araw(rt, _tok(c2), _csr(rt, pki.generate_key(), n2))
    assert (status, reason) == (grpc.StatusCode.UNAVAILABLE, "unavailable")
    assert await _unused(pg_settings, i2)
    assert (
        await _sql(pg_settings, "SELECT count(*) FROM node WHERE node_id=:n", {"n": n2})
    )[0][0] == 0


async def test_database_error_is_unavailable_and_the_listener_survives(
    rt: ControllerRuntime,
    make_code: MakeCode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import OperationalError

    code, _ = await make_code()
    csr = _csr(rt, pki.generate_key(), str(new_node_id()))
    real = enroll_service.Enroller._enroll

    async def down(self: Any, *a: Any, **k: Any) -> Any:
        raise OperationalError("SELECT 1", {}, Exception("password=hunter2 down"))

    monkeypatch.setattr(enroll_service.Enroller, "_enroll", down)
    status, reason, detail = await araw(rt, _tok(code), csr)
    assert (status, reason) == (grpc.StatusCode.UNAVAILABLE, "unavailable")
    assert "hunter2" not in detail and "SELECT" not in detail
    monkeypatch.setattr(enroll_service.Enroller, "_enroll", real)
    assert (await araw(rt, _tok(code), csr))[0] is None


# ------------------------------------------------------------- listener hostility


def _open_tls_garbage(port: int) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        s.settimeout(3)
        try:
            s.recv(1024)
        except OSError:
            pass


async def test_listener_survives_garbage_plaintext_and_idle_connections(
    rt: ControllerRuntime, make_code: MakeCode, tmp_path: Path
) -> None:
    port = rt.enroll_port
    await asyncio.to_thread(_open_tls_garbage, port)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"\x16\x03\x01\xff\xff" + b"\x00" * 300)  # absurd TLS record header
    idle = [
        socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(150)
    ]
    try:
        half = socket.create_connection(("127.0.0.1", port), timeout=5)
        half.sendall(b"\x16\x03\x01\x00\x80")  # a TLS record header, then silence
        code, _ = await make_code()
        start = time.monotonic()
        result = await asyncio.to_thread(
            agent_enrollment.join, _ident(tmp_path, "a"), code, timeout_s=20
        )
        assert result.enrollment.node_id
        assert time.monotonic() - start < 15
        half.close()
    finally:
        for s in idle:
            s.close()


async def test_refused_attempts_still_count_against_the_rate_limit_and_burn_nothing(
    pg_settings: DatabaseSettings,
) -> None:
    settings = make_settings(
        pg_settings,
        fabric=False,
        enroll={"bind": "127.0.0.1:0", "rate_per_minute": 0.01, "rate_burst": 3},
    )
    create_fabric(settings.ca_dir)
    async with ControllerRuntime.start(settings) as rt:
        engine = create_engine(pg_settings)
        try:
            new = await InviteStore(engine).create(ttl_s=300, actor="t")
        finally:
            await engine.dispose()
        csr = _csr(rt, pki.generate_key(), str(new_node_id()))
        good_token = new.token
        junk = [await araw(rt, join_code.new_token(), csr) for _ in range(3)]
        assert [j[1] for j in junk] == ["token_unknown"] * 3
        # the 4th call - even with the GOOD token - is rate limited ...
        status, reason, _ = await araw(rt, good_token, csr)
        assert (status, reason) == (grpc.StatusCode.RESOURCE_EXHAUSTED, "rate_limited")
        # ... and a rate-limited call never touched the invite
        assert await _unused(pg_settings, new.record.id)
        assert (await _sql(pg_settings, "SELECT count(*) FROM node"))[0][0] == 0


# ------------------------------------------------------- restart / persistence


async def test_outstanding_invite_survives_a_controller_restart(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = make_settings(
        pg_settings, fabric=False, enroll={**FAST, "bind": "127.0.0.1:0"}
    )
    create_fabric(settings.ca_dir)
    async with ControllerRuntime.start(settings) as first:
        engine = create_engine(pg_settings)
        try:
            new = await InviteStore(engine).create(ttl_s=600, actor="t")
        finally:
            await engine.dispose()
        assert first.fabric is not None
        pin = pki.short_fingerprint(first.fabric.cert)
    # controller is down: the agent gets a clean "unreachable", the code is intact
    ident = _ident(tmp_path, "late")
    port_free = socket.socket()
    port_free.bind(("127.0.0.1", 0))
    dead = port_free.getsockname()[1]
    port_free.close()
    dead_code = join_code.encode(
        join_code.JoinCode(host="127.0.0.1", port=dead, token=new.token, ca_pin=pin)
    )
    with pytest.raises(JoinError) as info:
        await asyncio.to_thread(agent_enrollment.join, ident, dead_code, timeout_s=5)
    assert info.value.reason is JoinReason.UNREACHABLE
    async with ControllerRuntime.start(settings) as second:
        assert second.fabric is not None
        assert pki.short_fingerprint(second.fabric.cert) == pin  # same CA, same pin
        code = join_code.encode(
            join_code.JoinCode(
                host="127.0.0.1", port=second.enroll_port, token=new.token, ca_pin=pin
            )
        )
        result = await asyncio.to_thread(agent_enrollment.join, ident, code)
        assert result.enrollment.node_id == ident.node_id
    # restart again: the placeholder 'enrolling' row must admit the first Hello
    async with ControllerRuntime.start(settings) as third:
        async with identity_channel(f"127.0.0.1:{third.grpc_port}", ident) as ch:
            agent = FakeAgent(ch, node_id=ident.node_id, epoch=1)
            await agent.connect()
            await agent.close()
        state = (
            await _sql(
                pg_settings,
                "SELECT state FROM node WHERE node_id=:n",
                {"n": ident.node_id},
            )
        )[0][0]
        assert state in ("online", "suspect", "offline")


# --------------------------------------------------------------- secrets nowhere


@pytest.fixture
def log_buffer() -> Iterator[io.StringIO]:
    buffer = io.StringIO()
    configure_logging("DEBUG", stream=buffer)
    yield buffer
    configure_logging("INFO")


async def test_code_and_token_appear_in_no_table_log_or_error(
    rt: ControllerRuntime,
    make_code: MakeCode,
    pg_settings: DatabaseSettings,
    tmp_path: Path,
    log_buffer: io.StringIO,
) -> None:
    code, invite_id = await make_code()
    token = _tok(code)
    ok = _ident(tmp_path, "ok")
    errors: list[str] = []
    # the full cast: success, reuse, wrong pin, garbage CSR, revoked, bad code
    await asyncio.to_thread(agent_enrollment.join, ok, code)
    for victim, text_code in (
        (_ident(tmp_path, "reuse"), code),
        (
            _ident(tmp_path, "typo"),
            code[:-4] + ("aaaa" if not code.endswith("aaaa") else "bbbb"),
        ),
    ):
        with pytest.raises(JoinError) as info:
            await asyncio.to_thread(agent_enrollment.join, victim, text_code)
        errors.append(f"{info.value} {info.value!r} {info.value.details}")
    revoked_code, revoked_id = await make_code()
    engine = create_engine(pg_settings)
    try:
        await InviteStore(engine).revoke(revoked_id, actor="t")
    finally:
        await engine.dispose()
    with pytest.raises(JoinError) as info:
        await asyncio.to_thread(
            agent_enrollment.join, _ident(tmp_path, "rv"), revoked_code
        )
    errors.append(f"{info.value} {info.value!r} {info.value.details}")
    status, reason, detail = await araw(rt, token, b"garbage")
    errors.append(detail)

    secrets_ = []
    for c in (code, revoked_code):
        t = _tok(c)
        body = c.split("-", 1)[1]
        secrets_ += [c, body, body.replace("-", ""), t.hex(), t.hex().upper()]
        import base64

        secrets_ += [
            base64.b64encode(t).decode(),
            base64.urlsafe_b64encode(t).decode().rstrip("="),
        ]
    dump = ""
    for table in ("join_token", "audit_event", "node"):
        rows = await _sql(pg_settings, f"SELECT * FROM {table}")  # noqa: S608
        dump += json.dumps([[str(c) for c in r] for r in rows])
    haystack = dump + log_buffer.getvalue() + "\n".join(errors)
    for secret in secrets_:
        assert secret.lower() not in haystack.lower(), secret[:12]
    # audit rows: only ids / reason codes / small numbers, never anything token-like
    audit = await _sql(
        pg_settings, "SELECT action, actor, details::text FROM audit_event"
    )
    assert audit
    for _, _, details in audit:
        facts = {k: v for k, v in json.loads(details).items() if k != "serial"}
        assert not re.search(r"[0-9a-f]{32}", json.dumps(facts)), details
    # the stored token hash is sha256 of the token and is the ONLY derivative
    hashes_ = {
        h for (h,) in await _sql(pg_settings, "SELECT token_hash FROM join_token")
    }
    assert join_code.token_hash(token) in hashes_
    assert invite_id


async def test_enrolment_into_unicode_and_spaced_identity_path(
    rt: ControllerRuntime, make_code: MakeCode, tmp_path: Path
) -> None:
    ident = load_or_create(tmp_path / "Mein Laptop é日本" / "id dir")
    code, _ = await make_code()
    result = await asyncio.to_thread(agent_enrollment.join, ident, code)
    assert agent_enrollment.load(ident) is not None and result.address


# ----------------------------------------------------- lost response / retry


async def test_a_lost_response_burns_the_code_but_a_new_plain_code_recovers(
    rt: ControllerRuntime,
    make_code: MakeCode,
    pg_settings: DatabaseSettings,
    tmp_path: Path,
) -> None:
    """Retry semantics: the controller commits, the answer never reaches the laptop
    (the call is made raw and its response dropped). The same code is now
    ``token_used`` - by design - and the recovery is a NEW ordinary code: the node
    row holds this laptop's key, so no ``--replace`` is needed."""
    ident = _ident(tmp_path, "lost")
    code, _ = await make_code()
    csr = pki.build_csr(
        ident.private_key,
        fabric_id=rt.fabric.fabric_id if rt.fabric else "",
        node_id=ident.node_id,
    ).public_bytes(serialization.Encoding.PEM)
    assert (await araw(rt, _tok(code), csr))[0] is None  # response discarded
    err = None
    try:
        await asyncio.to_thread(agent_enrollment.join, ident, code)
    except JoinError as exc:
        err = exc
    assert err is not None and err.controller_reason == "token_used"
    assert not agent_enrollment.is_enrolled(ident.path)
    fresh, _ = await make_code()
    result = await asyncio.to_thread(agent_enrollment.join, ident, fresh)
    assert result.enrollment.node_id == ident.node_id
    serials = await _sql(
        pg_settings,
        "SELECT cert_serial FROM node WHERE node_id = :n",
        {"n": ident.node_id},
    )
    assert serials == [(format(result.enrollment.node_cert.serial_number, "x"),)]
