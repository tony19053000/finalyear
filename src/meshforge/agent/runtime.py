"""The agent process (roadmap 0.3.8 E): identity -> session client -> signals -> exit code.

:func:`run_agent` is the blocking entry used by ``meshforge-agent run``::

    settings -> identity.load_or_create (thread) -> AgentSessionClient.run()
             -> SIGINT/SIGTERM -> client.request_stop() -> exit 0

Signals are Windows-safe: ``loop.add_signal_handler`` where it exists, else
``signal.signal`` + ``call_soon_threadsafe`` (ADR-0009: the agent runs natively on
Windows). On stop the agent half-closes its stream and exits 0; the controller
records ``agent_closed`` from the stream end - no farewell message is needed.

Exit codes: ``OK`` after a requested stop; the error's own code for a typed failure
(:class:`~meshforge.common.errors.IdentityError` / ``ConfigError`` ->
``CONFIG_ERROR``; :class:`~meshforge.agent.session.errors.SupersededError` ->
``CONFLICT``); ``FAILURE`` for anything else, which is logged with its traceback.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable

from meshforge.agent.identity import NodeIdentity, load_or_create
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.settings import AgentSettings
from meshforge.common.errors import ExitCode, MeshForgeError
from meshforge.common.logging import get_logger

__all__ = ["install_signal_handlers", "run_agent", "serve"]

log = get_logger(__name__)


def install_signal_handlers(on_signal: Callable[[], None]) -> None:
    """SIGINT/SIGTERM -> ``on_signal()`` on the running loop; works on Windows too."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal)
        except (NotImplementedError, RuntimeError):  # Windows / non-main thread
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(on_signal))


async def serve(
    settings: AgentSettings, *, identity: NodeIdentity | None = None
) -> AgentSessionClient:
    """Run the agent until a signal stops it; returns the finished client.

    Raises the typed error that stopped it, if any (see :func:`run_agent`).
    """
    if identity is None:
        identity = await asyncio.to_thread(
            load_or_create,
            settings.identity_dir,
            lock_timeout_s=settings.agent.identity_lock_timeout_s,
        )
    client = AgentSessionClient(settings, identity)
    install_signal_handlers(client.request_stop)
    log.info(
        "agent.started",
        node_id=client.node_id,
        agent_version=settings.agent.agent_version,
        controller_address=settings.agent.controller_address or None,
        discovery=settings.agent.uses_discovery,
    )
    await client.run()
    return client


def run_agent(settings: AgentSettings) -> int:
    """Blocking entry used by ``meshforge-agent run``; returns the exit code."""
    try:
        asyncio.run(serve(settings))
    except MeshForgeError as exc:
        log.exception(
            "agent.exited_with_error",
            error=str(exc),
            failure_type=exc.failure_type.value,
            exit_code=int(exc.exit_code),
            details=exc.details,
        )
        return int(exc.exit_code)
    except Exception as exc:  # last line of defence: log with traceback, exit FAILURE
        log.exception("agent.crashed", cause=type(exc).__name__)
        return int(ExitCode.FAILURE)
    log.info("agent.exited", exit_code=int(ExitCode.OK))
    return int(ExitCode.OK)
