"""V0.3 G3 (0.3.9): an agent with an empty ``controller_address`` finds an advertising
controller runtime over real mDNS on this host and comes ONLINE.

mDNS needs a multicast-capable interface, which sandboxes and some CI runners do
not have. The module probes first (register + browse a throw-away record within
2 s) and skips with a reason when that fails; a controller that cannot advertise
must still serve, which is asserted separately.
"""

from __future__ import annotations

import asyncio
import socket
import uuid

import pytest
import pytest_asyncio
from mf_agent_harness import AgentFactory, make_controller_settings

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("zeroconf", reason="needs the agent/controller extra")

from meshforge.agent.discovery import MdnsResolver, StaticResolver, resolver_for
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import DiscoverySettings
from meshforge.controller.discovery import ServiceAdvertiser
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]

PROBE_TYPE = "_mfprobe._tcp.local."


async def _mdns_works(timeout_s: float = 2.0) -> bool:
    """Register a throw-away record and see our own browse find it."""
    from zeroconf import ServiceStateChange, Zeroconf
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    probe_name = f"probe-{uuid.uuid4().hex[:8]}.{PROBE_TYPE}"
    found: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    # zeroconf calls handlers with *keyword* arguments; the names are the contract.
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
        PROBE_TYPE,
        probe_name,
        addresses=[socket.inet_aton("127.0.0.1")],
        port=1,
        server=f"{socket.gethostname()}.local.",
    )
    browser = AsyncServiceBrowser(browser_zc.zeroconf, PROBE_TYPE, handlers=[on_change])
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


@pytest_asyncio.fixture
async def mdns_available() -> None:
    if not await _mdns_works():
        pytest.skip("mDNS unavailable on this host (no multicast-capable interface)")


@pytest.mark.asyncio
async def test_agent_discovers_advertising_controller_on_loopback(
    mdns_available: None,
    pg_settings: DatabaseSettings,
    agents: AgentFactory,
) -> None:
    # A unique service type per test run so a developer's real controller on the
    # LAN can never satisfy this test by accident.
    service_type = f"_mf-{uuid.uuid4().hex[:8]}._tcp.local."
    settings = make_controller_settings(
        pg_settings, discovery={"advertise": True, "service_type": service_type}
    )
    async with ControllerRuntime.start(settings) as rt:
        assert rt._advertiser is not None and rt._advertiser.registered
        running = await agents(
            "",
            discovery={"enabled": True, "timeout_s": 4.0, "service_type": service_type},
        )
        await running.client.wait_for_state(AgentState.ONLINE, timeout_s=20.0)
        assert rt.manager is not None
        assert rt.manager.get(running.client.node_id) is not None
        # The record pointed at loopback and the agent dialled exactly that.
        assert running.client.stats.sessions_accepted == 1
    # Withdrawn on stop: a fresh browse finds nothing within a short timeout.
    resolver = MdnsResolver(
        DiscoverySettings(timeout_s=1.0, service_type=service_type), node_id="node_x"
    )
    assert await resolver.resolve() is None


@pytest.mark.asyncio
async def test_no_controller_found_yields_none_not_an_error(
    mdns_available: None,
) -> None:
    resolver = MdnsResolver(
        DiscoverySettings(
            timeout_s=0.5, service_type=f"_none-{uuid.uuid4().hex[:6]}._tcp.local."
        ),
        node_id="node_x",
    )
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await resolver.resolve() is None
    assert 0.4 <= loop.time() - t0 < 3.0  # bounded by timeout_s, never spins


@pytest.mark.asyncio
async def test_record_with_public_address_is_rejected(mdns_available: None) -> None:
    """A record pointing at a non-LAN address (spoofed or misconfigured) is ignored."""
    service_type = f"_mf-{uuid.uuid4().hex[:8]}._tcp.local."
    from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

    azc = AsyncZeroconf()
    info = AsyncServiceInfo(
        service_type,
        f"rogue.{service_type}",
        addresses=[socket.inet_aton("8.8.8.8")],
        port=47100,
        server=f"{socket.gethostname()}.local.",
    )
    await azc.async_register_service(info)
    try:
        resolver = MdnsResolver(
            DiscoverySettings(timeout_s=1.5, service_type=service_type),
            node_id="node_x",
        )
        assert await resolver.resolve() is None
    finally:
        await azc.async_unregister_all_services()
        await azc.async_close()


@pytest.mark.asyncio
async def test_advertiser_without_zeroconf_trouble_is_idempotent(
    mdns_available: None,
) -> None:
    adv = ServiceAdvertiser(
        service_type=f"_mf-{uuid.uuid4().hex[:8]}._tcp.local.", host="127.0.0.1", port=1
    )
    await adv.start()
    await adv.start()  # no double registration
    assert adv.registered
    await adv.stop()
    await adv.stop()
    assert not adv.registered


def test_resolver_selection() -> None:
    static = resolver_for("10.0.0.5:47100", DiscoverySettings(), node_id="n")
    assert isinstance(static, StaticResolver)
    v6 = resolver_for("[::1]:47100", DiscoverySettings(), node_id="n")
    assert isinstance(v6, StaticResolver)
    assert isinstance(resolver_for("", DiscoverySettings(), node_id="n"), MdnsResolver)


@pytest.mark.asyncio
async def test_static_resolver_formats_targets() -> None:
    assert await StaticResolver("10.0.0.5:47100").resolve() == "10.0.0.5:47100"
    v6 = resolver_for("[::1]:47100", DiscoverySettings(), node_id="n")
    assert await v6.resolve() == "[::1]:47100"
