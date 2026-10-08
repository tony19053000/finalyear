"""V0.7 G2 tester: (1) the 'enrolling' placeholder node row that an enrolment of a
never-seen node inserts (declared deviation 4) must not break any read API before
its first Hello; (2) end-to-end per-IP rate limit over IPv6 (defect T-1).
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Any

import grpc
import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from cryptography.hazmat.primitives import serialization
from mf_controller_harness import make_settings

from meshforge.common import join_code, pki
from meshforge.common.ids import new_node_id
from meshforge.common.proto.meshforge.v1 import enroll_pb2, enroll_pb2_grpc
from meshforge.controller.enrollment.invites import InviteStore
from meshforge.controller.fabric_ca import create_fabric
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]


def _csr(rt: ControllerRuntime, node: str) -> bytes:
    assert rt.fabric is not None
    return pki.build_csr(
        pki.generate_key(), fabric_id=rt.fabric.fabric_id, node_id=node
    ).public_bytes(serialization.Encoding.PEM)


def _call(rt: ControllerRuntime, target: str, token: bytes, csr: bytes) -> str | None:
    assert rt.fabric is not None
    creds = grpc.ssl_channel_credentials(pki.cert_to_pem(rt.fabric.cert))
    with grpc.secure_channel(
        target, creds, options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)]
    ) as channel:
        try:
            enroll_pb2_grpc.EnrollmentStub(channel).Enroll(
                enroll_pb2.EnrollRequest(join_token=token, csr_pem=csr), timeout=10
            )
        except grpc.RpcError as exc:
            info: Any = exc
            return dict(info.trailing_metadata() or ()).get("meshforge-reason")
    return None


@pytest_asyncio.fixture
async def rt(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = make_settings(
        pg_settings,
        fabric=False,
        enroll={"bind": "127.0.0.1:0", "rate_per_minute": 600.0, "rate_burst": 100},
    )
    create_fabric(settings.ca_dir)
    async with ControllerRuntime.start(settings) as runtime:
        yield runtime


async def test_every_read_endpoint_copes_with_an_enrolling_placeholder(
    rt: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    engine = create_engine(pg_settings)
    try:
        invite = await InviteStore(engine).create(ttl_s=300, actor="t")
    finally:
        await engine.dispose()
    node = str(new_node_id())
    assert (
        await asyncio.to_thread(
            _call, rt, f"127.0.0.1:{rt.enroll_port}", invite.token, _csr(rt, node)
        )
        is None
    )
    base = f"https://127.0.0.1:{rt.http_port}"
    async with httpx.AsyncClient(base_url=base) as http:
        for url in (
            "/v1/nodes",
            f"/v1/nodes/{node}",
            f"/v1/nodes/{node}/resources",
            f"/v1/nodes/{node}/resources?window=300",
            f"/v1/sessions?node_id={node}",
            "/v1/sessions",
            "/v1/network",
            "/v1/data",
            "/v1/jobs",
        ):
            response = await http.get(url)
            assert response.status_code < 500, (
                url,
                response.status_code,
                response.text[:200],
            )
        listing = (await http.get("/v1/nodes")).json()
        entry = next(n for n in listing["nodes"] if n["node_id"] == node)
        assert entry["state"] == "enrolling"
        assert entry["connected"] is False
        detail = await http.get(f"/v1/nodes/{node}")
        assert detail.status_code == 200 and detail.json()["state"] == "enrolling"


def _has_ipv6() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


@pytest.mark.skipif(not _has_ipv6(), reason="no IPv6 loopback")
async def test_ipv6_clients_are_rate_limited_per_ip(
    pg_settings: DatabaseSettings,
) -> None:
    settings = make_settings(
        pg_settings,
        fabric=False,
        enroll={"bind": "[::1]:0", "rate_per_minute": 0.01, "rate_burst": 2},
    )
    create_fabric(settings.ca_dir)
    async with ControllerRuntime.start(settings) as runtime:
        csr = _csr(runtime, str(new_node_id()))
        reasons = [
            await asyncio.to_thread(
                _call,
                runtime,
                f"[::1]:{runtime.enroll_port}",
                join_code.new_token(),
                csr,
            )
            for _ in range(8)
        ]
    assert "rate_limited" in reasons, reasons
