"""V0.7 G2 (0.7.4, D136, D150): enrolment end to end - real PostgreSQL, the real
controller runtime with its real TLS enrolment listener, the real agent join code.

* happy path: certificate stored owner-only beside ``node.key``, verified on load,
  recorded on the node (``enrolling`` until its first Hello, then ``online``),
  audited, listed by ``GET /v1/nodes``;
* a used, an expired and a revoked code are refused with their own reason and
  write nothing on the laptop;
* a code pinned to **another fabric's CA**: the agent refuses and the token never
  left it - the server records **no** ``Enroll`` call and the invite stays unused;
* CSRs: claiming another (enrolled) node_id, a broken signature, another fabric;
* ``--replace``: a new key for an enrolled node needs a replace code; a replace
  code for another node is refused and **not** burned;
* a refused enrolment never consumes the code;
* 16 laptops race one code: exactly one certificate;
* the per-IP rate limit refuses the burst+1-th call and does not touch another IP.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import grpc
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from mf_controller_harness import make_settings
from mf_fabric import identity_channel
from mf_session_client import FakeAgent
from sqlalchemy import text

from meshforge.agent import enrollment as agent_enrollment
from meshforge.agent.enrollment import JoinError, JoinReason
from meshforge.agent.identity import NodeIdentity, load_or_create
from meshforge.common import join_code, pki
from meshforge.common.ids import new_fabric_id, new_node_id
from meshforge.common.proto.meshforge.v1 import enroll_pb2, enroll_pb2_grpc
from meshforge.controller.enrollment import listener as enroll_listener
from meshforge.controller.enrollment.codes import issue_code
from meshforge.controller.enrollment.invites import InviteStore
from meshforge.controller.fabric_ca import create_fabric
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

FAST_ENROLL = {
    "bind": "127.0.0.1:0",
    "rate_per_minute": 600.0,
    "rate_burst": 100,
}


def _settings(pg: DatabaseSettings, **enroll: Any) -> ControllerSettings:
    return make_settings(pg, fabric=False, enroll={**FAST_ENROLL, **enroll})


@pytest_asyncio.fixture
async def enroll_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Spy on the server: every Enroll call that reached the servicer."""
    calls: list[str] = []
    real = enroll_listener.EnrollmentServicer.Enroll

    async def spy(self: Any, request: Any, context: Any) -> Any:
        calls.append(context.peer())
        return await real(self, request, context)

    monkeypatch.setattr(enroll_listener.EnrollmentServicer, "Enroll", spy)
    return calls


@pytest_asyncio.fixture
async def fabric_runtime(
    pg_settings: DatabaseSettings, enroll_calls: list[str]
) -> AsyncIterator[ControllerRuntime]:
    settings = _settings(pg_settings)
    create_fabric(settings.ca_dir)
    async with ControllerRuntime.start(settings) as runtime:
        assert runtime.enroll_port > 0
        yield runtime


@pytest_asyncio.fixture
async def invites(pg_settings: DatabaseSettings) -> AsyncIterator[InviteStore]:
    engine = create_engine(pg_settings)
    try:
        yield InviteStore(engine)
    finally:
        await engine.dispose()


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


MakeCode = Callable[..., Any]


@pytest.fixture
def make_code(fabric_runtime: ControllerRuntime, invites: InviteStore) -> MakeCode:
    async def make(*, ttl_s: int = 900, replace: str | None = None) -> tuple[str, str]:
        new = await invites.create(ttl_s=ttl_s, actor="test", replace_node_id=replace)
        assert fabric_runtime.fabric is not None
        issued = issue_code(
            new,
            fabric=fabric_runtime.fabric,
            host="127.0.0.1",
            port=fabric_runtime.enroll_port,
        )
        return issued.code, issued.invite_id

    return make


def _identity(tmp_path: Path, name: str) -> NodeIdentity:
    return load_or_create(tmp_path / name / "identity")


async def _join(identity: NodeIdentity, code: str, **kw: Any) -> Any:
    return await asyncio.to_thread(agent_enrollment.join, identity, code, **kw)


