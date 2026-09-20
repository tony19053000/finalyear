"""Hello construction (roadmap 0.3.8) and the *fallback* hardware summary.

The Hello's real payload comes from hardware discovery
(:mod:`meshforge.agent.hardware`, roadmap 0.3.3): the agent runs one bounded pass at
start-up and passes the result to :class:`~meshforge.agent.session.client
.AgentSessionClient` as ``hardware=``.

:func:`basic_hardware` is what is sent when discovery fails outright (and what
tests constructing a client without ``hardware=`` get): what the standard library
(and psutil, if the agent extra is installed) can tell for free - CPU model string,
core/thread counts, total RAM - with no GPUs and ``capabilities == ["v0.3"]``. That
tag stays first in the discovered list too: it marks the *shape* of the Hello, not
the state of the discovery.

Strings are clipped to the controller's caps (``controller/session/limits.py``) so a
long ``platform.platform()`` can never turn into an ``INVALID_ARGUMENT`` on our own
Hello.
"""

from __future__ import annotations

import os
import platform
import socket
from typing import Any, Final

from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2

__all__ = [
    "CAPABILITIES_V03",
    "HOSTNAME_MAX_LENGTH",
    "OS_MAX_LENGTH",
    "PLATFORM_MAX_LENGTH",
    "PROTOCOL_VERSION",
    "basic_hardware",
    "build_hello",
    "host_facts",
]

PROTOCOL_VERSION: Final = common_pb2.PROTOCOL_VERSION_1
CAPABILITIES_V03: Final[tuple[str, ...]] = ("v0.3",)

# Same caps as the controller's limits module (its columns); duplicated on purpose:
# the agent never imports the controller.
HOSTNAME_MAX_LENGTH: Final = 253
OS_MAX_LENGTH: Final = 128
PLATFORM_MAX_LENGTH: Final = 64


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def host_facts() -> tuple[str, str, str]:
    """``(hostname, os, platform)`` strings for Hello, clipped to the caps."""
    hostname = socket.gethostname() or "unknown-host"
    os_text = f"{platform.system()} {platform.release()}".strip() or "unknown"
    plat = platform.machine() or "unknown"
    return (
        _clip(hostname, HOSTNAME_MAX_LENGTH),
        _clip(os_text, OS_MAX_LENGTH),
        _clip(plat, PLATFORM_MAX_LENGTH),
    )


def _psutil() -> Any | None:
    """The optional ``psutil`` module (agent extra), or None on a base install."""
    try:
        import psutil  # type: ignore[import-untyped]  # optional until G4
    except ImportError:
        return None
    return psutil


def _ram_bytes(psutil: Any | None) -> int:
    if psutil is None:
        return 0
    try:
        return int(psutil.virtual_memory().total)
    except (OSError, RuntimeError):
        return 0


def _cpu_cores(psutil: Any | None) -> tuple[int, int]:
    threads = os.cpu_count() or 0
    cores = threads
    if psutil is None:
        return cores, threads
    try:
        physical = psutil.cpu_count(logical=False)
    except (OSError, RuntimeError):
        physical = None
    return (int(physical) if physical else cores), threads


def basic_hardware() -> agent_pb2.HardwareSummary:
    """CPU/RAM basics from the standard library (+ psutil when present); no GPUs."""
    psutil = _psutil()
    cores, threads = _cpu_cores(psutil)
    return agent_pb2.HardwareSummary(
        cpu_model=_clip(platform.processor() or platform.machine() or "", 128),
        cpu_cores=cores,
        cpu_threads=threads,
        ram_bytes=_ram_bytes(psutil),
        capabilities=list(CAPABILITIES_V03),
    )


def build_hello(
    *,
    node_id: str,
    session_epoch: int,
    agent_version: str,
    hardware: agent_pb2.HardwareSummary | None = None,
    facts: tuple[str, str, str] | None = None,
) -> agent_pb2.Hello:
    """The Hello for one attempt. ``hardware``/``facts`` are injectable for tests."""
    hostname, os_text, plat = facts if facts is not None else host_facts()
    return agent_pb2.Hello(
        node_id=node_id,
        protocol_version=PROTOCOL_VERSION,
        agent_version=agent_version,
        hostname=hostname,
        session_epoch=session_epoch,
        os=os_text,
        platform=plat,
        hardware=hardware if hardware is not None else basic_hardware(),
    )
