"""mDNS / DNS-SD advertisement of the controller's gRPC listener (roadmap 0.3.9).

The controller registers ``<instance>._meshforge-ctl._tcp.local.`` with the gRPC
port and a ``node=controller`` TXT record so agents with an empty
``controller_address`` can find it (:mod:`meshforge.agent.discovery`). Advertised
addresses: the configured bind IP, or - for a wildcard bind - every address of this
host that zeroconf can see. Loopback binds are advertised as loopback (tests).

Advertising is best effort: a host without a multicast-capable interface logs
``controller.discovery.unavailable`` and keeps serving; agents then need an
explicit ``controller_address``. Switched off with ``[controller.discovery]
advertise = false``.
"""

from __future__ import annotations

import ipaddress
import socket
from contextlib import suppress
from typing import Any, Final

from meshforge.common.aio import finished_within, run_to_completion
from meshforge.common.logging import get_logger

__all__ = ["ServiceAdvertiser"]

_CLOSE_TIMEOUT_S: Final = 5.0
"""Bound on one zeroconf close/withdraw (V1.0 G8 F6): logged, never waited
on forever by a controller shutdown."""

log = get_logger(__name__)


class ServiceAdvertiser:
    """Registers one DNS-SD record for the lifetime between :meth:`start` and
    :meth:`stop`; both are idempotent and never raise on mDNS trouble."""

    def __init__(
        self, *, service_type: str, host: str, port: int, instance: str | None = None
    ) -> None:
        self._service_type = service_type
        self._host = host
        self._port = port
        self._instance = instance or f"meshforge-controller-{socket.gethostname()}"
        self._azc: Any | None = None
        self._info: Any | None = None

    @property
    def registered(self) -> bool:
        return self._info is not None

    def addresses(self) -> list[bytes]:
        addr = ipaddress.ip_address(self._host)
        if not addr.is_unspecified:
            return [addr.packed]
        from zeroconf._utils.net import get_all_addresses  # re-exported untyped

        packed = [socket.inet_aton(a) for a in get_all_addresses()]
        return packed or [ipaddress.ip_address("127.0.0.1").packed]

    async def start(self) -> None:
        if self._info is not None:
            return
        try:
            from zeroconf import Error as ZeroconfError
            from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf
        except ImportError as exc:  # controller extra without zeroconf
            log.warning("controller.discovery.unavailable", error=str(exc))
            return
        name = f"{self._instance}-{self._port}.{self._service_type}"
        azc: Any | None = None
        try:
            azc = AsyncZeroconf()
            info = AsyncServiceInfo(
                self._service_type,
                name,
                addresses=self.addresses(),
                port=self._port,
                properties={"node": "controller"},
                server=f"{socket.gethostname()}.local.",
            )
            await azc.async_register_service(info)
        except BaseException as exc:
            # A failed or *cancelled* registration (shutdown during start)
            # still closes the instance - else its UDP sockets leak (F6).
            if azc is not None:
                with suppress(Exception):  # best effort
                    await self._bounded(azc.async_close())
            if not isinstance(exc, (OSError, ZeroconfError)):
                raise
            log.warning(
                "controller.discovery.unavailable",
                error=str(exc),
                cause=type(exc).__name__,
            )
            return
        self._azc = azc
        self._info = info
        log.info(
            "controller.discovery.advertised",
            service_type=self._service_type,
            port=self._port,
            name=name,
        )

    async def stop(self) -> None:
        azc, info = self._azc, self._info
        self._azc = self._info = None
        if azc is None:
            return

        async def close() -> None:
            try:
                if info is not None:
                    await azc.async_unregister_service(info)
            finally:
                await azc.async_close()

        # To the end even if the controller's shutdown is cancelled meanwhile:
        # an interrupted close leaks the UDP sockets on port 5353 (V1.0 G8 F6).
        await self._bounded(close())
        log.info("controller.discovery.withdrawn", port=self._port)

    async def _bounded(self, close: Any) -> None:
        """``close`` to the end even under a cancel (:func:`run_to_completion`,
        the cancel is re-raised after), at most :data:`_CLOSE_TIMEOUT_S`."""
        if not await run_to_completion(finished_within(close, _CLOSE_TIMEOUT_S)):
            log.warning(
                "controller.discovery.close_timeout",
                port=self._port,
                timeout_s=_CLOSE_TIMEOUT_S,
            )
