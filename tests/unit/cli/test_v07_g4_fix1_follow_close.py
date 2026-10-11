"""D157: the CLI explains the controller's auth closes of a log follow."""

from __future__ import annotations

import io
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from meshforge.cli import follow as follow_module
from meshforge.cli.client import ControllerError
from meshforge.common.errors import FailureType

pytestmark = pytest.mark.unit


def _closing(code: int, reason: str) -> Any:
    async def fake(*_args: object, **_kwargs: object) -> None:
        raise ConnectionClosedError(Close(code, reason), None)

    return fake


@pytest.mark.parametrize(
    ("code", "reason", "failure", "words"),
    [
        (4401, "unauthorized", FailureType.UNAUTHORIZED, "compute login"),
        (4503, "auth_unavailable", FailureType.NETWORK_ERROR, "try again"),
    ],
)
def test_auth_closes_are_explained_and_typed(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    reason: str,
    failure: FailureType,
    words: str,
) -> None:
    monkeypatch.setattr(follow_module, "_follow", _closing(code, reason))
    with pytest.raises(ControllerError) as caught:
        follow_module.follow_logs(
            "wss://c:1/v1/jobs/x/logs/ws",
            auth_header="Bearer t",
            outputs={"stdout": io.BytesIO()},
            open_timeout_s=1,
        )
    assert caught.value.failure_type is failure
    assert words in caught.value.message
