"""D202 tester r1: ``compute run -f`` re-login hint against a REAL websockets
server (not a monkeypatched connect). 403 handshake and a mid-stream 4401 close
are the two ways the real controller refuses a login; both must exit 3 (AUTH)
with the re-login hint and no secret in the output. Every other failure keeps
the ``logs -f`` hint."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import typer
from websockets.asyncio.server import serve

from meshforge.cli import job_commands
from meshforge.cli.exits import CliExit

pytestmark = [pytest.mark.component, pytest.mark.timeout(60)]

SECRET = "mfk_SUPERSECRETTOKENVALUE123"


class _Client:
    def __init__(self, port: int) -> None:
        self.controller_url = f"http://127.0.0.1:{port}"
        self.auth_header = f"Bearer {SECRET}"
        self.ssl_context = None


def _run_server(
    handler: Callable[[Any], Any] | None, process_request: Callable[..., Any] | None
) -> Iterator[int]:
    ready = threading.Event()
    box: dict[str, Any] = {}

    def main() -> None:
        async def go() -> None:
            stop = asyncio.Event()
            box["loop"], box["stop"] = asyncio.get_running_loop(), stop
            async with serve(
                handler or _noop, "127.0.0.1", 0, process_request=process_request
            ) as server:
                box["port"] = next(iter(server.sockets)).getsockname()[1]
                ready.set()
                await stop.wait()

        asyncio.run(go())

    thread = threading.Thread(target=main, daemon=True)
    thread.start()
    assert ready.wait(10)
    try:
        yield box["port"]
    finally:
        box["loop"].call_soon_threadsafe(box["stop"].set)
        thread.join(10)


async def _noop(ws: Any) -> None:
    await ws.wait_closed()


def _status_server(status: int) -> Callable[..., Any]:
    def process_request(connection: Any, request: Any) -> Any:
        return connection.respond(status, "refused\n")

    return process_request


def _close_server(code: int) -> Callable[[Any], Any]:
    async def handler(ws: Any) -> None:
        await ws.send('{"type":"hello"}')
        await ws.close(code=code, reason="nope")

    return handler


def _follow(port: int, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    with pytest.raises(typer.Exit) as exited:
        job_commands._follow_to_exit(_Client(port), "job_01ABC", 5.0)
    out = capsys.readouterr()
    return int(exited.value.exit_code), out.out, out.err


@pytest.fixture
def s403() -> Iterator[int]:
    yield from _run_server(None, _status_server(403))


@pytest.fixture
def s401() -> Iterator[int]:
    yield from _run_server(None, _status_server(401))


@pytest.fixture
def s500() -> Iterator[int]:
    yield from _run_server(None, _status_server(500))


@pytest.fixture
def c4401() -> Iterator[int]:
    yield from _run_server(_close_server(4401), None)


@pytest.fixture
def c4503() -> Iterator[int]:
    yield from _run_server(_close_server(4503), None)


@pytest.fixture
def c4404() -> Iterator[int]:
    yield from _run_server(_close_server(4404), None)


def _relogin(err: str) -> None:
    assert "compute login" in err
    assert "logs -f" not in err
    assert "keeps running" in err


def _old_hint(err: str) -> None:
    assert "compute logs -f job_01ABC" in err
    assert "compute cancel job_01ABC" in err


def test_real_403_handshake_exit_3_relogin_hint(
    s403: int, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _follow(s403, capsys)
    assert code == CliExit.AUTH == 3
    _relogin(err)
    assert SECRET not in out + err and "Bearer" not in out + err


def test_real_4401_midstream_close_exit_3_relogin_hint(
    c4401: int, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _follow(c4401, capsys)
    assert code == CliExit.AUTH
    _relogin(err)
    assert SECRET not in out + err


def test_real_401_handshake_is_not_silently_misclassified(
    s401: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """A proxy answering 401 on the upgrade: the user must not be told to run
    `logs -f` (it would be refused the same way) and the exit should be AUTH."""
    code, out, err = _follow(s401, capsys)
    assert SECRET not in out + err
    assert code == CliExit.AUTH, (code, err)
    _relogin(err)


@pytest.mark.parametrize("fixture_name", ["s500", "c4503", "c4404"])
def test_other_failures_keep_the_old_hint(
    fixture_name: str,
    request: pytest.FixtureRequest,
    capsys: pytest.CaptureFixture[str],
) -> None:
    port = request.getfixturevalue(fixture_name)
    code, out, err = _follow(port, capsys)
    assert code != CliExit.AUTH
    _old_hint(err)
    assert SECRET not in out + err
