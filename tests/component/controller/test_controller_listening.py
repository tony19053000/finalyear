"""V0.3 G6 (D24): ``controller.listening`` carries the *actually bound* addresses.

Both listeners bind ``127.0.0.1:0``; the one event must name the resolved ports,
agree with the runtime, and both addresses must accept a connection the moment
the event is logged (a harness reads the line and connects - no port guessing).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import structlog
from mf_controller_harness import make_settings

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.component


@pytest.mark.asyncio
async def test_listening_event_names_the_resolved_ports_and_both_accept(
    pg_settings: DatabaseSettings,
) -> None:
    settings = make_settings(pg_settings)  # grpc_bind/http_bind = 127.0.0.1:0
    assert settings.controller.grpc_bind.endswith(":0")
    assert settings.controller.http_bind.endswith(":0")
    with structlog.testing.capture_logs() as logs:
        async with ControllerRuntime.start(settings) as runtime:
            events = [e for e in logs if e["event"] == "controller.listening"]
            assert len(events) == 1, events
            (event,) = events
            assert event["log_level"] == "info"
            grpc_host, grpc_port = event["grpc"].rsplit(":", 1)
            http_host, http_port = event["http"].rsplit(":", 1)
            assert (grpc_host, http_host) == ("127.0.0.1", "127.0.0.1")
            assert int(grpc_port) == runtime.grpc_port != 0
            assert int(http_port) == runtime.http_port != 0

            # Connect using only what the event said.
            _reader, writer = await asyncio.open_connection(grpc_host, int(grpc_port))
            writer.close()
            await writer.wait_closed()
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"http://{event['http']}/healthz")
            assert response.status_code == 200
    started = [e["event"] for e in logs].index("controller.started")
    listening = [e["event"] for e in logs].index("controller.listening")
    assert listening < started
