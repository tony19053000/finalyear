"""V0.5 G4 D97: small authorised blob request latency on loopback (controller
path and peer path) - no ~40 ms delayed-ACK stall.

Cause found on the controller path: the REST listener socket was created with
proto 0, accepted sockets inherit it, and asyncio sets TCP_NODELAY only when
``sock.proto == IPPROTO_TCP`` - so every response written as head + body waited
for the client's delayed ACK (measured 43 ms median before the fix, ~1.6 ms
after, Laptop 1). Not a benchmark claim: the bound is generous (median < 20 ms);
the numbers are printed (``-s``) for the handoff.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_jobs_harness import token_of

from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_attempt_id
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(60)]

BOUND_S = 0.020


async def _median(
    client: httpx.AsyncClient, url: str, token: str, *, method: str = "GET", n: int = 30
) -> float:
    headers = {"authorization": f"Bearer {token}"}
    r = await client.request(method, url, headers=headers)
    assert r.status_code == 200, r.status_code
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = await client.request(method, url, headers=headers)
        samples.append(time.perf_counter() - t0)
        assert r.status_code == 200
    samples.sort()
    return samples[len(samples) // 2]


async def test_controller_small_requests_do_not_wait_for_delayed_ack(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    data = b"small blob for latency\n"
    digest = sha256_bytes(data)
    assert runtime.fetch_tokens is not None
    base = f"https://127.0.0.1:{runtime.http_port}"
    async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
        put = await client.put(
            f"{base}/v1/blobs/{digest.hex}",
            content=data,
            headers={"authorization": f"Bearer {token_of(runtime)}"},
        )
        assert put.status_code in (200, 201), put.text
        token = runtime.fetch_tokens.mint(
            new_attempt_id(), [digest], ttl_s=60
        ).get_secret_value()
        get_s = await _median(client, f"{base}/v1/blobs/{digest.hex}", token)
        health_s = await _median(client, f"{base}/healthz", token)
    print(  # noqa: T201 - measured, for the handoff (run with -s)
        f"controller small GET median {get_s * 1000:.2f} ms; "
        f"/healthz median {health_s * 1000:.2f} ms (not a benchmark claim)"
    )
    assert get_s < BOUND_S
    assert health_s < BOUND_S
