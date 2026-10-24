"""V0.17 G3 (0.17.4/0.17.5, D391/D392): the agent's local console end to end -
real PostgreSQL, the real controller runtime (fabric CA, TLS enrolment
listener, mTLS sessions) and the real agent process body (``run_with_console``:
uvicorn on 127.0.0.1, the supervisor, the real ``serve``).

The owner's path through the console, over real HTTP:

* the launcher's trade: token file -> ``POST /launch-code`` (Bearer) -> one-time
  code -> ``POST /session`` (Origin) -> HttpOnly cookie; the code works once;
* before the join: ``waiting_for_join``, the fabric part is not running;
* node name set in the console -> sent in the first Hello -> stored in
  ``node.display_name``;
* ``POST /join`` with a used code -> plain ``used`` error, nothing changes;
  with a fresh code -> the supervisor starts the fabric part by itself and the
  node is ONLINE on the controller (no restart, no path unit);
* ``PUT /policy`` -> the controller's live copy changes within the reload
  interval (the same live file as ``meshforge-agent policy set``);
* ``POST /restart`` -> a new session (in-process), the node comes back;
* ``POST /leave`` -> the session ends, the identity is gone, the console is
  back at the wizard; the process (and the console session) stayed up.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("fastapi", reason="needs the agent extra")

from mf_agent_harness import AGENT_RECONNECT
from mf_controller_harness import make_settings
from sqlalchemy import text

from meshforge.agent.console.logbuffer import install_log_buffer
from meshforge.agent.console.server import run_with_console
from meshforge.agent.console.supervisor import FabricPhase, Supervisor
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
    pytest.mark.timeout(240),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _until(check: Callable[[], Any], within_s: float = 20.0) -> Any:
    start = time.monotonic()
    while True:
        value = check()
        if value:
            return value
        assert time.monotonic() - start < within_s, "condition not reached"
        await asyncio.sleep(0.05)


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = make_settings(
        pg_settings,
        fabric=False,
        enroll={"bind": "127.0.0.1:0", "rate_per_minute": 600.0, "rate_burst": 100},
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


async def _display_name(pg: DatabaseSettings, node_id: str) -> str | None:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text("select display_name from node where node_id = :n"),
                    {"n": node_id},
                )
            ).first()
    finally:
        await engine.dispose()
    return None if row is None else str(row[0])


async def test_owner_joins_sets_policy_restarts_and_leaves_from_the_console(
    runtime: ControllerRuntime,
    invites: InviteStore,
    pg_settings: DatabaseSettings,
    tmp_path: Path,
) -> None:
    port = _free_port()
    token_dir = tmp_path / "console"
    agent = AgentSection(
        controller_address=f"127.0.0.1:{runtime.grpc_port}",
        hello_timeout_s=2.0,
        identity_lock_timeout_s=0.5,
        reconnect=dict(AGENT_RECONNECT),
        console={"enabled": True, "port": port, "token_dir": token_dir},
        policy={"reload_interval_s": 0.2},
    )
    settings = AgentSettings(data_dir=tmp_path / "data", agent=agent)
    supervisor = Supervisor(settings, install_signals=False, poll_s=0.2)
    logs_buffer = install_log_buffer(5000)
    task = asyncio.create_task(
        run_with_console(settings, supervisor=supervisor, logs=logs_buffer)
    )
    base = f"http://127.0.0.1:{port}"
    origin = {"origin": base}
    try:
        token_file = await _until(
            lambda: (token_dir / "token").is_file() and token_dir / "token"
        )
        token = token_file.read_text().strip()
        async with httpx.AsyncClient(base_url=base, timeout=30) as http:
            await _until(lambda: True)
            # --- the launcher's trade, then the page's
            launch = await http.post(
                "/console/v1/launch-code", headers={"authorization": f"Bearer {token}"}
            )
            assert launch.status_code == 200, launch.text
            code = launch.json()["code"]
            assert launch.json()["url"] == f"{base}/#c={code}"
            assert (await http.get("/console/v1/status")).status_code == 401
            signed = await http.post(
                "/console/v1/session", json={"code": code}, headers=origin
            )
            assert signed.status_code == 200, signed.text
            assert "httponly" in signed.headers["set-cookie"].lower()
            assert "samesite=strict" in signed.headers["set-cookie"].lower()
            again = await http.post(
                "/console/v1/session", json={"code": code}, headers=origin
            )
            assert again.status_code == 401  # single use
            http.cookies.update(signed.cookies)

            status = (await http.get("/console/v1/status")).json()
            assert status["phase"] == "waiting_for_join" and status["joined"] is False

            named = await http.put(
                "/console/v1/node-name", json={"name": "Lab A"}, headers=origin
            )
            assert named.status_code == 200 and named.json()["source"] == "console"

            # --- a used code: plain words, nothing changes
            used = await _code(runtime, invites)
            from meshforge.agent.enrollment import join as cli_join
            from meshforge.agent.identity import load_or_create

            other = load_or_create(tmp_path / "other" / "identity")
            await asyncio.to_thread(cli_join, other, used)
            refused = await http.post(
                "/console/v1/join", json={"code": used}, headers=origin
            )
            assert refused.status_code == 400
            assert refused.json()["error"]["code"] == "used", refused.text
            assert (await http.get("/console/v1/status")).json()["joined"] is False

            # --- a fresh code: joined, fabric part started by the supervisor
            joined = await http.post(
                "/console/v1/join",
                json={"code": await _code(runtime, invites)},
                headers=origin,
            )
            assert joined.status_code == 200, joined.text
            node_id = joined.json()["node_id"]
            await supervisor.wait_phase(FabricPhase.RUNNING, timeout_s=30)

            async def online() -> dict[str, Any]:
                for _ in range(400):
                    body = (await http.get("/console/v1/status")).json()
                    if body["connection"] == "ONLINE":
                        return dict(body)
                    await asyncio.sleep(0.05)
                raise AssertionError(f"never ONLINE: {body}")

            body = await online()
            assert body["joined"] is True and body["node_id"] == node_id
            assert body["dashboard_url"] == "https://127.0.0.1:47110/"
            assert await _display_name(pg_settings, node_id) == "Lab A"

            # --- policy from the console reaches the controller live
            def controller_policy() -> Any:
                live = runtime.manager.get(node_id) if runtime.manager else None
                return None if live is None else live.policy

            assert controller_policy() is not None
            put = await http.put(
                "/console/v1/policy",
                json={"values": {"accept_jobs": True, "max_cpu_percent": 40}},
                headers=origin,
            )
            assert put.status_code == 200, put.text
            assert put.json()["restart_needed"] == ["accept_jobs"]
            await _until(
                lambda: (
                    (p := controller_policy()) is not None
                    and p.accept_jobs
                    and p.max_cpu_percent == 40
                )
            )
            # CSRF: the same write without Origin is refused
            no_origin = await http.put(
                "/console/v1/policy", json={"values": {"accept_jobs": False}}
            )
            assert no_origin.status_code == 403

            # --- in-process restart: a new session, same process, same cookie
            first_session = body["session_id"]
            restarted = await http.post("/console/v1/restart", headers=origin)
            assert restarted.status_code == 200
            for _ in range(400):
                body = (await http.get("/console/v1/status")).json()
                if (
                    body["connection"] == "ONLINE"
                    and body["session_id"] != first_session
                ):
                    break
                await asyncio.sleep(0.05)
            assert body["session_id"] != first_session, body

            # --- leave: identity gone, back to the wizard, console still up
            assert (
                await http.post("/console/v1/leave", json={}, headers=origin)
            ).status_code == 422
            left = await http.post(
                "/console/v1/leave", json={"confirm": "leave"}, headers=origin
            )
            assert left.status_code == 200
            await supervisor.wait_phase(FabricPhase.WAITING_FOR_JOIN, timeout_s=30)
            body = (await http.get("/console/v1/status")).json()
            assert body["joined"] is False and body["setup_complete"] is False
            assert not (settings.identity_dir / "node.key").exists()
            await _until(lambda: runtime.manager.get(node_id) is None)
            logs = (await http.get("/console/v1/logs?limit=5000")).json()["lines"]
            events = [line["event"] for line in logs]
            assert "agent.console.left_fabric" in events
            assert token not in str(logs) and code not in str(logs)
    finally:
        supervisor.request_shutdown()
        try:
            assert await asyncio.wait_for(task, 60) == ExitCode.OK
        finally:
            logging.getLogger().removeHandler(logs_buffer)
    assert not (token_dir / "token").exists()  # removed on the way out
