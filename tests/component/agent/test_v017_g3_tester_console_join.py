# ruff: noqa: PLE2502, RUF001, RUF059, S104, S112
"""V0.17 G3 tester: join / node name / supervisor against a real controller
(PostgreSQL in Docker), real uvicorn console, real TLS.

Criteria: join with a bad code / wrong CA pin / an mDNS-substituted host where
the pin still decides (nothing secret sent to the impostor); node_name with
unicode, oversize and control characters carried end to end into node.display_name;
a CLI join is noticed by polling; leave resets identity; a rejoin is a new node.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import socket
import ssl
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("fastapi", reason="needs the agent extra")

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from mf_agent_harness import AGENT_RECONNECT
from mf_controller_harness import make_settings
from sqlalchemy import text

from meshforge.agent.console.logbuffer import install_log_buffer
from meshforge.agent.console.server import run_with_console
from meshforge.agent.console.supervisor import FabricPhase, Supervisor
from meshforge.agent.enrollment import join as cli_join
from meshforge.agent.identity import load_or_create
from meshforge.agent.settings import AgentSection, AgentSettings
from meshforge.common.errors import ExitCode
from meshforge.controller.enrollment.codes import issue_code
from meshforge.controller.enrollment.invites import InviteStore
from meshforge.controller.fabric_ca import create_fabric
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(300),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _until(check: Callable[[], Any], within_s: float = 30.0) -> Any:
    start = time.monotonic()
    while True:
        value = check()
        if value:
            return value
        assert time.monotonic() - start < within_s, "condition not reached"
        await asyncio.sleep(0.05)


class RogueController:
    """A TLS listener with its own (wrong) CA. Records every byte the client
    sends after the handshake: a joining agent must send none."""

    def __init__(self, host: str, port: int) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rogue")])
        now = dt.datetime.now(dt.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("meshforge-controller")]),
                False,
            )
            .sign(key, hashes.SHA256())
        )
        d = Path(__import__("tempfile").mkdtemp())
        (d / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (d / "k.pem").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(d / "c.pem", d / "k.pem")
        self.ctx.set_alpn_protocols(["h2"])
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.received = b""
        self.connections = 0
        self._stop = False
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self) -> None:
        while not self._stop:
            try:
                raw, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            self.connections += 1
            try:
                raw.settimeout(2)
                with self.ctx.wrap_socket(raw, server_side=True) as tls:
                    try:
                        while data := tls.recv(65536):
                            self.received += data
                    except (OSError, ssl.SSLError):
                        pass
            except (OSError, ssl.SSLError):
                pass

    def close(self) -> None:
        self._stop = True
        self._t.join(3)
        self.sock.close()


@pytest_asyncio.fixture
async def enroll_port() -> int:
    return _free_port()


@pytest_asyncio.fixture
async def runtime(
    pg_settings: DatabaseSettings, enroll_port: int
) -> AsyncIterator[ControllerRuntime]:
    settings = make_settings(
        pg_settings,
        fabric=False,
        enroll={
            "bind": f"127.0.0.1:{enroll_port}",
            "rate_per_minute": 600.0,
            "rate_burst": 100,
        },
    )
    create_fabric(settings.ca_dir)
    async with ControllerRuntime.start(settings) as rt:
        yield rt


@pytest_asyncio.fixture
async def invites(pg_settings: DatabaseSettings) -> AsyncIterator[InviteStore]:
    engine = create_engine(pg_settings)
    try:
        yield InviteStore(engine)
    finally:
        await engine.dispose()


async def _code(runtime: ControllerRuntime, invites: InviteStore) -> str:
    new = await invites.create(ttl_s=900, actor="test", replace_node_id=None)
    assert runtime.fabric is not None
    return issue_code(
        new, fabric=runtime.fabric, host="127.0.0.1", port=runtime.enroll_port
    ).code


async def _row(pg: DatabaseSettings, node_id: str) -> Any:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            return (
                await conn.execute(
                    text("select display_name, hostname from node where node_id = :n"),
                    {"n": node_id},
                )
            ).first()
    finally:
        await engine.dispose()


class Console:
    def __init__(self, http: httpx.AsyncClient, origin: dict[str, str]) -> None:
        self.http, self.origin = http, origin

    async def get(self, path: str) -> httpx.Response:
        return await self.http.get(f"/console/v1/{path}")

    async def post(self, path: str, body: Any) -> httpx.Response:
        return await self.http.post(
            f"/console/v1/{path}", json=body, headers=self.origin
        )

    async def put(self, path: str, body: Any) -> httpx.Response:
        return await self.http.put(
            f"/console/v1/{path}", json=body, headers=self.origin
        )


@pytest_asyncio.fixture
async def agent(
    runtime: ControllerRuntime, tmp_path: Path
) -> AsyncIterator[tuple[Console, Supervisor, AgentSettings]]:
    port = _free_port()
    token_dir = tmp_path / "console"
    cfg = AgentSection(
        controller_address=f"127.0.0.1:{runtime.grpc_port}",
        hello_timeout_s=2.0,
        identity_lock_timeout_s=0.5,
        reconnect=dict(AGENT_RECONNECT),
        console={"enabled": True, "port": port, "token_dir": token_dir},
        policy={"reload_interval_s": 0.2},
    )
    settings = AgentSettings(data_dir=tmp_path / "data", agent=cfg)
    sup = Supervisor(settings, install_signals=False, poll_s=0.2)
    buf = install_log_buffer(2000)
    task = asyncio.create_task(run_with_console(settings, supervisor=sup, logs=buf))
    base = f"http://127.0.0.1:{port}"
    try:
        await _until(lambda: (token_dir / "token").is_file())
        token = (token_dir / "token").read_text().strip()
        async with httpx.AsyncClient(base_url=base, timeout=60) as http:
            # fix round 1 (Lead item 3): the token only buys launch codes
            launch = await http.post(
                "/console/v1/launch-code", headers={"authorization": f"Bearer {token}"}
            )
            assert launch.status_code == 200
            r = await http.post(
                "/console/v1/session",
                json={"code": launch.json()["code"]},
                headers={"origin": base},
            )
            assert r.status_code == 200
            http.cookies.update(r.cookies)
            yield Console(http, {"origin": base}), sup, settings
    finally:
        import logging

        sup.request_shutdown()
        try:
            assert await asyncio.wait_for(task, 60) == ExitCode.OK
        finally:
            logging.getLogger().removeHandler(buf)


# ------------------------------------------------------------- bad joins


async def test_bad_codes_get_plain_errors_and_change_nothing(
    agent: tuple[Console, Supervisor, AgentSettings],
    runtime: ControllerRuntime,
    invites: InviteStore,
) -> None:
    con, sup, settings = agent
    good = await _code(runtime, invites)
    flipped = good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB")
    cases = {
        "garbage": "hello world",
        "prefix only": "mf1-",
        "truncated": good[: len(good) // 2],
        "bit-flipped": flipped,
        "unicode": "mf1-‮\u0000日本語💻",
        "whitespace": "   ",
    }
    for label, code in cases.items():
        r = await con.post("join", {"code": code})
        assert r.status_code in (400, 422), (label, r.text)
        body = r.json()["error"]
        assert body["code"] in ("bad_code", "invalid", "ca_mismatch", "refused"), (
            label,
            body,
        )
        assert code[:8] not in body["message"] or label in ("whitespace", "prefix only")
    assert (await con.post("join", {"code": "x" * 513})).status_code == 422
    assert (await con.post("join", {})).status_code == 422
    assert (await con.post("join", {"code": good, "extra": 1})).status_code == 422
    st = (await con.get("status")).json()
    assert st["joined"] is False and st["phase"] == "waiting_for_join"
    assert not (settings.identity_dir / "node.crt").exists()
    # the genuine code was not burned by any of that
    ok = await con.post("join", {"code": good})
    assert ok.status_code == 200, ok.text


async def test_non_lan_or_odd_controller_picks_are_refused(
    agent: tuple[Console, Supervisor, AgentSettings],
    runtime: ControllerRuntime,
    invites: InviteStore,
) -> None:
    con, _, _ = agent
    good = await _code(runtime, invites)
    for host in (
        "8.8.8.8",
        "evil.example",
        "localhost",
        "127.0.0.1 evil",
        "0.0.0.0",
        "224.0.0.1",
        "127.0.0.1:1",
        "127.0.0.1\n.evil",
    ):
        r = await con.post("join", {"code": good, "controller_host": host})
        assert r.status_code == 400, (host, r.text)
        assert r.json()["error"]["code"] in ("unreachable", "bad_code"), host
    assert (await con.get("status")).json()["joined"] is False


async def test_substituted_host_with_wrong_ca_is_ca_mismatch_and_nothing_secret_is_sent(
    agent: tuple[Console, Supervisor, AgentSettings],
    runtime: ControllerRuntime,
    invites: InviteStore,
    enroll_port: int,
) -> None:
    """An mDNS answer from an impostor: same port, other LAN address, other CA."""
    con, _, settings = agent
    good = await _code(runtime, invites)
    rogue = RogueController("127.0.0.2", enroll_port)
    try:
        r = await con.post("join", {"code": good, "controller_host": "127.0.0.2"})
        assert r.status_code == 400, r.text
        assert r.json()["error"]["code"] == "ca_mismatch", r.text
        assert rogue.connections >= 1, (
            "the impostor was never contacted: test is vacuous"
        )
        assert rogue.received == b"", (
            "bytes were sent to a controller whose CA did not match the pin"
        )
        assert not (settings.identity_dir / "node.crt").exists()
        assert (await con.get("status")).json()["joined"] is False
    finally:
        rogue.close()
    # the code is still good for the real controller (impostor never saw it)
    r = await con.post("join", {"code": good, "controller_host": "127.0.0.1"})
    assert r.status_code == 200, r.text


async def test_substituted_host_that_is_dead_is_unreachable(
    agent: tuple[Console, Supervisor, AgentSettings],
    runtime: ControllerRuntime,
    invites: InviteStore,
) -> None:
    con, _, _ = agent
    good = await _code(runtime, invites)
    r = await con.post("join", {"code": good, "controller_host": "127.0.0.3"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "unreachable", r.text


async def test_second_join_while_joined_is_409_and_does_not_burn_the_code(
    agent: tuple[Console, Supervisor, AgentSettings],
    runtime: ControllerRuntime,
    invites: InviteStore,
) -> None:
    con, sup, _ = agent
    assert (
        await con.post("join", {"code": await _code(runtime, invites)})
    ).status_code == 200
    spare = await _code(runtime, invites)
    r = await con.post("join", {"code": spare})
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_joined"
    # concurrency: two simultaneous joins on a fresh laptop cannot both win
    # (checked below with leave)
    await con.post("leave", {"confirm": "leave"})
    await sup.wait_phase(FabricPhase.WAITING_FOR_JOIN, timeout_s=30)
    a, b = await _code(runtime, invites), await _code(runtime, invites)
    ra, rb = await asyncio.gather(
        con.post("join", {"code": a}), con.post("join", {"code": b})
    )
    codes = sorted([ra.status_code, rb.status_code])
    assert codes[0] == 200, (ra.text, rb.text)
    assert codes[1] in (200, 400, 409), "unexpected status"
    st = (await con.get("status")).json()
    assert st["joined"] is True
    # whichever finished: exactly one node id is the identity
    if codes[1] == 200:
        assert ra.json()["node_id"] == rb.json()["node_id"]


# ------------------------------------------------------------- node name


async def _joined(
    con: Console, sup: Supervisor, runtime: ControllerRuntime, invites: InviteStore
) -> str:
    r = await con.post("join", {"code": await _code(runtime, invites)})
    assert r.status_code == 200, r.text
    await sup.wait_phase(FabricPhase.RUNNING, timeout_s=30)
    return str(r.json()["node_id"])


async def test_node_name_matrix_is_validated_at_the_console(
    agent: tuple[Console, Supervisor, AgentSettings],
) -> None:
    con, _, settings = agent
    bad = {
        "65 chars": "a" * 65,
        "tab": "a\tb",
        "newline": "a\nb",
        "nul": "a\x00b",
        "esc": "a\x1b[31mb",
        "rtl override": "a‮b",
        "zero width": "a\u200bb",
        "bom": "﻿ab",
        "line sep": "a b",
        "nbsp": "a b",
        "lone surrogate": "a\ud800b",
        "del": "a\x7fb",
        "c1": "a\x85b",
    }
    for label, name in bad.items():
        try:
            r = await con.put("node-name", {"name": name})
        except Exception:  # noqa: BLE001 - an unencodable surrogate fails in httpx
            continue
        assert r.status_code in (400, 422), (label, r.status_code, r.text)
    assert not (settings.data_dir / "node_name").exists()
    assert (await con.put("node-name", {"name": "a" * 4000})).status_code == 422
    assert (await con.put("node-name", {"name": 7})).status_code == 422
    assert (await con.put("node-name", {})).status_code == 422


async def test_node_name_unicode_end_to_end_to_the_database_and_api(
    agent: tuple[Console, Supervisor, AgentSettings],
    runtime: ControllerRuntime,
    invites: InviteStore,
    pg_settings: DatabaseSettings,
) -> None:
    con, sup, settings = agent
    names = [
        "Lab-Laptop ñandú",
        "日本語のノート 💻",
        "<script>alert(1)</script> & \"quotes\" 'x'",
        "x" * 64,
        "💻" * 64,  # 64 characters, 256 bytes
        "é" * 20,  # combining marks
        "'; drop table node; --",
    ]
    for name in names:
        r = await con.put("node-name", {"name": name})
        assert r.status_code == 200, (name, r.text)
        assert r.json()["name"] == name
    # padded whitespace is trimmed, an all-space name clears
    assert (await con.put("node-name", {"name": "  trimmed  "})).json()[
        "name"
    ] == "trimmed"
    assert (await con.get("node-name")).json()["name"] == "trimmed"
    final = names[1]
    await con.put("node-name", {"name": final})
    node_id = await _joined(con, sup, runtime, invites)
    await _until(lambda: True)
    for _ in range(200):
        row = await _row(pg_settings, node_id)
        if row is not None and row[0] == final:
            break
        await asyncio.sleep(0.1)
    assert row is not None and row[0] == final, row
    # restart picks up a changed name for the NEXT hello
    for name in (names[4], names[2]):
        assert (await con.put("node-name", {"name": name})).status_code == 200
        assert (await con.post("restart", {})).status_code == 200
        for _ in range(300):
            row = await _row(pg_settings, node_id)
            if row[0] == name:
                break
            await asyncio.sleep(0.1)
        assert row[0] == name, (name, row)
    # the name file is private and not world readable
    import stat

    mode = stat.S_IMODE((settings.data_dir / "node_name").stat().st_mode)
    assert mode & 0o077 == 0
    # clearing: empty name -> back to hostname-only (display_name empty)
    r = await con.put("node-name", {"name": "   "})
    assert r.status_code == 200 and r.json()["name"] == ""
    await con.post("restart", {})
    for _ in range(300):
        row = await _row(pg_settings, node_id)
        if row[0] == "":
            break
        await asyncio.sleep(0.1)
    assert row[0] == "", row


async def test_corrupt_node_name_file_does_not_stop_the_hello(
    agent: tuple[Console, Supervisor, AgentSettings],
    runtime: ControllerRuntime,
    invites: InviteStore,
    pg_settings: DatabaseSettings,
) -> None:
    con, sup, settings = agent
    (settings.data_dir).mkdir(parents=True, exist_ok=True)
    f = settings.data_dir / "node_name"
    f.write_bytes(b"bad\x00name\xff" * 40)
    f.chmod(0o600)
    node_id = await _joined(con, sup, runtime, invites)
    row = await _row(pg_settings, node_id)
    assert row is not None  # node registered despite the corrupt name file
    st = (await con.get("status")).json()
    assert (
        st["connection"] in ("ONLINE", "CONNECTING", None) or st["phase"] == "running"
    )
    # the console reports it instead of a 500
    assert (await con.get("node-name")).status_code == 200


# ------------------------------------------------------------ supervisor


async def test_cli_join_is_noticed_by_polling_and_leave_resets_identity(
    agent: tuple[Console, Supervisor, AgentSettings],
    runtime: ControllerRuntime,
    invites: InviteStore,
    pg_settings: DatabaseSettings,
) -> None:
    con, sup, settings = agent
    assert sup.phase is FabricPhase.WAITING_FOR_JOIN
    identity = load_or_create(settings.identity_dir, lock_timeout_s=2.0)
    await asyncio.to_thread(cli_join, identity, await _code(runtime, invites))
    await sup.wait_phase(FabricPhase.RUNNING, timeout_s=30)  # no console call at all
    st = (await con.get("status")).json()
    first_node = st["node_id"]
    assert st["joined"] is True and first_node == str(identity.node_id)
    # leave -> identity gone -> console stays -> rejoin = a NEW node
    r = await con.post("leave", {"confirm": "leave"})
    assert r.status_code == 200
    await sup.wait_phase(FabricPhase.WAITING_FOR_JOIN, timeout_s=30)
    st = (await con.get("status")).json()
    assert st["joined"] is False and st["node_id"] is None
    assert (await con.post("leave", {"confirm": "leave"})).status_code == 409
    assert (await con.post("leave", {"confirm": "yes"})).status_code == 422
    r = await con.post("join", {"code": await _code(runtime, invites)})
    assert r.status_code == 200
    assert r.json()["node_id"] != first_node
    await sup.wait_phase(FabricPhase.RUNNING, timeout_s=30)


async def test_two_restarts_in_a_row_and_restart_before_join_are_harmless(
    agent: tuple[Console, Supervisor, AgentSettings],
    runtime: ControllerRuntime,
    invites: InviteStore,
) -> None:
    con, sup, _ = agent
    assert (await con.post("restart", {})).status_code == 200  # nothing to restart
    assert sup.phase is FabricPhase.WAITING_FOR_JOIN
    await _joined(con, sup, runtime, invites)
    await asyncio.gather(con.post("restart", {}), con.post("restart", {}))
    await sup.wait_phase(FabricPhase.RUNNING, timeout_s=30)
    st = (await con.get("status")).json()
    assert st["joined"] is True


async def test_policy_is_validated_and_empty_put_is_a_noop(
    agent: tuple[Console, Supervisor, AgentSettings],
) -> None:
    con, _, _ = agent
    before = (await con.get("policy")).json()
    bad = [
        {"max_cpu_percent": 0},
        {"max_cpu_percent": 101},
        {"max_cpu_percent": "fifty"},
        {"accept_jobs": "yes please"},
        {"accept_jobs": None},
        {"max_cpu_jobs": -1},
        {"max_ram_bytes": -5},
        {"allowed_users": ["a"] * 65},
        {"nope": 1},
    ]
    for values in bad:
        r = await con.put("policy", {"values": values})
        assert r.status_code in (400, 422), (values, r.text)
    assert (await con.put("policy", {"values": {}})).status_code == 200
    assert (await con.put("policy", {})).status_code == 422
    after = (await con.get("policy")).json()
    assert before["policy"] == after["policy"]
    # safe defaults before the wizard changes anything (Q7/Q26)
    assert after["policy"]["accept_jobs"] is False
    assert after["policy"].get("accept_gpu_sessions") is False
