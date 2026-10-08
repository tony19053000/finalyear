"""The test suite's fabric CA (V0.7 G3, D139): one CA per test process, and
helpers that enrol test controllers and agents with it **without the join
dance** (tests about joining use the real ``fabric invite`` / ``join`` flow).

* :func:`fabric_ca` - the process-wide :class:`~meshforge.common.pki.CertificateAuthority`
  (created once, lazily; the ``fabric_ca`` fixture in the tier conftests returns it);
* :func:`seed_controller_fabric` - installs that CA into a controller's
  ``<data_dir>/controller/ca`` exactly as ``meshforge-controller fabric create``
  would (private folder, atomic install), unless a CA is already there;
* :func:`session_channel` - a gRPC channel to a running controller's session
  port for a hand-driven agent with ``node_id``.

Nothing here is a mock: every certificate is a real X.509 certificate issued by
a real CA, verified by the real TLS stacks.
"""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import grpc

from meshforge.common import pki
from meshforge.common.ids import new_fabric_id
from meshforge.controller import fabric_ca as fabric_ca_mod

if TYPE_CHECKING:
    from meshforge.controller.server import ControllerRuntime
    from meshforge.controller.settings import ControllerSettings

__all__ = ["fabric_ca", "seed_controller_fabric", "session_channel"]


@functools.cache
def fabric_ca() -> pki.CertificateAuthority:
    """The one fabric CA of this test process."""
    return pki.create_ca(new_fabric_id())


def seed_controller_fabric(settings: ControllerSettings) -> None:
    """Install :func:`fabric_ca` as ``settings``' fabric CA (idempotent)."""
    ca_dir = settings.ca_dir
    if fabric_ca_mod.fabric_present(ca_dir):
        return
    ca = fabric_ca()
    with fabric_ca_mod._lock(ca_dir):
        fabric_ca_mod._install_dir(
            ca_dir,
            {
                fabric_ca_mod.CA_KEY_NAME: pki.key_to_pem(ca.key),
                fabric_ca_mod.CA_CERT_NAME: pki.cert_to_pem(ca.cert),
            },
        )


@asynccontextmanager
async def session_channel(
    runtime: ControllerRuntime, node_id: str
) -> AsyncIterator[grpc.aio.Channel]:
    """A channel to ``runtime``'s session port for the agent ``node_id``."""
    del node_id  # the plaintext session port does not identify the caller yet
    async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as ch:
        yield ch
