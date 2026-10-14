"""V0.7 G7 (found by the criterion 3 control run): an ``insecure-dev``
controller whose data dir also holds a fabric CA served its blob port in
plain HTTP **and** demanded a node client certificate for fetch tokens and
upload grants - a certificate plain HTTP can never carry. Every agent fetch
was ``401 no_cert``: no job could stage.

Fixed: the node-certificate lock on ``/v1/blobs`` exists only when the port
really is TLS (``mtls``). In ``insecure-dev`` - loopback only, the agent's
claimed identity trusted by design (THREAT_MODEL CG-S7) - the token alone
decides, as before G3. In ``mtls`` nothing changes (CB-S1:
``test_v07_g3_https.py``).
"""

from __future__ import annotations

import os

import httpx
import pytest
from mf_controller_harness import make_settings

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_attempt_id, new_node_id
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

BLOB = os.urandom(2048)


async def test_insecure_dev_with_a_fabric_ca_serves_fetch_tokens_without_a_cert(
    pg_settings: DatabaseSettings,
) -> None:
    base = make_settings(pg_settings)  # the fixture CA is installed
    settings = ControllerSettings(
        security={"mode": "insecure-dev"},
        controller=base.controller,
        database=base.database,
    )
    async with ControllerRuntime.start(settings) as rt:
        assert rt.fabric is not None  # a CA is present - the case that broke
        digest = sha256_bytes(BLOB)
        url = f"http://127.0.0.1:{rt.http_port}/v1/blobs/{digest.hex}"
        token_path = rt.settings.token_path
        async with httpx.AsyncClient(timeout=10) as http:
            # store it with the fabric token (dev controller, no users)
            fabric = token_path.read_text().strip()
            put = await http.put(
                url, content=BLOB, headers={"authorization": f"Bearer {fabric}"}
            )
        assert rt.fetch_tokens is not None
        token = rt.fetch_tokens.mint(
            str(new_attempt_id()), [digest], ttl_s=60, node_id=str(new_node_id())
        ).get_secret_value()
        async with httpx.AsyncClient(timeout=10) as http:
            got = await http.get(url, headers={"authorization": f"Bearer {token}"})
            assert got.status_code == 200, (put.status_code, got.text)
            assert got.content == BLOB
            # the token is still the lock
            refused = await http.get(url, headers={"authorization": "Bearer nope"})
            assert refused.status_code == 401
