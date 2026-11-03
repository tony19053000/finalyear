"""V1.0 G8 F4 (lab L4): a joined agent whose mDNS finds nothing still reaches its
controller, and a controller that moved (DHCP) is followed through mDNS.

Real PostgreSQL, the real controller runtime (fabric CA, TLS enrolment listener,
mTLS session port), the real ``meshforge-agent join`` flow and the real
``AgentSessionClient``. "mDNS finds nothing" is real too: the agent browses a
service type nobody advertises (on a host without multicast zeroconf cannot
even open its socket - also "nothing"). The DHCP test needs working mDNS on
this host and skips with a reason otherwise.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import grpc
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("zeroconf", reason="needs the agent/controller extra")

from mf_agent_harness import make_agent_settings
from mf_controller_harness import make_settings

from meshforge.agent import known_controller
from meshforge.agent.enrollment import join
from meshforge.agent.identity import load_or_create
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import AgentSettings
from meshforge.controller.enrollment.codes import issue_code
from meshforge.controller.enrollment.invites import InviteStore
from meshforge.controller.fabric_ca import create_fabric
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(180),
]


def _service_type() -> str:
    # Unique per test: a real controller on the developer's LAN never answers.
    return f"_mf-{uuid.uuid4().hex[:8]}._tcp.local."


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _mdns_works(timeout_s: float = 2.0) -> bool:
    """Register a throw-away record and see our own browse find it."""
    from zeroconf import ServiceStateChange, Zeroconf
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    probe_type = "_mfprobe._tcp.local."
    probe_name = f"probe-{uuid.uuid4().hex[:8]}.{probe_type}"
    found: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def on_change(
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if (
            state_change is ServiceStateChange.Added
            and name.lower() == probe_name.lower()
            and not found.done()
        ):
            found.set_result(name)

    try:
        azc = AsyncZeroconf()
        browser_zc = AsyncZeroconf()
    except OSError:
        return False
    info = AsyncServiceInfo(
        probe_type,
        probe_name,
        addresses=[socket.inet_aton("127.0.0.1")],
        port=1,
        server=f"{socket.gethostname()}.local.",
    )
    browser = AsyncServiceBrowser(browser_zc.zeroconf, probe_type, handlers=[on_change])
    try:
        await azc.async_register_service(info)
        await asyncio.wait_for(found, timeout_s)
        return True
    except (TimeoutError, OSError):
        return False
    finally:
        await browser.async_cancel()
        await azc.async_unregister_all_services()
        await azc.async_close()
        await browser_zc.async_close()


def _controller_settings(
    pg_settings: DatabaseSettings, **overrides: Any
) -> ControllerSettings:
    """A fresh fabric CA plus a TLS enrolment listener on loopback."""
    settings = make_settings(
        pg_settings,
        fabric=False,
        enroll={"bind": "127.0.0.1:0", "rate_per_minute": 600.0, "rate_burst": 100},
        **overrides,
    )
    create_fabric(settings.ca_dir)
    return settings


@pytest_asyncio.fixture
async def invites(pg_settings: DatabaseSettings) -> AsyncIterator[InviteStore]:
    engine = create_engine(pg_settings)
    try:
        yield InviteStore(engine)
    finally:
        await engine.dispose()


async def _join(
    rt: ControllerRuntime, invites: InviteStore, settings: AgentSettings
) -> str | None:
    """A real ``meshforge-agent join`` with a fresh code; the session address."""
    new = await invites.create(ttl_s=900, actor="test", replace_node_id=None)
    assert rt.fabric is not None
    code = issue_code(new, fabric=rt.fabric, host="127.0.0.1", port=rt.enroll_port)
    identity = load_or_create(settings.identity_dir)
    result = await asyncio.to_thread(join, identity, code.code)
    return result.session_address


def _mdns_only_agent(data_dir: Path, *, service_type: str) -> AgentSettings:
    return make_agent_settings(
        data_dir,
        "",  # no controller_address: mDNS, then the last-known endpoint
        enroll=False,
        discovery={"enabled": True, "timeout_s": 0.3, "service_type": service_type},
    )


async def _run(
    settings: AgentSettings,
) -> tuple[AgentSessionClient, asyncio.Task[None]]:
    client = AgentSessionClient(settings, load_or_create(settings.identity_dir))
    return client, asyncio.create_task(client.run())


async def _stop(client: AgentSessionClient, task: asyncio.Task[None]) -> None:
    client.request_stop()
    await asyncio.wait_for(task, 15)


async def test_joined_agent_with_silent_mdns_connects_to_the_enrolled_endpoint(
    pg_settings: DatabaseSettings, invites: InviteStore, tmp_path: Path
) -> None:
    """The L4 failure: joined fine, mDNS answers nothing -> must still come ONLINE."""
    settings = _controller_settings(pg_settings)
    async with ControllerRuntime.start(settings) as rt:
        agent = _mdns_only_agent(tmp_path / "agent", service_type=_service_type())
        # The enrolment listener names the session port: the join stores
        # "<host the code named>:<session port>", not the enrolment port.
        expected = f"127.0.0.1:{rt.grpc_port}"
        assert rt.enroll_port != rt.grpc_port
        assert await _join(rt, invites, agent) == expected
        known = known_controller.load_known(agent.identity_dir)
        assert known is not None
        assert (known.address, known.source) == (expected, "join")

        client, task = await _run(agent)
        try:
            await client.wait_for_state(AgentState.ONLINE, timeout_s=30)
            assert client.controller_target == expected
            assert rt.manager is not None
            assert rt.manager.get(client.node_id) is not None
        finally:
            await _stop(client, task)
        # An unchanged endpoint is not rewritten by the accepted session.
        after = known_controller.load_known(agent.identity_dir)
        assert after is not None and after.source == "join"


async def test_stale_hint_and_silent_mdns_backs_off_then_follows_a_new_hint(
    pg_settings: DatabaseSettings, invites: InviteStore, tmp_path: Path
) -> None:
    """A wrong hint is just a failed dial (BACKOFF, retried), never fatal; the
    hint is re-read every cycle."""
    settings = _controller_settings(pg_settings)
    async with ControllerRuntime.start(settings) as rt:
        agent = _mdns_only_agent(tmp_path / "agent", service_type=_service_type())
        assert await _join(rt, invites, agent) is not None
        stale = f"127.0.0.1:{_closed_port()}"
        assert known_controller.remember(agent.identity_dir, stale, source="session")

        client, task = await _run(agent)
        try:
            for _ in range(600):
                if client.stats.cycles >= 2:
                    break
                await asyncio.sleep(0.05)
            assert client.stats.cycles >= 2
            assert client.stats.sessions_accepted == 0
            assert not task.done()  # still trying, not crashed
            known_controller.remember(
                agent.identity_dir, f"127.0.0.1:{rt.grpc_port}", source="session"
            )
            await client.wait_for_state(AgentState.ONLINE, timeout_s=30)
        finally:
            await _stop(client, task)


async def test_controller_moved_mdns_finds_it_and_the_hint_follows(
    pg_settings: DatabaseSettings, invites: InviteStore, tmp_path: Path
) -> None:
    """DHCP gave the controller a new address: the stored hint is stale, mDNS
    has the new one - mDNS wins, and the accepted session rewrites the hint."""
    if not await _mdns_works():
        pytest.skip("mDNS unavailable on this host (no multicast-capable interface)")
    service_type = _service_type()
    settings = _controller_settings(
        pg_settings,
        discovery={"advertise": True, "service_type": service_type},
    )
    async with ControllerRuntime.start(settings) as rt:
        assert rt._advertiser is not None and rt._advertiser.registered
        agent = make_agent_settings(
            tmp_path / "agent",
            "",
            enroll=False,
            discovery={"enabled": True, "timeout_s": 4.0, "service_type": service_type},
        )
        assert await _join(rt, invites, agent) is not None
        old = f"127.0.0.1:{_closed_port()}"  # where the controller used to be
        assert known_controller.remember(agent.identity_dir, old, source="session")

        client, task = await _run(agent)
        try:
            await client.wait_for_state(AgentState.ONLINE, timeout_s=30)
            new = f"127.0.0.1:{rt.grpc_port}"
            assert client.controller_target == new
            for _ in range(200):
                known = known_controller.load_known(agent.identity_dir)
                if known is not None and known.address == new:
                    break
                await asyncio.sleep(0.05)
            assert known is not None
            assert (known.address, known.source) == (new, "session")
        finally:
            await _stop(client, task)


async def test_controller_address_at_the_join_port_logs_a_clear_hint(
    pg_settings: DatabaseSettings, invites: InviteStore, tmp_path: Path
) -> None:
    """The join code names the enrolment port; an owner who copies it into
    ``controller_address`` gets a hint naming the session port, not a silent
    reconnect loop. Still retried (never fatal)."""
    import structlog

    settings = _controller_settings(pg_settings)
    async with ControllerRuntime.start(settings) as rt:
        agent = _mdns_only_agent(tmp_path / "agent", service_type=_service_type())
        assert await _join(rt, invites, agent) is not None
        wrong = make_agent_settings(
            tmp_path / "agent", f"127.0.0.1:{rt.enroll_port}", enroll=False
        )
        with structlog.testing.capture_logs() as logs:
            client, task = await _run(wrong)
            try:
                for _ in range(600):
                    if len(client.stats.stream_ends) >= 2:
                        break
                    await asyncio.sleep(0.05)
                # Snapshot BEFORE stopping: ``request_stop`` can land while the
                # next retry waits for its first reply, and that stream then
                # ends CANCELLED by the agent itself (``_close_call``, appended
                # to ``stream_ends`` too) - the client's own shutdown, not what
                # the server answered (F6: seen under CI load).
                before_stop = list(client.stats.stream_ends)
                assert not task.done()  # retried, never fatal
            finally:
                await _stop(client, task)
        assert len(before_stop) >= 2, before_stop  # at least one retry happened
        codes = {e.code for e in before_stop}
        assert codes == {grpc.StatusCode.UNIMPLEMENTED}, codes
        hints = [e for e in logs if e["event"] == "agent.session.not_a_session_port"]
        assert len(hints) == 1  # once per address, not once per retry
        assert hints[0]["controller_address"] == f"127.0.0.1:{rt.enroll_port}"
        assert "session port" in hints[0]["hint"]
        assert client.stats.sessions_accepted == 0
