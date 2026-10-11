"""V0.7 G4 deliverable 6 (D141): another user's job is **404, never 403**, on
every job route - enumerated from the live app's router, so a job route added
later without an entry here fails this file.

Real PostgreSQL, a real controller (HTTPS + wss), a real agent running the job.
Alice's job is checked twice by Bob: **while it runs** (cancel must not touch
it, the WebSocket follow must not subscribe) and **after it ended** (outputs
exist). Every answer Bob gets is byte-identical to the answer for a job id
that does not exist (no oracle), with every query-parameter variant the route
takes (``?attempt=`` with Alice's real attempt id, ``?stream=``, offsets).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Iterable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from fastapi import FastAPI
from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_users import bearer, create_user, login
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.routing import Route, WebSocketRoute
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from meshforge.common.ids import new_job_id
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

ALICE_PW = "alice-password-1"
BOB_PW = "bob-password-123"
ADMIN_PW = "admin-password-1"

# Every (method, path template) of the job surface. "WS" = the WebSocket.
# ``/v1/jobs`` (list, submit) has no job id: list must filter, submit's
# Idempotency-Key must not reach another user's job.
JOB_ROUTES = frozenset(
    {
        ("GET", "/v1/jobs"),
        ("POST", "/v1/jobs"),
        ("GET", "/v1/jobs/{job_id}"),
        ("POST", "/v1/jobs/{job_id}/cancel"),
        ("GET", "/v1/jobs/{job_id}/logs"),
        ("GET", "/v1/jobs/{job_id}/decisions"),
        ("WS", "/v1/jobs/{job_id}/logs/ws"),
    }
)

PROGRAM = """\
import os, sys, time
print("alice out", flush=True)
release = sys.argv[1]
deadline = time.monotonic() + 90
while not os.path.exists(release) and time.monotonic() < deadline:
    time.sleep(0.05)
os.makedirs("results", exist_ok=True)
open("results/r.txt", "w").write("secret result of alice\\n")
print("alice done", flush=True)
"""


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth=dict(USERS_ONLY),
        jobs={"dispatch_interval_s": 0.1},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir)
    yield running
    await running.stop()


def _walk(routes: Iterable[Any]) -> Iterator[Any]:
    """Leaf routes. FastAPI >= 0.141 wraps an included router in an object
    without ``path`` that holds the ``original_router``; older ones copy the
    routes - both shapes are walked."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _walk(inner.routes)
        else:
            yield route


def _routes_of(runtime: ControllerRuntime) -> set[tuple[str, str]]:
    """Every job route the running controller actually serves."""
    server = runtime._http_server  # the live app (private: test only)
    assert server is not None
    app = server.config.app
    assert isinstance(app, FastAPI)
    found: set[tuple[str, str]] = set()
    for route in _walk(app.routes):
        if not route.path.startswith("/v1/jobs"):
            continue
        if isinstance(route, WebSocketRoute):
            found.add(("WS", route.path))
        elif isinstance(route, Route):
            found.update((m, route.path) for m in route.methods or () if m != "HEAD")
    # Cross-check the HTTP half with the public OpenAPI document (it cannot
    # list WebSockets): the walk above did not miss a route.
    documented = {
        (method.upper(), path)
        for path, ops in app.openapi()["paths"].items()
        if path.startswith("/v1/jobs")
        for method in ops
    }
    assert documented == {r for r in found if r[0] != "WS"}, documented
    return found


async def _follow(
    runtime: ControllerRuntime, job_id: str, token: str, query: str = ""
) -> tuple[int | None, str | None, list[dict[str, Any]]]:
    url = f"wss://127.0.0.1:{runtime.http_port}/v1/jobs/{job_id}/logs/ws{query}"
    async with connect(url, additional_headers=bearer(token)) as ws:
        frames: list[dict[str, Any]] = []
        with contextlib.suppress(ConnectionClosed):
            while True:
                frames.append(json.loads(await ws.recv()))
        return ws.close_code, ws.close_reason, frames


