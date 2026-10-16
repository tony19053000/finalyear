"""Tester r1 (D156): independent checks of the follow-open timeout contract
with real sockets: TLS-stalled open, post-open abrupt close stays a
network_error, exit code 6, no secret in any message, job-keeps-running hint."""

from __future__ import annotations

import asyncio
import io
import socket
import ssl
import threading
import time
from collections.abc import Iterator

import pytest
import typer

import meshforge.cli  # noqa: F401  (import app first: circular import otherwise)
from meshforge.cli import app as _app  # noqa: F401
from meshforge.cli import job_commands
from meshforge.cli.client import ControllerError
from meshforge.cli.exits import CliExit
from meshforge.cli.follow import follow_logs
from meshforge.common.errors import FailureType

pytestmark = [pytest.mark.component, pytest.mark.timeout(60)]

SECRET = "Bearer s3cr3t-TOKEN-abc123"


@pytest.fixture
def stalled_tcp() -> Iterator[int]:
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.1)
    held: list[socket.socket] = []
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                held.append(listener.accept()[0])
            except TimeoutError:
                continue
            except OSError:
                return

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        t.join(5)
        listener.close()
        for c in held:
            c.close()


@pytest.fixture
def ws_server_that_drops() -> Iterator[int]:
    """Real websocket server: completes the upgrade, then closes the TCP
    connection with no close frame and no end frame."""
    from websockets.asyncio.server import serve

    ready = threading.Event()
    box: dict[str, object] = {}

    async def handler(ws: object) -> None:
        transport = ws.transport  # type: ignore[attr-defined]
        transport.abort()

    async def main() -> None:
        async with serve(handler, "127.0.0.1", 0) as server:
            box["port"] = server.sockets[0].getsockname()[1]
            box["loop"] = asyncio.get_running_loop()
            box["stop"] = asyncio.Event()
            ready.set()
            await box["stop"].wait()  # type: ignore[attr-defined]

    t = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    t.start()
    assert ready.wait(10)
    try:
        yield int(box["port"])  # type: ignore[call-overload]
    finally:
        box["loop"].call_soon_threadsafe(box["stop"].set)  # type: ignore[attr-defined]
        t.join(5)


def test_tls_stalled_open_is_typed_timeout_without_secret(stalled_tcp: int) -> None:
    ctx = ssl.create_default_context()
    started = time.monotonic()
    with pytest.raises(ControllerError) as caught:
        follow_logs(
            f"wss://127.0.0.1:{stalled_tcp}/v1/jobs/job_x/logs/ws",
            auth_header=SECRET,
            outputs={"stdout": io.BytesIO()},
            open_timeout_s=0.6,
            ssl_context=ctx,
        )
    assert caught.value.failure_type is FailureType.TIMEOUT
    assert time.monotonic() - started < 10
    assert "s3cr3t" not in caught.value.message
    assert "Bearer" not in caught.value.message


def test_abrupt_close_after_open_stays_network_error(ws_server_that_drops: int) -> None:
    with pytest.raises(ControllerError) as caught:
        follow_logs(
            f"ws://127.0.0.1:{ws_server_that_drops}/x",
            auth_header=SECRET,
            outputs={"stdout": io.BytesIO()},
            open_timeout_s=5,
        )
    assert caught.value.failure_type is FailureType.NETWORK_ERROR
    assert "did not open" not in caught.value.message
    assert "s3cr3t" not in caught.value.message


class _Client:
    def __init__(self, port: int) -> None:
        self.controller_url = f"http://127.0.0.1:{port}"
        self.auth_header = SECRET
        self.ssl_context = None


def test_run_follow_open_timeout_exit_6_hint_no_secret(
    stalled_tcp: int, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(typer.Exit) as exited:
        job_commands._follow_to_exit(_Client(stalled_tcp), "job_01ÜNI", 0.5)
    assert exited.value.exit_code == CliExit.UNAVAILABLE == 6
    out = capsys.readouterr()
    text = out.out + out.err
    assert "did not open within 0.5s" in text
    assert "keeps running" in text
    assert "compute logs -f job_01ÜNI" in text
    assert "s3cr3t" not in text and "Bearer" not in text


def test_run_follow_post_open_drop_exit_6_keeps_running(
    ws_server_that_drops: int, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(typer.Exit) as exited:
        job_commands._follow_to_exit(_Client(ws_server_that_drops), "job_1", 5.0)
    assert exited.value.exit_code == CliExit.UNAVAILABLE
    text = capsys.readouterr().err
    assert "keeps running" in text
    assert "did not open" not in text
    assert "s3cr3t" not in text