async def _join_error(identity: NodeIdentity, code: str, **kw: Any) -> JoinError:
    with pytest.raises(JoinError) as info:
        await _join(identity, code, **kw)
    return info.value


def _files(identity: NodeIdentity) -> set[str]:
    return {p.name for p in identity.path.iterdir() if p.suffix == ".crt"}


# -------------------------------------------------------------------- happy path


async def test_happy_path_and_first_hello(
    fabric_runtime: ControllerRuntime,
    make_code: MakeCode,
    pg_settings: DatabaseSettings,
    tmp_path: Path,
) -> None:
    import httpx

    identity = _identity(tmp_path, "a")
    code, invite_id = await make_code()
    result = await _join(identity, code)
    enrollment = result.enrollment
    assert fabric_runtime.fabric is not None
    assert enrollment.fabric_id == fabric_runtime.fabric.fabric_id
    assert enrollment.node_id == identity.node_id
    life = enrollment.not_after - enrollment.node_cert.not_valid_before_utc
    assert life.days == 30  # + the clock-skew back-dating
    usages = enrollment.node_cert.extensions.get_extension_for_class(
        x509.ExtendedKeyUsage
    ).value
    assert set(usages) == {
        x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH,
        x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
    }
    assert pki.cert_sans(enrollment.node_cert) == [
        f"meshforge://fabric/{enrollment.fabric_id}/node/{identity.node_id}"
    ]
    assert _files(identity) == {"node.crt", "ca.crt"}
    if os.name == "posix":
        for name in ("node.crt", "ca.crt"):
            mode = stat.S_IMODE((identity.path / name).stat().st_mode)
            assert mode == 0o600, (name, oct(mode))
    assert agent_enrollment.load(identity) is not None
    row = await _sql(
        pg_settings,
        "SELECT state, cert_serial, cert_fingerprint, public_key_fingerprint, "
        "cert_not_after FROM node WHERE node_id = :n",
        {"n": identity.node_id},
    )
    state, serial, fingerprint, key_fp, not_after = row[0]
    assert state == "enrolling"
    assert serial == format(enrollment.node_cert.serial_number, "x")
    assert fingerprint == enrollment.cert_fingerprint
    assert key_fp == identity.fingerprint
    assert not_after == enrollment.not_after
    audit = await _sql(
        pg_settings, "SELECT action, node_id, invite_id FROM audit_event ORDER BY id"
    )
    assert ("enrolled", identity.node_id, invite_id) in audit
    # The first Hello turns the placeholder row into a live node (§12.3).
    async with identity_channel(
        f"127.0.0.1:{fabric_runtime.grpc_port}", identity
    ) as ch:
        agent = FakeAgent(ch, node_id=identity.node_id, epoch=1)
        await agent.connect()
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{fabric_runtime.http_port}"
        ) as http:
            node = (await http.get(f"/v1/nodes/{identity.node_id}")).json()
        await agent.close()
    assert node["state"] == "online"
    assert node["certificate"]["serial"] == serial
    assert node["certificate"]["public_key_fingerprint_sha256"] == identity.fingerprint


# ------------------------------------------------------------- refused codes


async def test_used_expired_and_revoked_codes(
    fabric_runtime: ControllerRuntime,
    make_code: MakeCode,
    invites: InviteStore,
    pg_settings: DatabaseSettings,
    tmp_path: Path,
) -> None:
    code, _ = await make_code()
    await _join(_identity(tmp_path, "first"), code)
    second = _identity(tmp_path, "second")
    err = await _join_error(second, code)
    assert (err.reason, err.controller_reason) == (JoinReason.REFUSED, "token_used")
    assert code not in err.message
    expired, expired_id = await make_code(ttl_s=60)
    await _sql(
        pg_settings,
        "UPDATE join_token SET created_at = now() - interval '1 hour', "
        "expires_at = now() - interval '1 second' WHERE id = :i",
        {"i": expired_id},
    )
    err = await _join_error(second, expired)
    assert err.controller_reason == "token_expired"
    revoked, revoked_id = await make_code()
    await invites.revoke(revoked_id, actor="test")
    err = await _join_error(second, revoked)
    assert err.controller_reason == "token_revoked"
    assert _files(second) == set()
    refused = await _sql(
        pg_settings,
        "SELECT details->>'reason' FROM audit_event WHERE action = 'enroll_refused' "
        "ORDER BY id",
    )
    assert [r for (r,) in refused] == ["token_used", "token_expired", "token_revoked"]


