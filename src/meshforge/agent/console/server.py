"""Run the console inside the agent process: uvicorn in the agent's event loop.

:func:`run_with_console` is what ``meshforge-agent run`` calls when the console
is on (``[agent.console] enabled``, default on for the system service):

1. bind ``127.0.0.1:<port>`` **ourselves** and check the bound address is
   loopback (fail closed: the console is never served to the LAN, whatever the
   settings say - they already refuse any other ``bind``);
2. new token, written to the token file (:mod:`.auth`), the log tail handler;
3. uvicorn (plain, h11, no signal handlers: the supervisor owns signals) on
   that socket, next to the :class:`~.supervisor.Supervisor`;
4. on exit: uvicorn stopped, token file removed.

A console that cannot start (port taken by another agent, token folder
unwritable) is logged ``agent.console.unavailable`` and the agent carries on
without it - the fabric part still runs, and ``meshforge-agent join`` still
works from a terminal; refusing to start would take the laptop out of the
fabric over a UI problem.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

import uvicorn

from meshforge.agent.console.app import DEFAULT_DIST, ConsoleContext, create_app
from meshforge.agent.console.auth import ConsoleAuth
from meshforge.agent.console.logbuffer import LogBuffer, install_log_buffer
from meshforge.agent.console.supervisor import Supervisor
from meshforge.agent.settings import CONSOLE_HOST, AgentSettings
from meshforge.common.errors import ConfigError, ExitCode
from meshforge.common.logging import get_logger

__all__ = ["ConsoleServer", "bind_console_socket", "run_with_console"]

log = get_logger(__name__)

_BACKLOG: Final = 64


class _NoSignalUvicorn(uvicorn.Server):
    """The supervisor owns SIGINT/SIGTERM (like the controller's server)."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def bind_console_socket(port: int) -> socket.socket:
    """A listening TCP socket on ``127.0.0.1:port``; :class:`ConfigError` when
    the port is taken or the result is not loopback."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((CONSOLE_HOST, port))
        bound = str(sock.getsockname()[0])
        if not ipaddress.ip_address(bound).is_loopback:  # pragma: no cover - paranoia
            raise ConfigError(f"the console bound {bound}, not a loopback address")  # noqa: TRY301
        sock.listen(_BACKLOG)
        sock.setblocking(False)
    except OSError as exc:
        sock.close()
        raise ConfigError(
            f"cannot listen on {CONSOLE_HOST}:{port} for the local console "
            f"({exc.strerror or exc}); is another MeshForge agent running here?",
            details={"port": port},
        ) from None
    except ConfigError:
        sock.close()
        raise
    return sock


class ConsoleServer:
    """uvicorn serving the console app on an already bound socket."""

    def __init__(self, ctx: ConsoleContext, sock: socket.socket) -> None:
        self.ctx = ctx
        self.sock = sock
        self.port = int(sock.getsockname()[1])
        config = uvicorn.Config(
            create_app(ctx),
            host=CONSOLE_HOST,
            port=self.port,
            log_config=None,
            access_log=False,
            lifespan="off",
            http="h11",
            ws="none",
            proxy_headers=False,
            server_header=False,
            timeout_graceful_shutdown=2,
        )
        self._server = _NoSignalUvicorn(config)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(
            self._server.serve(sockets=[self.sock]), name="agent-console-http"
        )

    async def close(self) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        with contextlib.suppress(OSError):
            self.sock.close()


async def run_with_console(
    settings: AgentSettings,
    *,
    reload_settings: Callable[[], AgentSettings] | None = None,
    dist: Path = DEFAULT_DIST,
    supervisor: Supervisor | None = None,
    logs: LogBuffer | None = None,
) -> ExitCode:
    """The agent process with its console (module docstring)."""
    sup = supervisor or Supervisor(settings, reload_settings=reload_settings)
    cfg = settings.agent.console
    buffer = logs if logs is not None else install_log_buffer(cfg.log_lines)
    server: ConsoleServer | None = None
    auth: ConsoleAuth | None = None
    try:
        sock = bind_console_socket(cfg.port)
        auth = ConsoleAuth(
            port=int(sock.getsockname()[1]),
            session_ttl_s=cfg.session_ttl_s,
            launch_code_ttl_s=cfg.launch_code_ttl_s,
        )
        try:
            token_path = await asyncio.to_thread(
                auth.publish, settings.console_token_dir
            )
        except OSError as exc:
            sock.close()
            raise ConfigError(
                f"cannot write the console token in {settings.console_token_dir}: "
                f"{exc.strerror or exc}"
            ) from None
        ctx = ConsoleContext(supervisor=sup, auth=auth, logs=buffer, dist=dist)
        server = ConsoleServer(ctx, sock)
        server.start()
        log.info(
            "agent.console.listening",
            url=f"http://{CONSOLE_HOST}:{server.port}/",
            access_file=str(token_path),
        )
    except ConfigError as exc:
        log.error(  # noqa: TRY400 - explained, the agent carries on
            "agent.console.unavailable",
            error=exc.message,
            hint="the agent runs without its local console; join with "
            "`sudo meshforge-agent join <code>`",
        )
    try:
        return await sup.run()
    finally:
        if server is not None:
            await server.close()
        if auth is not None:
            await asyncio.to_thread(auth.unpublish)