async def _bob_sees_nothing(
    runtime: ControllerRuntime,
    bob: httpx.AsyncClient,
    bob_token: str,
    job_id: str,
    attempt_id: str,
) -> None:
    """Every job route, every variant: Bob's answer for Alice's job equals his
    answer for a job id nobody ever used."""
    missing = str(new_job_id())
    variants: list[tuple[str, str, dict[str, str]]] = [
        ("GET", "/v1/jobs/{}", {}),
        ("POST", "/v1/jobs/{}/cancel", {}),
        ("GET", "/v1/jobs/{}/logs", {}),
        ("GET", "/v1/jobs/{}/logs", {"stream": "stdout"}),
        ("GET", "/v1/jobs/{}/logs", {"attempt": attempt_id}),
        ("GET", "/v1/jobs/{}/decisions", {}),
        ("GET", "/v1/jobs/{}/decisions", {"attempt": "1"}),
    ]
    for method, template, params in variants:
        theirs = await bob.request(method, template.format(job_id), params=params)
        none = await bob.request(method, template.format(missing), params=params)
        assert theirs.status_code == 404, (method, template, params, theirs.text)
        assert theirs.json() == none.json(), (method, template, params)
        assert job_id not in theirs.text and attempt_id not in theirs.text
    for query in ("", "?stream=stdout", "?stdout_offset=0&stderr_offset=0"):
        theirs_ws = await _follow(runtime, job_id, bob_token, query)
        none_ws = await _follow(runtime, missing, bob_token, query)
        assert theirs_ws == none_ws, query
        assert theirs_ws[1] == "job_not_found" and theirs_ws[2] == []
    for state in (None, "running", "succeeded"):
        params = {} if state is None else {"state": state}
        listed = (await bob.get("/v1/jobs", params=params)).json()
        assert job_id not in {j["job_id"] for j in listed["jobs"]}, params


async def test_another_users_job_is_404_on_every_route_while_running_and_after(
    runtime: ControllerRuntime,
    engine: AsyncEngine,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    # The table above is complete: the live router has exactly these routes.
    assert _routes_of(runtime) == set(JOB_ROUTES)

    await create_user(engine, "admin", ADMIN_PW, admin=True)
    await create_user(engine, "alice", ALICE_PW)
    await create_user(engine, "bob", BOB_PW)
    base = f"https://127.0.0.1:{runtime.http_port}"
    release = tmp_path / "release"
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(PROGRAM)
    manifest = {
        "name": "alices",
        "entrypoint": ["python", "main.py", str(release)],
        "outputs": ["results/"],
    }
    async with httpx.AsyncClient(base_url=base, timeout=30) as anon:
        tokens = {
            name: await login(anon, name, pw)
            for name, pw in (("alice", ALICE_PW), ("bob", BOB_PW), ("admin", ADMIN_PW))
        }
    async with (
        httpx.AsyncClient(base_url=base, headers=bearer(tokens["alice"]), timeout=30)
        as alice,
        httpx.AsyncClient(base_url=base, headers=bearer(tokens["bob"]), timeout=30)
        as bob,
        httpx.AsyncClient(base_url=base, headers=bearer(tokens["admin"]), timeout=30)
        as admin,
    ):  # fmt: skip
        job_id = await submit(
            alice, project, agent.node_id, manifest, idempotency_key="alice-k"
        )
        running = await wait_job(
            alice, job_id, states=frozenset({"running"}), timeout_s=60
        )
        attempt_id = running["attempts"][-1]["attempt_id"]

        # 1. While it runs: nothing, and Bob's cancel did not cancel it.
        await _bob_sees_nothing(runtime, bob, tokens["bob"], job_id, attempt_id)
        still = (await alice.get(f"/v1/jobs/{job_id}")).json()
        assert still["state"] == "running", still
        # Submit with Alice's Idempotency-Key: a conflict, not Alice's job.
        conflict = await bob.post(
            "/v1/jobs",
            json={
                "manifest": manifest,
                "bundle_digest": still["bundle_digest"],
                "placement": {"node_id": agent.node_id},
            },
            headers={"idempotency-key": "alice-k"},
        )
        assert conflict.status_code == 409 and job_id not in conflict.text

        release.write_text("go")
        done = await wait_job(alice, job_id, timeout_s=90)
        assert done["state"] == "succeeded", done
        assert done["attempts"][-1]["attempt_id"] == attempt_id  # never cancelled

        # 2. After it ended (logs final, outputs stored).
        await _bob_sees_nothing(runtime, bob, tokens["bob"], job_id, attempt_id)
        digest = done["attempts"][-1]["output_digest"].removeprefix("sha256:")
        absent = "0" * 64
        theirs = await bob.get(f"/v1/blobs/{digest}")
        none = await bob.get(f"/v1/blobs/{absent}")
        assert theirs.status_code == 404 and theirs.json() == none.json()

        # The owner and the admin see all of it (the 404s above are the rule,
        # not a broken route).
        for client, token in ((alice, tokens["alice"]), (admin, tokens["admin"])):
            assert (await client.get(f"/v1/jobs/{job_id}")).status_code == 200
            logs = await client.get(
                f"/v1/jobs/{job_id}/logs", params={"attempt": attempt_id}
            )
            assert logs.status_code == 200 and b"alice done" in logs.content
            assert (
                await client.get(f"/v1/jobs/{job_id}/decisions", params={"attempt": 1})
            ).status_code == 200
            assert (await client.get(f"/v1/blobs/{digest}")).status_code == 200
            _code, _reason, frames = await _follow(runtime, job_id, token)
            assert frames and frames[-1].get("end")
            listed = (await client.get("/v1/jobs")).json()
            assert job_id in {j["job_id"] for j in listed["jobs"]}