async def test_wrong_ca_pin_sends_nothing(
    fabric_runtime: ControllerRuntime,
    make_code: MakeCode,
    enroll_calls: list[str],
    pg_settings: DatabaseSettings,
    tmp_path: Path,
) -> None:
    """A code pinned to another fabric's CA (a fake controller at the right
    address, from the agent's point of view): refused before the token is sent."""
    code, invite_id = await make_code()
    real = join_code.decode(code)
    other_fabric = pki.create_ca(new_fabric_id())
    forged = join_code.encode(
        join_code.JoinCode(
            host=real.host,
            port=real.port,
            token=real.token,
            ca_pin=pki.short_fingerprint(other_fabric.cert),
        )
    )
    identity = _identity(tmp_path, "a")
    err = await _join_error(identity, forged)
    assert err.reason is JoinReason.PIN_MISMATCH
    assert enroll_calls == []  # the server never saw an Enroll
    used = await _sql(
        pg_settings, "SELECT used_at FROM join_token WHERE id = :i", {"i": invite_id}
    )
    assert used == [(None,)]
    assert _files(identity) == set()
    # The genuine code still works afterwards.
    await _join(identity, code)
    assert len(enroll_calls) == 1


async def test_a_plain_tcp_server_is_not_a_controller(tmp_path: Path) -> None:
    server = await asyncio.start_server(
        lambda r, w: w.close(), host="127.0.0.1", port=0
    )
    port = server.sockets[0].getsockname()[1]
    code = join_code.encode(
        join_code.JoinCode(
            host="127.0.0.1", port=port, token=join_code.new_token(), ca_pin="a" * 32
        )
    )
    try:
        err = await _join_error(_identity(tmp_path, "a"), code, timeout_s=5)
    finally:
        server.close()
    assert err.reason in (JoinReason.TLS_ERROR, JoinReason.UNREACHABLE)


# ----------------------------------------------------------------------- CSRs


def _enroll_raw(
    runtime: ControllerRuntime, token: bytes, csr_pem: bytes
) -> tuple[grpc.StatusCode | None, str | None]:
    assert runtime.fabric is not None
    creds = grpc.ssl_channel_credentials(pki.cert_to_pem(runtime.fabric.cert))
    with grpc.secure_channel(
        f"127.0.0.1:{runtime.enroll_port}",
        creds,
        options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
    ) as channel:
        stub = enroll_pb2_grpc.EnrollmentStub(channel)
        try:
            stub.Enroll(
                enroll_pb2.EnrollRequest(join_token=token, csr_pem=csr_pem), timeout=10
            )
        except grpc.RpcError as exc:
            call: Any = exc
            reason = dict(call.trailing_metadata() or ()).get("meshforge-reason")
            return call.code(), reason
    return None, None


def _csr_pem(csr: x509.CertificateSigningRequest) -> bytes:
    return csr.public_bytes(serialization.Encoding.PEM)


