"""The S2 server: HTTP + gRPC on one machine, with a bounded lifetime.

:class:`S2Server` opens both listeners (so one temporary firewall rule for the
port range covers the whole spike), serves until Ctrl-C, ``idle_s`` without a
request, or ``duration_s`` in total, and always closes both listeners before
returning. It is usable in-process (the component tests and the loopback
self-check) and from the CLI.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from spikes._s2.grpcsrv import S2GrpcServer
from spikes._s2.httpsrv import S2HttpServer

STOP_REQUESTED: Final = "stop requested"
STOP_IDLE: Final = "idle limit reached"
STOP_DURATION: Final = "duration limit reached"
_POLL_S: Final = 0.2
_MAX_ERRORS: Final = 100


@dataclass(frozen=True, slots=True)
class ServerLimits:
    duration_s: float = 3600.0
    idle_s: float = 900.0
    #: per-connection socket timeout on the HTTP side
    io_timeout_s: float = 30.0
    max_workers: int = 8


@dataclass(slots=True)
class ServerState:
    errors: list[str] = field(default_factory=list)
    last_activity: float = field(default_factory=time.monotonic)


class S2Server:
    def __init__(
        self,
        bind: str,
        http_port: int,
        grpc_port: int,
        limits: ServerLimits | None = None,
        *,
        name: str,
        log: Callable[[str], None] = lambda _m: None,
    ) -> None:
        self.bind = bind
        self.name = name
        #: ports asked for; ``open()`` reports what was bound (0 = any free port)
        self.http_port_requested = http_port
        self.grpc_port_requested = grpc_port
        self.limits = limits or ServerLimits()
        self.log = log
        self.state = ServerState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.http: S2HttpServer | None = None
        self.grpc: S2GrpcServer | None = None

    # ------------------------------------------------------------ plumbing
    def touch(self) -> None:
        with self._lock:
            self.state.last_activity = time.monotonic()

    def note_error(self, text: str) -> None:
        with self._lock:
            if len(self.state.errors) < _MAX_ERRORS:
                self.state.errors.append(text)
        self.log(f"error: {text}")

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        """Thread-safe request to stop; :meth:`serve` returns shortly after."""
        self._stop.set()

    # ------------------------------------------------------------ lifecycle
    def open(self) -> tuple[int, int]:
        """Bind both listeners; returns the ports actually bound. Raises ``OSError``."""
        http = S2HttpServer(
            self.bind,
            self.http_port_requested,
            name=self.name,
            log=self.log,
            touch=self.touch,
            note_error=self.note_error,
            timeout_s=self.limits.io_timeout_s,
        )
        try:
            grpc_server = S2GrpcServer(
                self.bind,
                self.grpc_port_requested,
                name=self.name,
                log=self.log,
                touch=self.touch,
                note_error=self.note_error,
                max_workers=self.limits.max_workers,
            )
        except OSError:
            http.stop()
            raise
        self.http, self.grpc = http, grpc_server
        return http.port, grpc_server.port

    def serve(self) -> str:
        """Serve until stopped or a limit is reached; always closes both listeners."""
        http, grpc_server = self.http, self.grpc
        if http is None or grpc_server is None:
            raise RuntimeError("open() must be called before serve()")
        http.start()
        grpc_server.start()
        self.touch()
        started = time.monotonic()
        try:
            while not self._stop.is_set():
                reason = self._check_limits(started)
                if reason is not None:
                    return reason
                time.sleep(_POLL_S)
            return STOP_REQUESTED
        finally:
            self._shutdown()

    def _check_limits(self, started: float) -> str | None:
        now = time.monotonic()
        if now - started >= self.limits.duration_s:
            return STOP_DURATION
        with self._lock:
            idle = now - self.state.last_activity
        if idle >= self.limits.idle_s:
            return STOP_IDLE
        return None

    def _shutdown(self) -> None:
        self._stop.set()
        if self.grpc is not None:
            self.grpc.stop()
        if self.http is not None:
            self.http.stop()

    def counters(self) -> dict[str, Any]:
        return {
            "http": dict(self.http.counters) if self.http else {},
            "grpc": dict(self.grpc.counters) if self.grpc else {},
            "errors": list(self.state.errors),
        }

    def start_thread(self) -> threading.Thread:
        """Run :meth:`serve` on a background thread (in-process use)."""
        thread = threading.Thread(target=self.serve, name="s2-server", daemon=True)
        thread.start()
        return thread
