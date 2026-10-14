"""V0.7 G3 deliverable 4 (D139): the controller's REST + blob port is HTTPS.

Real PostgreSQL, the real ``ControllerRuntime`` (uvicorn with the server
certificate, TLS 1.3 only, client certificates optional), real clients:

* plaintext HTTP and TLS 1.2 are refused; TLS 1.3 is negotiated;
* the CLI's client context accepts the controller by its name;
* **two locks** on agent credentials: a fetch token / upload grant works only
  over a connection that presents the node certificate of the node it was
  minted for - no certificate, or another node's, is ``401``; the fabric
  token (the CLI's credential) needs no certificate.
"""

from __future__ import annotations

import asyncio
import os
import socket
import ssl

import httpx
import pytest
from mf_fabric import client_ssl_context, fabric_ca

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.fabric_tls import client_context
from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_attempt_id, new_node_id
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

BLOB = os.urandom(4096)


def _base(rt: ControllerRuntime) -> str:
    return f"https://127.0.0.1:{rt.http_port}"


async def test_plaintext_and_tls12_are_refused_tls13_negotiated(
    runtime: ControllerRuntime,
) -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.HTTPError):
            await http.get(f"http://127.0.0.1:{runtime.http_port}/healthz")

    def version(max_version: ssl.TLSVersion | None) -> str | None:
        ctx = client_ssl_context()
        if max_version is not None:
            ctx.maximum_version = max_version
        with (
            socket.create_connection(("127.0.0.1", runtime.http_port), 5) as raw,
            ctx.wrap_socket(raw, server_hostname="127.0.0.1") as tls,
        ):
            return tls.version()

    assert await asyncio.to_thread(version, None) == "TLSv1.3"
    with pytest.raises(ssl.SSLError):
        await asyncio.to_thread(version, ssl.TLSVersion.TLSv1_2)


@pytest.mark.security
async def test_cli_context_trusts_the_controller_by_name(
    runtime: ControllerRuntime,
) -> None:
    ctx = client_context(fabric_ca().cert)
    async with httpx.AsyncClient(verify=ctx) as http:
        for host in ("127.0.0.1", "localhost"):  # localhost is not in the SANs
            response = await http.get(f"https://{host}:{runtime.http_port}/healthz")
            assert response.status_code == 200


def _agent_client(node_id: str | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=client_ssl_context(node_id=node_id), timeout=10)


@pytest.mark.security
async def test_fetch_token_needs_the_minted_nodes_certificate(
    runtime: ControllerRuntime,
) -> None:
    from mf_jobs_harness import token_of

    digest = sha256_bytes(BLOB)
    async with httpx.AsyncClient(
        base_url=_base(runtime),
        headers={"authorization": f"Bearer {token_of(runtime)}"},
    ) as cli:  # the fabric token: no client certificate needed
        assert (
            await cli.put(f"/v1/blobs/{digest.hex}", content=BLOB)
        ).status_code == 201
    assert runtime.fetch_tokens is not None
    node, other = str(new_node_id()), str(new_node_id())
    token = runtime.fetch_tokens.mint(
        str(new_attempt_id()), [digest], ttl_s=60, node_id=node
    ).get_secret_value()
    headers = {"authorization": f"Bearer {token}"}
    url = f"{_base(runtime)}/v1/blobs/{digest.hex}"
    for who, expected in ((None, 401), (other, 401), (node, 200)):
        async with _agent_client(who) as http:
            response = await http.get(url, headers=headers)
        assert response.status_code == expected, (who, response.text)
    async with _agent_client(node) as http:
        assert (await http.get(url)).status_code == 401  # the cert alone: no


@pytest.mark.security
async def test_upload_grant_needs_the_minted_nodes_certificate(
    runtime: ControllerRuntime,
) -> None:
    assert runtime.upload_grants is not None
    node, other = str(new_node_id()), str(new_node_id())
    grant = runtime.upload_grants.mint(
        str(new_attempt_id()),
        ttl_s=60,
        max_blobs=4,
        max_bytes=1 << 20,
        node_id=node,
    ).get_secret_value()
    data = os.urandom(2048)
    digest = sha256_bytes(data)
    url = f"{_base(runtime)}/v1/blobs/{digest.hex}"
    headers = {"authorization": f"Bearer {grant}"}
    for who in (None, other):
        async with _agent_client(who) as http:
            refused = await http.put(url, content=data, headers=headers)
        assert refused.status_code == 401, (who, refused.text)
        async with _agent_client(who) as http:
            parts = await http.get(f"{url}/parts", headers=headers)
        assert parts.status_code in (401, 404), parts.text
    async with _agent_client(node) as http:
        stored = await http.put(url, content=data, headers=headers)
    assert stored.status_code == 201, stored.text
