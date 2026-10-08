"""The test suite's fabric CA (V0.7 G3, D139): one CA per test process, and
helpers that enrol test controllers and agents with it **without the join
dance** (tests about joining use the real ``fabric invite`` / ``join`` flow).

* :func:`fabric_ca` - the process-wide :class:`~meshforge.common.pki.CertificateAuthority`
  (created once, lazily; the session-scoped ``fabric_ca`` fixture returns it);
* :func:`seed_controller_fabric` - installs that CA into a controller's
  ``<data_dir>/controller/ca`` exactly as ``meshforge-controller fabric create``
  would (private folder, atomic install), unless a CA is already there.
  ``mf_controller_harness.make_settings`` and ``mf_jobs_harness.g2_settings``
  call it, so every component/cluster controller has a CA by default;
* :func:`enroll_agent` - gives an agent data directory a node certificate for
  its own identity key (``node.crt`` + ``ca.crt`` beside ``node.key``, stored by
  the agent's real :func:`~meshforge.agent.enrollment.store`).
  ``mf_agent_harness.make_agent_settings`` calls it;
* :func:`node_material` - a fresh key + certificate for a hand-driven agent
  with a given ``node_id`` (cached per ``node_id``);
* :func:`client_ssl_context` - a Python ``ssl`` client context that trusts only
  the fabric CA (for ``httpx`` / raw sockets), optionally presenting a node cert;
* :class:`NodeChannels` / :func:`session_channel` - gRPC channels to a running
  controller's session port, one per ``node_id`` (a channel carries exactly one
  client certificate, so one identity).

Nothing here is a mock: every certificate is a real X.509 certificate issued by
a real CA, verified by the real TLS stacks.
"""

from __future__ import annotations

import functools
import ssl
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import grpc

from meshforge.common import pki
from meshforge.common.ids import new_fabric_id
from meshforge.controller import fabric_ca as fabric_ca_mod

if TYPE_CHECKING:
    from meshforge.agent.identity import NodeIdentity
    from meshforge.agent.settings import AgentSettings
    from meshforge.controller.server import ControllerRuntime
    from meshforge.controller.settings import ControllerSettings

__all__ = [
    "NodeChannels",
    "ca_pem",
    "client_ssl_context",
    "enroll_agent",
    "enroll_identity",
    "fabric_ca",
    "grpc_channel",
    "identity_channel",
    "node_material",
    "seed_ca_dir",
    "seed_controller_fabric",
    "session_channel",
]

GRPC_TARGET_NAME = pki.SERVER_NAME
"""Hand-driven channels verify the controller as ``meshforge-controller`` (the
fixed SAN every server cert carries), like an agent that found it via mDNS."""


@functools.cache
def fabric_ca() -> pki.CertificateAuthority:
    """The one fabric CA of this test process."""
    return pki.create_ca(new_fabric_id())


def ca_pem() -> bytes:
    return pki.cert_to_pem(fabric_ca().cert)


def seed_controller_fabric(settings: ControllerSettings) -> None:
    """Install :func:`fabric_ca` as ``settings``' fabric CA (idempotent)."""
    seed_ca_dir(settings.ca_dir)


def seed_ca_dir(ca_dir: Path) -> None:
    """Install :func:`fabric_ca` at ``ca_dir`` (``<data>/controller/ca``),
    unless a CA is already there."""
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


def enroll_identity(identity: NodeIdentity) -> None:
    """Store a node certificate for ``identity`` from :func:`fabric_ca`
    (replacing whatever enrolment was there)."""
    from meshforge.agent import enrollment

    ca = fabric_ca()
    csr = pki.build_csr(
        identity.private_key, fabric_id=ca.fabric_id, node_id=identity.node_id
    )
    cert = pki.issue_node_cert(
        ca, csr, node_id=identity.node_id, fabric_id=ca.fabric_id
    )
    enrollment.store(identity, cert, ca.cert, force=True)


def enroll_agent(settings: AgentSettings) -> NodeIdentity:
    """Make ``settings``' agent enrolled in the test fabric (idempotent):
    creates the identity if needed, keeps a valid enrolment from this CA."""
    from meshforge.agent import enrollment
    from meshforge.agent.identity import load_or_create

    identity = load_or_create(settings.identity_dir)
    try:
        current = enrollment.load(identity)
    except enrollment.JoinError:
        current = None
    if current is None or current.ca_fingerprint != pki.cert_fingerprint(
        fabric_ca().cert
    ):
        enroll_identity(identity)
    return identity