async def test_hostile_csrs_are_refused_and_do_not_burn_the_code(
    fabric_runtime: ControllerRuntime,
    make_code: MakeCode,
    pg_settings: DatabaseSettings,
    tmp_path: Path,
) -> None:
    assert fabric_runtime.fabric is not None
    fabric_id = fabric_runtime.fabric.fabric_id
    victim = _identity(tmp_path, "victim")
    code, _ = await make_code()
    await _join(victim, code)
    attacker_key = pki.generate_key()
    code, invite_id = await make_code()
    token = join_code.decode(code).token
    claim = pki.build_csr(attacker_key, fabric_id=fabric_id, node_id=victim.node_id)
    status, reason = await asyncio.to_thread(
        _enroll_raw, fabric_runtime, token, _csr_pem(claim)
    )
    assert (status, reason) == (grpc.StatusCode.ALREADY_EXISTS, "node_key_conflict")
    # A CSR whose signature does not verify (its key was swapped after signing).
    good = pki.build_csr(attacker_key, fabric_id=fabric_id, node_id=str(new_node_id()))
    der = bytearray(good.public_bytes(serialization.Encoding.DER))
    der[-5] ^= 0x01  # inside the signature
    tampered = x509.load_der_x509_csr(bytes(der))
    status, reason = await asyncio.to_thread(
        _enroll_raw, fabric_runtime, token, _csr_pem(tampered)
    )
    assert status is grpc.StatusCode.INVALID_ARGUMENT
    assert reason in ("csr_bad_signature", "csr_malformed")
    other_fabric = pki.build_csr(
        attacker_key, fabric_id=str(new_fabric_id()), node_id=str(new_node_id())
    )
    status, reason = await asyncio.to_thread(
        _enroll_raw, fabric_runtime, token, _csr_pem(other_fabric)
    )
    assert (status, reason) == (grpc.StatusCode.INVALID_ARGUMENT, "csr_fabric_mismatch")
    status, reason = await asyncio.to_thread(
        _enroll_raw, fabric_runtime, b"short", _csr_pem(good)
    )
    assert (status, reason) == (grpc.StatusCode.INVALID_ARGUMENT, "malformed_request")
    # None of that consumed the code.
    used = await _sql(
        pg_settings, "SELECT used_at FROM join_token WHERE id = :i", {"i": invite_id}
    )
    assert used == [(None,)]
    status, _ = await asyncio.to_thread(
        _enroll_raw, fabric_runtime, token, _csr_pem(good)
    )
    assert status is None  # finally a valid one: accepted


# -------------------------------------------------------------------- replace


def _rekeyed(identity: NodeIdentity, tmp_path: Path) -> NodeIdentity:
    """The same node_id with a brand-new key (a reinstalled laptop)."""
    key = pki.generate_key()
    directory = tmp_path / "reinstalled"
    directory.mkdir(mode=0o700)
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    digest = hashes.Hash(hashes.SHA256())
    digest.update(spki)
    return NodeIdentity(
        node_id=identity.node_id,
        private_key=key,
        session_epoch=0,
        fingerprint=digest.finalize().hex(),
        path=directory,
    )


async def test_replace_flow(
    fabric_runtime: ControllerRuntime,
    make_code: MakeCode,
    pg_settings: DatabaseSettings,
    tmp_path: Path,
) -> None:
    original = _identity(tmp_path, "a")
    code, _ = await make_code()
    await _join(original, code)
    rekeyed = _rekeyed(original, tmp_path)
    plain, plain_id = await make_code()
    err = await _join_error(rekeyed, plain)
    assert err.controller_reason == "node_key_conflict"
    other = str(new_node_id())
    wrong, wrong_id = await make_code(replace=other)
    err = await _join_error(rekeyed, wrong)
    assert err.controller_reason == "replace_mismatch"
    replace, _ = await make_code(replace=original.node_id)
    result = await _join(rekeyed, replace)
    assert pki.public_keys_equal(
        rekeyed.private_key.public_key(), result.enrollment.node_cert.public_key()
    )
    row = await _sql(
        pg_settings,
        "SELECT public_key_fingerprint FROM node WHERE node_id = :n",
        {"n": original.node_id},
    )
    assert row == [(rekeyed.fingerprint,)]
    unused = await _sql(
        pg_settings,
        "SELECT id FROM join_token WHERE used_at IS NULL ORDER BY id",
    )
    assert {plain_id, wrong_id} <= {i for (i,) in unused}  # refusals burned nothing
    audit = await _sql(
        pg_settings,
        "SELECT details->>'key_replaced' FROM audit_event WHERE action = 'enrolled' "
        "ORDER BY id",
    )
    assert [r for (r,) in audit] == ["false", "true"]


async def test_same_key_rejoin_needs_force_and_reissues(
    fabric_runtime: ControllerRuntime, make_code: MakeCode, tmp_path: Path
) -> None:
    identity = _identity(tmp_path, "a")
    first, _ = await make_code()
    one = (await _join(identity, first)).enrollment
    second, _ = await make_code()
    err = await _join_error(identity, second)
    assert err.reason is JoinReason.ALREADY_ENROLLED  # refused locally, code unused
    two = (await _join(identity, second, force=True)).enrollment
    assert two.node_cert.serial_number != one.node_cert.serial_number


