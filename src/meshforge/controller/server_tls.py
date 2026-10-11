"""The controller's server certificate *in use*, renewed while it runs (V0.7 G3,
D149 carry-over of D134).

:class:`ServerTls` holds the current :class:`~meshforge.controller.fabric_ca.ServerCert`
and hands it to every TLS listener in a form that follows renewals **without a
restart**:

* gRPC - :meth:`ServerTls.grpc_credentials` builds
  ``grpc.dynamic_ssl_server_credentials``: gRPC asks a fetcher at the start of
  every new handshake (on a core thread), which returns the new configuration
  once per credential after a renewal and ``None`` (keep) otherwise;
* Python ``ssl`` (uvicorn REST) - :meth:`ServerTls.ssl_context` returns a
  context it remembers; after a renewal :func:`ssl.SSLContext.load_cert_chain`
  is called on it again (in the event loop, where every asyncio handshake runs).

Either way **established connections keep the certificate they were set up
with** (a live agent session is never cut by a renewal); new handshakes see the
new serial.

:meth:`ServerTls.run` re-runs :func:`~meshforge.controller.fabric_ca.ensure_server_cert`
every ``[controller.tls] check_interval_s`` (in a thread: it may sign). It renews
at 2/3 of the certificate's life and re-issues one that became unusable (files
changed under it, SANs changed). A failed check is logged and retried at the
next interval with the current certificate still served - never a crash of the
controller, never a listener without a certificate.
"""

from __future__ import annotations

import asyncio
import ssl
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

import grpc

from meshforge.common import pki
from meshforge.common.errors import MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.controller.fabric_ca import (
    SERVER_CERT_NAME,
    SERVER_KEY_NAME,
    FabricCa,
    ServerCert,
    ensure_server_cert,
)

__all__ = ["ClientCerts", "ServerTls"]

log = get_logger(__name__)

ClientCerts = Literal["none", "optional", "required"]
_VERIFY: Final[dict[str, ssl.VerifyMode]] = {
    "none": ssl.CERT_NONE,
    "optional": ssl.CERT_OPTIONAL,
    "required": ssl.CERT_REQUIRED,
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ServerTls:
    """See module docstring. One per controller process."""

    def __init__(
        self,
        fabric: FabricCa,
        initial: ServerCert,
        *,
        hosts: list[str],
        lifetime: timedelta,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._fabric = fabric
        self._hosts = list(hosts)
        self._lifetime = lifetime
        self._now = now
        self._lock = threading.Lock()
        self._current = initial
        self._generation = 0
        self._configs: grpc.ServerCertificateConfiguration = self._grpc_config()
        self._contexts: list[ssl.SSLContext] = []
        self.renewals = 0
        self.check_failures = 0

    # ------------------------------------------------------------------ state

    @property
    def current(self) -> ServerCert:
        with self._lock:
            return self._current

    @property
    def generation(self) -> int:
        """Bumped by every swap to a new certificate."""
        with self._lock:
            return self._generation

    @property
    def ca_pem(self) -> bytes:
        return pki.cert_to_pem(self._fabric.cert)

    def chain_pem(self) -> bytes:
        """``server.crt`` + ``ca.crt``: a joining agent reads the CA from the
        handshake and checks it against its code's pin (D136)."""
        return pki.cert_to_pem(self.current.cert) + self.ca_pem

    def _grpc_config(self) -> grpc.ServerCertificateConfiguration:
        cert = self._current
        return grpc.ssl_server_certificate_configuration(
            [
                (
                    pki.key_to_pem(cert.key),
                    pki.cert_to_pem(cert.cert) + self.ca_pem,
                )
            ],
            root_certificates=self.ca_pem,
        )

    # ------------------------------------------------------------ listeners

    def grpc_credentials(self, *, require_client_auth: bool) -> grpc.ServerCredentials:
        """Credentials whose certificate follows renewals (module docstring).

        ``require_client_auth``: the client must present a certificate that
        chains to the fabric CA (BoringSSL checks chain and validity).
        """
        with self._lock:
            initial, seen = self._configs, self._generation
        served = [seen]

        def fetch() -> grpc.ServerCertificateConfiguration | None:
            # Called by gRPC core on its own threads, once per new handshake.
            with self._lock:
                if self._generation == served[0]:
                    return None
                served[0] = self._generation
                return self._configs

        # The stubs say the fetcher returns a configuration; gRPC documents
        # None as "keep the current one", which is what we rely on.
        return grpc.dynamic_ssl_server_credentials(
            initial,
            fetch,  # type: ignore[arg-type]
            require_client_authentication=require_client_auth,
        )

    def ssl_context(self, *, client_certs: ClientCerts) -> ssl.SSLContext:
        """A TLS 1.3-only server context (D139) that follows renewals.

        ``client_certs`` ``optional``/``required``: a presented certificate must
        chain to the fabric CA (else the handshake fails).
        """
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        # No TLS 1.3 session tickets (D156 e): nothing here resumes sessions,
        # and a post-handshake ticket is what a client that reads and writes
        # one TLS connection from two threads (websockets.sync) trips over.
        context.num_tickets = 0
        if client_certs != "none":
            context.load_verify_locations(cadata=self.ca_pem.decode("ascii"))
        context.verify_mode = _VERIFY[client_certs]
        self._load(context, self.current)
        self._contexts.append(context)
        return context

    def _load(self, context: ssl.SSLContext, cert: ServerCert) -> None:
        """``server.crt`` + ``ca.crt`` as the presented chain (V0.7 G4): like
        the gRPC listeners, so ``compute login --fingerprint`` can read the CA
        from the handshake and pin it (D141). ``ssl`` loads a chain only from
        a file, so the public chain goes through a short-lived temporary file
        (no secret in it; the key is read from ``server.key`` as before)."""
        chain = pki.cert_to_pem(cert.cert) + self.ca_pem
        with tempfile.TemporaryDirectory(prefix="meshforge-chain-") as tmp:
            chain_file = Path(tmp) / SERVER_CERT_NAME
            chain_file.write_bytes(chain)
            context.load_cert_chain(chain_file, cert.path / SERVER_KEY_NAME)

    # ---------------------------------------------------------------- renew

    async def check(self) -> bool:
        """Renew / re-issue when due; ``True`` when a new certificate is now
        served. Raises nothing but ``CancelledError`` (failures are logged)."""
        try:
            result = await asyncio.to_thread(
                ensure_server_cert,
                self.current.path,
                self._fabric,
                hosts=self._hosts,
                now=self._now(),
                lifetime=self._lifetime,
            )
        except MeshForgeError as exc:
            self.check_failures += 1
            log.warning(
                "controller.tls.renew_failed",
                error=exc.message,
                failure_type=exc.failure_type.value,
                serving_serial=format(self.current.cert.serial_number, "x"),
            )
            return False
        if result.cert.serial_number == self.current.cert.serial_number:
            return False
        self._swap(result)
        return True

    def _swap(self, new: ServerCert) -> None:
        for context in self._contexts:
            self._load(context, new)
        with self._lock:
            old = self._current
            self._current = new
            self._generation += 1
            self._configs = self._grpc_config()
        self.renewals += 1
        log.info(
            "controller.tls.server_cert_reloaded",
            action=new.action.value,
            reason=new.reason,
            old_serial=format(old.cert.serial_number, "x"),
            serial=format(new.cert.serial_number, "x"),
            not_after=new.cert.not_valid_after_utc.isoformat(),
        )

    async def run(self, interval_s: float) -> None:
        """Forever: wait ``interval_s``, then :meth:`check`."""
        while True:
            await asyncio.sleep(interval_s)
            await self.check()
