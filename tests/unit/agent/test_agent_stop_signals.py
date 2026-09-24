"""G6 fix round: the agent's graceful-stop signals include SIGBREAK where it exists.

On Windows a graceful stop of a console process is CTRL_BREAK_EVENT, delivered as
``SIGBREAK``; unhandled it kills the agent without the stream half-close (so the
controller would record a transport loss, not ``agent_closed``). CTRL_BREAK cannot
be sent on Linux, so on a platform without ``SIGBREAK`` the test stands a real,
sendable signal in for it and proves the whole path - registration, delivery,
``on_signal`` on the loop. On Windows it checks the real ``SIGBREAK`` is listed.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

import pytest

from meshforge.agent import runtime

pytestmark = pytest.mark.unit


def test_sigint_and_sigterm_always_stop_the_agent() -> None:
    assert runtime.stop_signals()[:2] == (signal.SIGINT, signal.SIGTERM)


def test_sigbreak_is_a_stop_signal_exactly_where_the_platform_has_it() -> None:
    has_break = hasattr(signal, "SIGBREAK")
    assert (getattr(signal, "SIGBREAK", None) in runtime.stop_signals()) is has_break
    if sys.platform == "win32":
        assert has_break


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX stand-in signal")
@pytest.mark.asyncio
async def test_a_sigbreak_handler_is_installed_and_requests_the_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SIGUSR2 plays SIGBREAK: same code path (hasattr -> registered -> delivered).
    monkeypatch.setattr(signal, "SIGBREAK", signal.SIGUSR2, raising=False)
    assert signal.SIGUSR2 in runtime.stop_signals()
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    try:
        runtime.install_signal_handlers(stopped.set)
        os.kill(os.getpid(), signal.SIGUSR2)
        await asyncio.wait_for(stopped.wait(), 1.0)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGUSR2):
            loop.remove_signal_handler(sig)
