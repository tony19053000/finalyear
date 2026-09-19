"""One ``serve`` process hosting all three transports on three consecutive ports.

Running raw TCP, gRPC and HTTP in one process is deliberate: every transport
then sees the same CPU, the same NIC and the same Python interpreter, and the
client can fetch server-side statistics for *any* transfer over one control
channel. Ports: ``--port`` (raw TCP + control), ``+1`` gRPC, ``+2`` HTTP.

Safety (docs/LAB.md §6): the bind address must be a private/loopback address
unless ``--allow-any``; every transport admits LAN peers only; the whole server
stops after ``idle_s`` without traffic or ``duration_s`` in total, and
:meth:`S3Server.stop` unblocks it from any thread (Ctrl-C, SIGTERM, tests).
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from spikes._harness.atomic import atomic_write_text
from spikes._s3.context import ServerContext, ServerLimits
from spikes._s3.grpc_server import GRPC_IMPORT_ERROR, GrpcHost, build_grpc_server
from spikes._s3.http_server import S3HttpServer
from spikes._s3.protocol import (
    GRPC_PORT_OFFSET,
    HTTP_PORT_OFFSET,
    MAX_CHUNK_BYTES,
    TransferError,
    grpc_limit,
)
from spikes._s3.tcp_server import TcpServer

_POLL_S: Final = 0.2

STOP_REQUESTED: Final = "stop requested"
STOP_IDLE: Final = "idle limit reached"
STOP_DURATION: Final = "duration limit reached"


class S3Server:
    """Raw TCP + gRPC + HTTP senders with one shared transfer log."""

    def __init__(
        self,
        bind: str,
        port: int,
        limits: ServerLimits | None = None,
        *,
        name: str | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.bind = bind
        self.requested_port = port
        self.ctx = ServerContext(
            limits or ServerLimits(), name=name or socket.gethostname(), log=log
        )
        self.tcp = TcpServer(bind, port, self.ctx)
        self._http: S3HttpServer | None = None
        self._grpc: GrpcHost | None = None
        self.ports: dict[str, int] = {}
        #: why gRPC is not served (``None`` when it is); reported in ``hello``
        self.grpc_error: str | None = GRPC_IMPORT_ERROR
        self.stop_reason: str | None = None
        self._opened = False

    # ------------------------------------------------------------------ setup
    def open(self) -> dict[str, int]:
        """Bind all three transports. Raises ``OSError``; nothing leaks on failure.

        With ``--port 0`` each transport gets an arbitrary free port (the +1/+2
        convention only applies to an explicit port); clients read the real
        ports from ``hello``. Without grpcio the gRPC port is ``0`` and
        :attr:`grpc_error` says why; raw TCP and HTTP are served regardless.
        """
        try:
            tcp_port = self.tcp.open()
            grpc_port = 0 if self.requested_port == 0 else tcp_port + GRPC_PORT_OFFSET
            http_port = 0 if self.requested_port == 0 else tcp_port + HTTP_PORT_OFFSET
            try:
                self._grpc = build_grpc_server(
                    self.bind,
                    grpc_port,
                    self.ctx,
                    max_message_bytes=grpc_limit(MAX_CHUNK_BYTES),
                )
                grpc_port = self._grpc.port
            except TransferError as exc:
                self.grpc_error = str(exc)
                self.ctx.note_error(str(exc))
                grpc_port = 0
            self._http = S3HttpServer(self.bind, http_port, self.ctx)
        except OSError:
            self._close_all()
            raise
        self.ports = {"tcp": tcp_port, "grpc": grpc_port, "http": self._http.port}
        self.tcp.ports = dict(self.ports)
        self.tcp.grpc_error = self.grpc_error
        self._opened = True
        return dict(self.ports)

    def start(self) -> dict[str, int]:
        """Open (if needed) and start serving in background threads."""
        if not self._opened:
            self.open()
        self.tcp.start()
        if self._http is not None:
            self._http.start()
        if self._grpc is not None:
            self._grpc.start()
        return dict(self.ports)

    def stop(self) -> None:
        """Thread-safe and idempotent; unblocks :meth:`serve`."""
        self.ctx.stopping.set()

    def close(self) -> None:
        """Close every socket and join the threads (idempotent)."""
        self._close_all()

    def serve(self) -> str:
        """Start everything and block until stopped or a limit is reached."""
        self.start()
        deadline = time.monotonic() + self.ctx.limits.duration_s
        try:
            while not self.ctx.stopping.is_set():
                if time.monotonic() >= deadline:
                    self.stop_reason = STOP_DURATION
                    break
                if self.ctx.idle_seconds >= self.ctx.limits.idle_s:
                    self.stop_reason = STOP_IDLE
                    break
                self.ctx.stopping.wait(timeout=_POLL_S)
            if self.stop_reason is None:
                self.stop_reason = STOP_REQUESTED
        finally:
            self.ctx.stopping.set()
            self._close_all()
        return self.stop_reason

    def _close_all(self) -> None:
        self.ctx.stopping.set()
        if self._grpc is not None:
            with contextlib.suppress(OSError, RuntimeError):
                self._grpc.stop()
            self._grpc = None
        if self._http is not None:
            self._http.close()
            self._http = None
        self.tcp.close()

    def start_thread(self) -> threading.Thread:
        """Run :meth:`serve` in a background thread (in-process use, tests)."""
        thread = threading.Thread(target=self.serve, name="s3-server", daemon=True)
        thread.start()
        return thread

    # ------------------------------------------------------------------ misc
    def write_ready_file(self, path: Path) -> None:
        """Publish the bound ports atomically (used by the loopback ceiling)."""
        atomic_write_text(path, json.dumps(self.ports, sort_keys=True))

    def summary(self) -> str:
        c = self.ctx.counters
        return (
            f"accepted {c.accepted}, refused non-LAN {c.refused_peer}, refused busy "
            f"{c.refused_busy}, header timeouts {c.header_timeouts}, bad requests "
            f"{c.bad_requests}, transfers {c.transfers} "
            f"({self.ctx.transfers.total_bytes} bytes), errors {len(c.errors)}"
        )
