"""V0.7 G6 exit list: the agent's HTTPS transports open their connections
through :class:`CancelSafeBackend` (the leak itself: component
``test_v07_g6_exitlist_http_cancel.py``).

Pins the private httpx/httpcore attribute :func:`cancel_safe_transport`
replaces: an upgrade that renames it fails here, not silently in production.
"""

from __future__ import annotations

import ssl

import httpcore

from meshforge.agent.http_connect import CancelSafeBackend, cancel_safe_transport
from meshforge.agent.http_tls import AgentHttpTls


def _backend(transport: object) -> object:
    pool = transport._pool  # type: ignore[attr-defined]
    assert isinstance(pool, httpcore.AsyncConnectionPool)
    return pool._network_backend


def test_the_transport_opens_connections_through_the_cancel_safe_backend() -> None:
    context = ssl.create_default_context()
    transport = cancel_safe_transport(context)
    assert isinstance(_backend(transport), CancelSafeBackend)
    assert transport._pool._ssl_context is context


def test_every_agent_https_client_gets_it() -> None:
    tls = AgentHttpTls(context=ssl.create_default_context())
    first, second = tls.transport(), tls.transport()
    assert isinstance(_backend(first), CancelSafeBackend)
    assert first is not second  # still a fresh transport per client