# ----------------------------------------------------------------------- race


async def test_sixteen_laptops_race_one_code(
    fabric_runtime: ControllerRuntime,
    make_code: MakeCode,
    pg_settings: DatabaseSettings,
    tmp_path: Path,
) -> None:
    code, invite_id = await make_code()
    laptops = [_identity(tmp_path, f"l{i}") for i in range(16)]
    barrier = threading.Barrier(16)

    def attempt(identity: NodeIdentity) -> str:
        barrier.wait(timeout=30)
        try:
            agent_enrollment.join(identity, code)
        except JoinError as exc:
            return exc.controller_reason or exc.reason.value
        return "enrolled"

    results = await asyncio.gather(
        *(asyncio.to_thread(attempt, laptop) for laptop in laptops)
    )
    assert sorted(results).count("enrolled") == 1, results
    assert set(results) == {"enrolled", "token_used"}, results
    winners = [lap for lap, r in zip(laptops, results, strict=True) if r == "enrolled"]
    used_by = await _sql(
        pg_settings,
        "SELECT used_by_node FROM join_token WHERE id = :i",
        {"i": invite_id},
    )
    assert used_by == [(winners[0].node_id,)]
    certs = await _sql(
        pg_settings, "SELECT count(*) FROM node WHERE cert_serial IS NOT NULL"
    )
    assert certs == [(1,)]


# ------------------------------------------------------------------ rate limit


async def _forward(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="needs 127.0.0.0/8 source addresses"
)
async def test_rate_limit_is_per_source_ip(
    pg_settings: DatabaseSettings, enroll_calls: list[str]
) -> None:
    settings = _settings(pg_settings, rate_per_minute=0.01, rate_burst=2)
    create_fabric(settings.ca_dir)
    async with ControllerRuntime.start(settings) as runtime:
        target = runtime.enroll_port

        async def relay(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            # Re-originate the connection from another loopback address.
            ur, uw = await asyncio.open_connection(
                "127.0.0.1", target, local_addr=("127.0.0.3", 0)
            )
            await asyncio.gather(_forward(r, uw), _forward(ur, w))

        proxy = await asyncio.start_server(relay, host="127.0.0.1", port=0)
        proxy_port = proxy.sockets[0].getsockname()[1]
        try:
            csr = _csr_pem(
                pki.build_csr(
                    pki.generate_key(),
                    fabric_id=runtime.fabric.fabric_id if runtime.fabric else "",
                    node_id=str(new_node_id()),
                )
            )

            def call(port: int) -> str | None:
                assert runtime.fabric is not None
                creds = grpc.ssl_channel_credentials(
                    pki.cert_to_pem(runtime.fabric.cert)
                )
                with grpc.secure_channel(
                    f"127.0.0.1:{port}",
                    creds,
                    options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
                ) as channel:
                    try:
                        enroll_pb2_grpc.EnrollmentStub(channel).Enroll(
                            enroll_pb2.EnrollRequest(
                                join_token=join_code.new_token(), csr_pem=csr
                            ),
                            timeout=10,
                        )
                    except grpc.RpcError as exc:
                        info: Any = exc
                        return dict(info.trailing_metadata() or ()).get(
                            "meshforge-reason"
                        )
                return None

            first = [await asyncio.to_thread(call, target) for _ in range(3)]
            assert first == ["token_unknown", "token_unknown", "rate_limited"]
            other = [await asyncio.to_thread(call, proxy_port) for _ in range(2)]
            assert other == ["token_unknown", "token_unknown"]  # its own bucket
            assert any("127.0.0.3" in peer for peer in enroll_calls)
        finally:
            proxy.close()


# ------------------------------------------------------------------ REST + CLI


async def test_rest_invites_need_the_fabric_token(
    fabric_runtime: ControllerRuntime, tmp_path: Path
) -> None:
    import httpx

    from meshforge.common.fabric_token import read_token_file

    token = read_token_file(fabric_runtime.settings.token_path).get_secret_value()
    auth = {"authorization": f"Bearer {token}"}
    base = f"http://127.0.0.1:{fabric_runtime.http_port}"
    async with httpx.AsyncClient(base_url=base) as http:
        for method, path in (
            ("POST", "/v1/fabric/invites"),
            ("GET", "/v1/fabric/invites"),
            ("POST", "/v1/fabric/invites/inv_01J0000000000000000000AAAA/revoke"),
        ):
            refused = await http.request(method, path, json={})
            assert refused.status_code == 401, (method, path)
            wrong = await http.request(
                method, path, json={}, headers={"authorization": "Bearer " + "x" * 43}
            )
            assert wrong.status_code == 401
        made = await http.post("/v1/fabric/invites", json={"ttl_s": 120}, headers=auth)
        assert made.status_code == 201, made.text
        issued = made.json()
        decoded = join_code.decode(issued["code"])
        assert decoded.port == fabric_runtime.enroll_port  # the BOUND port
        bad = await http.post("/v1/fabric/invites", json={"ttl_s": 59}, headers=auth)
        assert bad.status_code == 400
        listed = (await http.get("/v1/fabric/invites", headers=auth)).json()
        assert [i["id"] for i in listed["invites"]] == [issued["invite_id"]]
        assert issued["code"] not in str(listed)
    # The REST code really enrols a laptop.
    result = await _join(_identity(tmp_path, "rest"), issued["code"])
    assert result.enrollment.node_id


async def test_rest_invite_without_a_ca_is_a_conflict(
    pg_settings: DatabaseSettings,
) -> None:
    import httpx

    from meshforge.common.fabric_token import read_token_file
    from meshforge.controller.settings import ControllerSettings

    base = _settings(pg_settings)
    # V0.7 G3: only insecure-dev (loopback) runs without a CA at all.
    settings = ControllerSettings(
        security={"mode": "insecure-dev"},
        controller=base.controller,
        database=base.database,
    )
    async with ControllerRuntime.start(settings) as runtime:
        assert runtime.enroll_port == 0  # no CA: no enrolment listener
        token = read_token_file(runtime.settings.token_path).get_secret_value()
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{runtime.http_port}"
        ) as http:
            response = await http.post(
                "/v1/fabric/invites",
                json={},
                headers={"authorization": f"Bearer {token}"},
            )
    assert response.status_code == 409
    assert "fabric create" in response.json()["error"]["message"]


