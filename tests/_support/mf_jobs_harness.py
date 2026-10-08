"""Shared helpers for the V0.4 G2 component tests (blobs, jobs, token on the wire).

A real :class:`~meshforge.controller.server.ControllerRuntime` (PostgreSQL from
``pg_settings``, uvicorn on an ephemeral loopback port) with **default** liveness
timing - these tests are about the REST surface, not about leases - and a real
``httpx.AsyncClient`` over TCP.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx
from mf_controller_harness import controller_section
from mf_fabric import seed_controller_fabric

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings, LivenessSettings

if TYPE_CHECKING:
    from meshforge.controller.store.settings import DatabaseSettings

__all__ = ["api_client", "g2_settings", "running_controller", "token_of"]


def g2_settings(database: DatabaseSettings, **overrides: Any) -> ControllerSettings:
    overrides.setdefault("liveness", LivenessSettings())
    settings = ControllerSettings(
        controller=controller_section(**overrides), database=database
    )
    seed_controller_fabric(settings)  # V0.7 G3: mtls needs a fabric CA
    return settings


@asynccontextmanager
async def running_controller(
    settings: ControllerSettings,
) -> AsyncIterator[ControllerRuntime]:
    async with ControllerRuntime.start(settings) as runtime:
        yield runtime


def token_of(runtime: ControllerRuntime) -> str:
    return runtime.settings.token_path.read_text().strip()


@asynccontextmanager
async def api_client(
    runtime: ControllerRuntime, *, token: str | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers=headers,
        timeout=10,
    ) as client:
        yield client