@functools.cache
def node_material(node_id: str) -> tuple[bytes, bytes]:
    """``(key_pem, cert_pem)`` of a fresh node key certified for ``node_id``.

    A malformed ``node_id`` (a test of the controller's Hello validation)
    gets the certificate of a fresh, valid node instead: the CA cannot certify
    garbage, and the controller must refuse the Hello's claim either way.
    """
    from meshforge.common.errors import InvalidIdError
    from meshforge.common.ids import new_node_id, validate_node_id

    try:
        validate_node_id(node_id)
    except InvalidIdError:
        node_id = str(new_node_id())
    ca = fabric_ca()
    key = pki.generate_key()
    cert = pki.issue_node_cert(
        ca,
        pki.build_csr(key, fabric_id=ca.fabric_id, node_id=node_id),
        node_id=node_id,
        fabric_id=ca.fabric_id,
    )
    return pki.key_to_pem(key), pki.cert_to_pem(cert)


@functools.cache
def _material_files(node_id: str) -> tuple[Path, Path]:
    key_pem, cert_pem = node_material(node_id)
    folder = Path(tempfile.mkdtemp(prefix="mf-node-"))
    key, cert = folder / "node.key", folder / "node.crt"
    key.write_bytes(key_pem)
    key.chmod(0o600)
    cert.write_bytes(cert_pem)
    return key, cert


def client_ssl_context(*, node_id: str | None = None) -> ssl.SSLContext:
    """Trusts only the fabric CA; presents ``node_id``'s certificate if given.
    Hostname checks are on, against the address the test dials (the server
    cert names ``127.0.0.1`` and ``meshforge-controller``)."""
    context = ssl.create_default_context(cadata=ca_pem().decode("ascii"))
    if node_id is not None:
        key, cert = _material_files(node_id)
        context.load_cert_chain(cert, key)
    return context


def grpc_channel(target: str, node_id: str) -> grpc.aio.Channel:
    """A mutual-TLS channel to ``target`` for the agent ``node_id``."""
    key_pem, cert_pem = node_material(node_id)
    credentials = grpc.ssl_channel_credentials(
        root_certificates=ca_pem(), private_key=key_pem, certificate_chain=cert_pem
    )
    return grpc.aio.secure_channel(
        target,
        credentials,
        options=[("grpc.ssl_target_name_override", GRPC_TARGET_NAME)],
    )


def identity_channel(target: str, identity: NodeIdentity) -> grpc.aio.Channel:
    """A mutual-TLS channel with ``identity``'s stored enrolment, exactly as the
    real agent builds it (whatever CA enrolled it - e.g. a real ``join``)."""
    from meshforge.agent.session.tls import channel_options, load_session_tls

    tls = load_session_tls(identity)
    return grpc.aio.secure_channel(target, tls.credentials, options=channel_options())


class NodeChannels:
    """One mTLS channel per ``node_id`` to one controller target.

    :class:`~mf_session_client.FakeAgent` accepts this in place of a channel
    and asks :meth:`for_node` for its own when it opens its stream.
    """

    def __init__(self, target: str) -> None:
        self.target = target
        self._channels: dict[str, grpc.aio.Channel] = {}

    def for_node(self, node_id: str) -> grpc.aio.Channel:
        channel = self._channels.get(node_id)
        if channel is None:
            channel = grpc_channel(self.target, node_id)
            self._channels[node_id] = channel
        return channel

    async def close(self) -> None:
        channels, self._channels = list(self._channels.values()), {}
        for channel in channels:
            await channel.close()

    async def __aenter__(self) -> NodeChannels:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


@asynccontextmanager
async def session_channel(
    runtime: ControllerRuntime, node_id: str
) -> AsyncIterator[grpc.aio.Channel]:
    """A channel to ``runtime``'s session port for the agent ``node_id``."""
    async with grpc_channel(f"127.0.0.1:{runtime.grpc_port}", node_id) as channel:
        yield channel