async def test_agent_cli_join_and_identity_show(
    fabric_runtime: ControllerRuntime,
    make_code: MakeCode,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from typer.testing import CliRunner

    from meshforge.agent.main import app as agent_app

    config = tmp_path / "agent.toml"
    config.write_text(
        f"data_dir = '{(tmp_path / 'agent-data').as_posix()}'\n", encoding="utf-8"
    )
    code, _ = await make_code()
    runner = CliRunner()

    def invoke(*args: str, stdin: str | None = None) -> Any:
        return runner.invoke(agent_app, list(args), input=stdin)

    shown = await asyncio.to_thread(
        invoke, "identity", "show", "--json", "--config", str(config)
    )
    assert json.loads(shown.stdout)["enrolled"] is False
    joined = await asyncio.to_thread(
        invoke, "join", "-", "--config", str(config), stdin=code + "\n"
    )
    assert joined.exit_code == 0, joined.output
    assert "joined fabric" in joined.stdout and code not in joined.output
    shown = await asyncio.to_thread(
        invoke, "identity", "show", "--json", "--config", str(config)
    )
    facts = json.loads(shown.stdout)
    assert facts["enrolled"] is True and facts["cert_expired"] is False
    assert fabric_runtime.fabric is not None
    assert facts["fabric_id"] == fabric_runtime.fabric.fabric_id
    again = await asyncio.to_thread(
        invoke, "join", "-", "--config", str(config), stdin=code + "\n"
    )
    assert again.exit_code == 5  # CONFLICT: already enrolled, code untouched
    bad = await asyncio.to_thread(invoke, "join", "mf1-zzzzz", "--config", str(config))
    assert bad.exit_code == 2
