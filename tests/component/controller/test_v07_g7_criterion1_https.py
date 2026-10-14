"""V0.7 G7 criterion 1 sweep: every listener refuses an unacceptable node
certificate. This file adds the two 47110 (REST + blobs) cells that had no
test - a certificate of **another CA** and an **expired** one.

The whole matrix (one cell = one existing test; nothing is duplicated) is in
``docs/THREAT_MODEL.md`` §8 "Criterion 1 matrix".

On 47110 the client certificate is optional (the CLI has none) but, when one
is presented, it must be valid for this fabric: a certificate of another CA
or an expired one never gets a fetch token's blob - the handshake fails or
the request is ``401``, and not a byte of the blob comes back. A positive
control shows the same request with a valid certificate works.
"""

from __future__ import annotations

import datetime as dt
import os
import ssl
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from mf_controller_harness import make_settings
from mf_forged_certs import Forged, forge
from mf_jobs_harness import token_of

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common import pki
from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_attempt_id, new_fabric_id
from meshforge.controller.fabric_ca import create_fabric
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.security,
]

BLOB = os.urandom(3000)


@pytest_asyncio.fixture
async def past_runtime(
    pg_settings: DatabaseSettings,
) -> AsyncIterator[tuple[ControllerRuntime, pki.CertificateAuthority]]:
    """A controller whose CA dates from yesterday, so it can issue an
    already expired node certificate (as ``test_v07_g3_mtls_session``)."""
    settings = make_settings(pg_settings, fabric=False)
    fabric = create_fabric(
        settings.ca_dir, now=dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    )
    async with ControllerRuntime.start(settings) as runtime:
        yield runtime, fabric.authority


def _context(ca: pki.CertificateAuthority, forged: Forged | None) -> ssl.SSLContext:
    context = ssl.create_default_context(
        cadata=pki.cert_to_pem(ca.cert).decode("ascii")
    )
    if forged is not None:
        folder = Path(tempfile.mkdtemp(prefix="mf-g7-c1-"))
        cert, key = folder / "c.pem", folder / "k.pem"
        cert.write_bytes(forged.cert_pem)
        key.write_bytes(forged.key_pem)
        context.load_cert_chain(cert, key)
    return context


async def _fetch(
    runtime: ControllerRuntime, ca: pki.CertificateAuthority, forged: Forged
) -> httpx.Response | None:
    """GET a stored blob with a fetch token minted for ``forged``'s node,
    presenting ``forged``; ``None`` = refused at the handshake."""
    digest = sha256_bytes(BLOB)
    base = f"https://127.0.0.1:{runtime.http_port}"
    async with httpx.AsyncClient(
        base_url=base,
        verify=_context(ca, None),
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=10,
    ) as cli:
        put = await cli.put(f"/v1/blobs/{digest.hex}", content=BLOB)
        assert put.status_code in (200, 201), put.text
    assert runtime.fetch_tokens is not None
    token = runtime.fetch_tokens.mint(
        str(new_attempt_id()), [digest], ttl_s=60, node_id=forged.node_id
    ).get_secret_value()
    async with httpx.AsyncClient(verify=_context(ca, forged), timeout=10) as http:
        try:
            return await http.get(
                f"{base}/v1/blobs/{digest.hex}",
                headers={"authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError:
            return None


def _assert_refused(response: httpx.Response | None) -> None:
    if response is not None:
        assert response.status_code == 401, response.text
        assert BLOB not in response.content


async def test_a_certificate_from_another_ca_gets_nothing_on_the_blob_port(
    past_runtime: tuple[ControllerRuntime, pki.CertificateAuthority],
) -> None:
    runtime, ca = past_runtime
    stranger = forge(ca=pki.create_ca(new_fabric_id()))
    _assert_refused(await _fetch(runtime, ca, stranger))


async def test_an_expired_certificate_gets_nothing_on_the_blob_port(
    past_runtime: tuple[ControllerRuntime, pki.CertificateAuthority],
) -> None:
    runtime, ca = past_runtime
    expired = forge(
        ca=ca,
        now=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
        lifetime=dt.timedelta(minutes=30),
    )
    _assert_refused(await _fetch(runtime, ca, expired))


async def test_positive_control_a_valid_certificate_gets_the_blob(
    past_runtime: tuple[ControllerRuntime, pki.CertificateAuthority],
) -> None:
    """The refusals above are about the certificate, not the request."""
    runtime, ca = past_runtime
    response = await _fetch(runtime, ca, forge(ca=ca))
    assert response is not None
    assert response.status_code == 200 and response.content == BLOB
