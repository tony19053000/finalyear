"""D156 stabilisation: a log stream that does not open in time is reported
honestly - a typed ``timeout`` naming the budget and the setting that raises it
(not a bare ``(TimeoutError)``), and ``compute run -f`` says the submitted job
keeps running and how to re-attach (losing the stream never stops the job)."""

from __future__ import annotations

import io
from typing import Any

import pytest
import typer

from meshforge.cli import follow as follow_module
from meshforge.cli import job_commands
from meshforge.cli.client import ControllerError
from meshforge.cli.exits import CliExit
from meshforge.common.errors import FailureType

pytestmark = pytest.mark.unit


def _never_opens(*_args: object, **_kwargs: object) -> Any:
    async def opening() -> None:
        raise TimeoutError("timed out during opening handshake")

    return opening()


def test_open_timeout_is_a_typed_timeout_naming_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("websockets.asyncio.client.connect", _never_opens)
    with pytest.raises(ControllerError) as caught:
        follow_module.follow_logs(
            "wss://c:1/v1/jobs/x/logs/ws",
            auth_header="Bearer t",
            outputs={"stdout": io.BytesIO()},
            open_timeout_s=2.5,
        )
    assert caught.value.failure_type is FailureType.TIMEOUT
    assert "did not open within 2.5s" in caught.value.message
    assert "[cli] timeout_s" in caught.value.message


def test_a_timeout_after_the_open_is_not_reported_as_an_open_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the open is bounded by ``open_timeout_s``; a socket-level timeout
    later in the stream stays a network error, never "did not open"."""

    class Opened:
        def __await__(self) -> Any:
            async def opened() -> Opened:
                return self

            return opened().__await__()

        async def __aenter__(self) -> Opened:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        def __aiter__(self) -> Opened:
            return self

        async def __anext__(self) -> str:
            raise TimeoutError("read timed out")

    monkeypatch.setattr("websockets.asyncio.client.connect", lambda *a, **k: Opened())
    with pytest.raises(ControllerError) as caught:
        follow_module.follow_logs(
            "wss://c:1/v1/jobs/x/logs/ws",
            auth_header=None,
            outputs={"stdout": io.BytesIO()},
            open_timeout_s=2.5,
        )
    assert caught.value.failure_type is FailureType.NETWORK_ERROR
    assert "did not open" not in caught.value.message


class _Client:
    controller_url = "https://c:1"
    auth_header = "Bearer t"
    ssl_context = None


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (FailureType.TIMEOUT, CliExit.UNAVAILABLE),
        (FailureType.NETWORK_ERROR, CliExit.UNAVAILABLE),
        (FailureType.UNAUTHORIZED, CliExit.AUTH),
    ],
)
def test_run_follow_failure_says_the_job_keeps_running(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: FailureType,
    code: CliExit,
) -> None:
    def failing(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise ControllerError("the log stream did not open", failure_type=failure)

    monkeypatch.setattr(follow_module, "follow_logs", failing)
    with pytest.raises(typer.Exit) as exited:
        job_commands._follow_to_exit(_Client(), "job_01ABC", 5.0)
    assert exited.value.exit_code == code
    err = capsys.readouterr().err
    assert "the log stream did not open" in err
    assert "job_01ABC was submitted and keeps running" in err
    assert "compute logs -f job_01ABC" in err
    assert "compute cancel job_01ABC" in err
