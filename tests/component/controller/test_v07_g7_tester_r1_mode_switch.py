"""V0.7 G7 tester r1: the insecure-dev blob change (2aa2caa) must not weaken
mtls. One database, three consecutive controllers: mtls -> insecure-dev -> mtls.

* mtls: plaintext HTTP refused; fetch token / upload grant / part routes without
  a cert, with another node's cert, and a cert alone (no token) are 401; the
  minted node's cert + token works. Every route, not only GET.
* insecure-dev (loopback, plain HTTP): the token alone decides; a bad token is 401.
* back to mtls: the lock is there again (no state left behind by the dev run).
"""

from __future__ import annotations

import os

import httpx
import pytest
from mf_controller_harness import make_settings
from mf_fabric import client_ssl_context

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_attempt_id, new_node_id
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.security,
]

BLOB = os.urandom(3000)


def _agent(node_id: str | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=client_ssl_context(node_id=node_id), timeout=10)


async def _mtls_round(rt: ControllerRuntime) -> None:
    assert rt.fetch_tokens is not None and rt.upload_grants is not None
    digest = sha256_bytes(BLOB)
    base = f"https://127.0.0.1:{rt.http_port}"
    url = f"{base}/v1/blobs/{digest.hex}"
    fabric = rt.settings.token_path.read_text().strip()
    async with httpx.AsyncClient(
        verify=client_ssl_context(node_id=None), timeout=10
    ) as cli:
        put = await cli.put(
            url, content=BLOB, headers={"authorization": f"Bearer {fabric}"}
        )
        assert put.status_code in (201, 200), put.text
    # plaintext on the same port is not served
    async with httpx.AsyncClient() as plain:
        with pytest.raises(httpx.HTTPError):
            await plain.get(f"http://127.0.0.1:{rt.http_port}/v1/blobs/{digest.hex}")

    node, other = str(new_node_id()), str(new_node_id())
    fetch = rt.fetch_tokens.mint(
        str(new_attempt_id()), [digest], ttl_s=60, node_id=node
    ).get_secret_value()
    up_data = os.urandom(1024)
    up_digest = sha256_bytes(up_data)
    grant = rt.upload_grants.mint(
        str(new_attempt_id()), ttl_s=60, max_blobs=4, max_bytes=1 << 20, node_id=node
    ).get_secret_value()
    for who in (None, other):
        async with _agent(who) as http:
            for method, target, token, content in (
                ("GET", url, fetch, None),
                ("HEAD", url, fetch, None),
                ("PUT", f"{base}/v1/blobs/{up_digest.hex}", grant, up_data),
                ("GET", f"{base}/v1/blobs/{up_digest.hex}/parts", grant, None),
            ):
                r = await http.request(
                    method,
                    target,
                    content=content,
                    headers={"authorization": f"Bearer {token}"},
                )
                assert r.status_code in (401, 404), (who, method, target, r.status_code)
                if method == "GET" and target == url:
                    assert r.status_code == 401 and r.content != BLOB
    async with _agent(node) as http:
        ok = await http.get(url, headers={"authorization": f"Bearer {fetch}"})
        assert ok.status_code == 200 and ok.content == BLOB
        # a cert alone is not a credential
        assert (await http.get(url)).status_code == 401


async def test_mtls_lock_survives_an_insecure_dev_run_on_the_same_database(
    pg_settings: DatabaseSettings,
) -> None:
    async with ControllerRuntime.start(make_settings(pg_settings)) as rt:
        await _mtls_round(rt)

    base = make_settings(pg_settings)
    dev = ControllerSettings(
        security={"mode": "insecure-dev"},
        controller=base.controller,
        database=base.database,
    )
    async with ControllerRuntime.start(dev) as rt:
        assert rt.fetch_tokens is not None
        digest = sha256_bytes(BLOB)
        url = f"http://127.0.0.1:{rt.http_port}/v1/blobs/{digest.hex}"
        token = rt.fetch_tokens.mint(
            str(new_attempt_id()), [digest], ttl_s=60, node_id=str(new_node_id())
        ).get_secret_value()
        async with httpx.AsyncClient(timeout=10) as http:
            got = await http.get(url, headers={"authorization": f"Bearer {token}"})
            assert got.status_code == 200 and got.content == BLOB
            for bad in ("Bearer nope", "", "Bearer " + token[:-2] + "xx"):
                r = await http.get(url, headers={"authorization": bad})
                assert r.status_code == 401, bad
            assert (await http.get(url)).status_code == 401

    async with ControllerRuntime.start(make_settings(pg_settings)) as rt:
        await _mtls_round(rt)
