"""Finding the controller (roadmap 0.3.9): configured address, else mDNS.

The session client calls :meth:`ControllerResolver.resolve` at the start of every
cycle (state ``DISCOVERING``). Two implementations:

* :class:`StaticResolver` - ``agent.controller_address`` is set; returns it at once.
* :class:`MdnsResolver` - browses DNS-SD for ``agent.discovery.service_type``
  (default ``_meshforge-ctl._tcp.local.``) for at most ``timeout_s``, resolves the
  first record whose address passes :func:`meshforge.common.netaddr.is_lan_address`
  (private / loopback / link-local only - a record pointing at a public address is
  ignored and logged, CLAUDE.md §9), and returns ``"ip:port"``. **Trust point:**
  mDNS records are unauthenticated until pairing/mTLS exist (V0.7, ADR-0008); the
  LAN filter *limits* the target, it does not authenticate it. Nothing found ->
  ``None`` -> the client goes to BACKOFF and browses again next cycle; it never
  spins.

``zeroconf`` (agent extra) is imported lazily so the module - and the whole agent
with a configured address - works on a base install; a missing package is a typed
:class:`~meshforge.common.errors.ConfigError` naming the extra.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from typing import TYPE_CHECKING, Any, Protocol

from meshforge.common.errors import ConfigError
from meshforge.common.logging import get_logger
from meshforge.common.netaddr import is_lan_address

if TYPE_CHECKING:
    from meshforge.agent.settings import DiscoverySettings

__all__ = [
    "ControllerResolver",
    "MdnsResolver",
    "StaticResolver",
    "format_target",
    "resolver_for",
]

log = get_logger(__name__)

_RESOLVE_TIMEOUT_MS = 2000


def format_target(host: str, port: int) -> str:
    """``host:port`` as a gRPC target; IPv6 literals are bracketed."""
    with contextlib.suppress(ValueError):
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            return f"[{host}]:{port}"
    return f"{host}:{port}"


class ControllerResolver(Protocol):
    """Where is the controller? ``None`` = not found this cycle."""

    async def resolve(self) -> str | None: ...


class StaticResolver:
    """A configured ``controller_address``."""

    def __init__(self, target: str) -> None:
        self._target = target

    async def resolve(self) -> str | None:
        return self._target


class MdnsResolver:
    """DNS-SD browse for the controller's service type (see module docstring)."""

    def __init__(
        self,
        settings: DiscoverySettings,
        *,
        node_id: str,
        resolve_timeout_ms: int = _RESOLVE_TIMEOUT_MS,
    ) -> None:
        self._settings = settings
        self._node_id = node_id
        self._resolve_timeout_ms = resolve_timeout_ms

    async def resolve(self) -> str | None:
        try:
            from zeroconf import ServiceStateChange, Zeroconf
            from zeroconf.asyncio import (
                AsyncServiceBrowser,
                AsyncServiceInfo,
                AsyncZeroconf,
            )
        except ImportError as exc:
            raise ConfigError(
                "controller_address is empty and mDNS discovery needs the agent "
                "extra (uv sync --extra agent), or set agent.controller_address",
                details={"missing": "zeroconf"},
            ) from exc

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.timeout_s
        names: asyncio.Queue[str] = asyncio.Queue()

        def on_change(
            zeroconf: Zeroconf,
            service_type: str,
            name: str,
            state_change: ServiceStateChange,
        ) -> None:
            if state_change is ServiceStateChange.Added:
                names.put_nowait(name)

        try:
            azc = AsyncZeroconf()
        except OSError as exc:
            # No multicast-capable interface: not fatal, retry next cycle.
            log.warning(
                "agent.discovery.unavailable",
                node_id=self._node_id,
                error=str(exc),
            )
            return None
        browser: Any | None = None
        try:
            browser = AsyncServiceBrowser(
                azc.zeroconf, self._settings.service_type, handlers=[on_change]
            )
            rejected = 0
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    name = await asyncio.wait_for(names.get(), remaining)
                except TimeoutError:
                    break
                info = AsyncServiceInfo(self._settings.service_type, name)
                if not await info.async_request(azc.zeroconf, self._resolve_timeout_ms):
                    log.debug(
                        "agent.discovery.unresolved", node_id=self._node_id, name=name
                    )
                    continue
                target = self._pick(info.parsed_addresses(), int(info.port or 0))
                if target is None:
                    rejected += 1
                    log.warning(
                        "agent.discovery.rejected",
                        node_id=self._node_id,
                        name=name,
                        reason="no LAN address or no port in the record",
                    )
                    continue
                log.info(
                    "agent.discovery.found",
                    node_id=self._node_id,
                    address=target,
                    name=name,
                )
                return target
            log.info(
                "agent.discovery.none",
                node_id=self._node_id,
                service_type=self._settings.service_type,
                timeout_s=self._settings.timeout_s,
                rejected=rejected,
            )
            return None
        finally:
            if browser is not None:
                await browser.async_cancel()
            await azc.async_close()

    @staticmethod
    def _pick(addresses: list[str], port: int) -> str | None:
        if port <= 0:
            return None
        for address in addresses:
            if is_lan_address(address):
                return format_target(address, port)
        return None


def resolver_for(
    controller_address: str, discovery: DiscoverySettings, *, node_id: str
) -> ControllerResolver:
    """The resolver the settings ask for."""
    if controller_address:
        from meshforge.agent.settings import parse_controller_address

        host, port = parse_controller_address(controller_address)
        return StaticResolver(format_target(host, port))
    return MdnsResolver(discovery, node_id=node_id)
