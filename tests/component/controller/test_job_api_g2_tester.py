"""Tester round 1 (V0.4 G2): job/blob API edges beyond the implementer's suite.

Real controller (uvicorn + real Postgres), real HTTP. Focus: (1) concurrent
duplicate submissions with the SAME Idempotency-Key but DIFFERENT bodies under a
real race (the existing suite only tests this serially), (2) auth surface tricks
(percent-encoded route, trailing slash, method-override header, unknown path),
(3) the token never appearing in the controller's structured logs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import grpc
import pytest
import pytest_asyncio
import structlog.testing
from mf_fabric import NodeChannels
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

from meshforge.common.hashing import sha256_bytes
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

GIB = 1024**3
BUNDLE = b"g2-tester-bundle-bytes" * 100


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    async with running_controller(g2_settings(pg_settings)) as rt:
        yield rt


@pytest_asyncio.fixture
async def channel(runtime: ControllerRuntime) -> AsyncIterator[grpc.aio.Channel]:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as ch:
        yield ch


@pytest_asyncio.fixture
async def agent(channel: grpc.aio.Channel) -> AsyncIterator[FakeAgent]:
    fa = FakeAgent(channel)
    hardware = agent_pb2.HardwareSummary(
        cpu_model="test",
        cpu_cores=4,
        cpu_threads=8,
        ram_bytes=16 * GIB,
        capabilities=["exec"],
    )
    await fa.connect(hardware=hardware)
    yield fa
    await fa.close()


@pytest_asyncio.fixture
async def bundle(runtime: ControllerRuntime) -> str:
    digest = sha256_bytes(BUNDLE)
    async with api_client(runtime, token=token_of(runtime)) as client:
        put = await client.put(f"/v1/blobs/{digest.hex}", content=BUNDLE)
        assert put.status_code == 201
    return str(digest)


def _body(bundle: str, node_id: str, **manifest: Any) -> dict[str, Any]:
    return {
        "manifest": {"name": "demo", "entrypoint": "python main.py", **manifest},
        "bundle_digest": bundle,
        "placement": {"node_id": node_id},
    }


async def _job_count(runtime: ControllerRuntime) -> int:
    assert runtime.jobs is not None
    return len(await runtime.jobs.list_jobs(limit=1000))


# ----------------------------------------------- concurrent conflicting keys


async def test_concurrent_same_key_different_bodies_exactly_one_wins(
    runtime: ControllerRuntime, agent: FakeAgent, bundle: str
) -> None:
    """N racing POSTs, same Idempotency-Key, genuinely DIFFERENT request bodies
    (different entrypoints -> different request_hash). Exactly one must be
    accepted (201); every other request -- whichever body it carried -- must be
    409 conflict, never silently treated as a replay of a different job's
    content. Only one job row ever exists."""
    key = {"Idempotency-Key": "racing-conflict-key"}
    async with api_client(runtime, token=token_of(runtime)) as client:
        bodies = [
            _body(bundle, agent.node_id, entrypoint=f"python main.py --run {i}")
            for i in range(8)
        ]
        responses = await asyncio.gather(
            *[client.post("/v1/jobs", json=b, headers=key) for b in bodies]
        )
    codes = sorted(r.status_code for r in responses)
    assert codes == [201] + [409] * 7, [r.text for r in responses]
    for r in responses:
        if r.status_code == 409:
            assert r.json()["error"]["code"] == "conflict"
    assert await _job_count(runtime) == 1


async def test_concurrent_racers_where_some_share_a_body_and_others_do_not(
    runtime: ControllerRuntime, agent: FakeAgent, bundle: str
) -> None:
    """Four racers share one body (a genuine retry storm from one caller), four
    others each carry a distinct body under the SAME key (a bug in a different
    caller reusing the key). Whichever racer wins the race for the row is
    unspecified (real concurrency) -- the invariant under test is: every request
    that shares a body with the actual winner gets 200/201 with the winner's
    job_id, every request that does not gets 409, and exactly one job exists."""
    key = {"Idempotency-Key": "mixed-racing-key"}
    matching = _body(bundle, agent.node_id, entrypoint="python main.py --same")
    conflicting = [
        _body(bundle, agent.node_id, entrypoint=f"python main.py --other {i}")
        for i in range(4)
    ]
    requests = [matching] * 4 + conflicting
    async with api_client(runtime, token=token_of(runtime)) as client:
        responses = await asyncio.gather(
            *[client.post("/v1/jobs", json=b, headers=key) for b in requests]
        )
    winners = [i for i, r in enumerate(responses) if r.status_code in (200, 201)]
    assert winners, [r.status_code for r in responses]
    winning_body = requests[winners[0]]
    expected_winner_count = sum(1 for b in requests if b == winning_body)
    assert len(winners) == expected_winner_count
    for i in winners:
        assert requests[i] == winning_body
    job_ids = {responses[i].json()["job_id"] for i in winners}
    assert len(job_ids) == 1
    assert sorted(responses[i].status_code for i in winners).count(201) == 1
    losers = [i for i in range(len(requests)) if i not in winners]
    for i in losers:
        assert responses[i].status_code == 409, responses[i].text
        assert requests[i] != winning_body
    assert await _job_count(runtime) == 1


# ------------------------------------------------------------- auth tricks


async def test_unknown_mutating_path_is_not_a_silent_bypass(
    runtime: ControllerRuntime,
) -> None:
    """POST to a path with no route: FastAPI's app-wide auth dependency only
    fires for MATCHED routes, so an unmatched path is a plain 404 -- confirm
    this is really a 404 (nothing executed), never a 200 or a leaked 500."""
    async with api_client(runtime) as anon:
        response = await anon.post("/v1/jobs/does-not-exist-as-a-route/sub")
        assert response.status_code == 404


async def test_trailing_slash_on_a_protected_route_still_needs_the_token(
    runtime: ControllerRuntime,
) -> None:
    async with api_client(runtime) as anon:
        response = await anon.post("/v1/jobs/", json={})
        # Either FastAPI 404s it (no route with a trailing slash) or it matches
        # and enforces auth -- either way, it must NOT be treated as an
        # authenticated 200/201.
        assert response.status_code in (401, 404, 405, 307, 308)
        if response.status_code in (401, 404, 405):
            pass
        else:  # a redirect must not itself have created anything
            pass
    assert await _job_count(runtime) == 0


async def test_percent_encoded_path_still_requires_auth(
    runtime: ControllerRuntime,
) -> None:
    """%6A = 'j': '/v1/%6Aobs' is the percent-encoded spelling of '/v1/jobs'.
    Whether the ASGI server normalises it to match the route or not, it must
    never be treated as an authenticated call that bypasses the token check."""
    async with api_client(runtime) as anon:
        response = await anon.post("/v1/%6Aobs", json={})
        assert response.status_code in (401, 404)
    assert await _job_count(runtime) == 0


async def test_method_override_header_does_not_bypass_auth(
    runtime: ControllerRuntime, agent: FakeAgent, bundle: str
) -> None:
    """A common reverse-proxy convention (X-HTTP-Method-Override) must not let an
    unauthenticated GET-shaped request masquerade as a POST that skips the
    mutation guard -- the guard keys off the real HTTP method only."""
    async with api_client(runtime) as anon:
        response = await anon.get(
            "/v1/jobs",
            headers={"X-HTTP-Method-Override": "POST"},
        )
        # Reads need the token too (D44), so this is 401 either way -- the
        # meaningful assertion is that supplying the header via POST doesn't
        # somehow get treated as a safe method and skip the guard.
        assert response.status_code == 401
        response = await anon.request(
            "POST",
            "/v1/jobs",
            json=_body(bundle, agent.node_id),
            headers={"X-HTTP-Method-Override": "GET"},
        )
        assert response.status_code == 401
    assert await _job_count(runtime) == 0


async def test_token_never_appears_in_controller_logs(
    runtime: ControllerRuntime, agent: FakeAgent, bundle: str
) -> None:
    token = token_of(runtime)
    with structlog.testing.capture_logs() as logs:
        async with api_client(runtime, token=token) as client:
            created = await client.post(
                "/v1/jobs",
                json=_body(bundle, agent.node_id),
                headers={"Idempotency-Key": "log-check-key"},
            )
            assert created.status_code == 201, created.text
            wrong = await client.get(
                "/v1/jobs", headers={"authorization": f"Bearer {token}x"}
            )
        # also try with a definitely-wrong token to be sure failures don't log it
        async with api_client(runtime, token=token + "wrong") as bad_client:
            await bad_client.get("/v1/jobs")
    rendered = "\n".join(repr(entry) for entry in logs)
    assert token not in rendered
    assert wrong.status_code == 401
