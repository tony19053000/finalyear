"""Follow a job's logs over the controller's WebSocket (V0.4 G5, D72/D76).

A blocking call, like the rest of ``compute``, that runs the **asyncio**
WebSocket client on a private event loop (V0.7 G4 item 0, D156 e). Not
``websockets.sync``: that client reads the socket in a background thread while
the caller's thread writes it, and over TLS that means two threads inside one
OpenSSL connection at once - unsupported by OpenSSL. With TLS 1.3 the server's
post-handshake session tickets are processed by the reader thread exactly while
the handshake request is being written, and about one open in a few lost the
request or the response (``TimeoutError`` waiting for the handshake response,
the V0.7 G3 cluster flake). The asyncio client drives the TLS object from one
thread only. The API token goes in the handshake's ``Authorization`` header -
never in the URL. Frames are
written to this process's stdout/stderr as raw bytes (the workload's own bytes,
not re-encoded); the end frame is returned to the caller, which turns it into an
exit code. A retry notice (V0.6 G3) goes to ``on_retry``. A typed close from
the controller (slow consumer, too many followers, unknown job) becomes a
:class:`~.client.ControllerError`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import ssl
from collections.abc import Callable, Mapping
from typing import IO, Any, Final

from meshforge.cli.client import ControllerError
from meshforge.common.errors import FailureType

__all__ = ["follow_logs", "ws_url"]

_MAX_FRAME: Final = 1024 * 1024
_CLOSE_REASONS: Final = {
    4008: "the controller dropped this follower because it read too slowly",
    4029: "too many followers on this job; try again later",
    4404: "no such job",
    4400: "not a valid job id",
    1008: "the controller refused the log stream (not logged in, or the login "
    "expired or was revoked: run `compute login`)",
    4401: "the controller closed the log stream: this login was revoked or "
    "expired, or the user was disabled (run `compute login`)",
    4503: "the controller closed the log stream: it could not check the login "
    "(user database unavailable); try again",
}


def ws_url(controller_url: str, job_id: str, *, stream: str) -> str:
    base = controller_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base.removeprefix("https://")
    elif base.startswith("http://"):
        base = "ws://" + base.removeprefix("http://")
    return f"{base}/v1/jobs/{job_id}/logs/ws?stream={stream}"


def follow_logs(
    url: str,
    *,
    auth_header: str | None,
    outputs: Mapping[str, IO[bytes]],
    open_timeout_s: float,
    on_retry: Callable[[Mapping[str, Any]], None] | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> dict[str, Any]:
    """Print the job's logs until the end frame; return that frame.
    ``on_retry`` gets the controller's retry notice (V0.6 G3: the followed
    attempt ended, the job was retried, the next frames are the new
    attempt's)."""
    from websockets.exceptions import (
        ConnectionClosed,
        InvalidStatus,
        WebSocketException,
    )

    try:
        end = asyncio.run(
            _follow(
                url,
                auth_header=auth_header,
                outputs=outputs,
                open_timeout_s=open_timeout_s,
                on_retry=on_retry,
                ssl_context=ssl_context,
            )
        )
    except InvalidStatus as exc:
        status = exc.response.status_code
        raise ControllerError(
            _CLOSE_REASONS[1008]
            if status == 403
            else f"log stream refused (HTTP {status})",
            failure_type=FailureType.UNAUTHORIZED
            if status == 403
            else FailureType.INTERNAL_ERROR,
        ) from None
    except ConnectionClosed as exc:
        code = exc.rcvd.code if exc.rcvd is not None else None
        raise ControllerError(
            _CLOSE_REASONS.get(code or 0, "the log stream closed before the job ended"),
            failure_type=FailureType.NOT_FOUND
            if code == 4404
            else FailureType.UNAUTHORIZED
            if code == 4401
            else FailureType.NETWORK_ERROR,
        ) from None
    except (OSError, TimeoutError, WebSocketException) as exc:
        raise ControllerError(
            f"cannot follow the job's logs ({type(exc).__name__})",
            failure_type=FailureType.NETWORK_ERROR,
        ) from None
    if end is not None:
        return end
    raise ControllerError(
        "the log stream closed before the job ended",
        failure_type=FailureType.NETWORK_ERROR,
    )


def _frame(raw: str | bytes) -> dict[str, Any]:
    try:
        frame = json.loads(raw)
    except ValueError:
        raise _bad_frame() from None
    if not isinstance(frame, dict):
        raise _bad_frame()
    return frame


def _bad_frame() -> ControllerError:
    return ControllerError(
        "the controller sent a log frame this compute cannot read",
        failure_type=FailureType.INTERNAL_ERROR,
    )


async def _follow(
    url: str,
    *,
    auth_header: str | None,
    outputs: Mapping[str, IO[bytes]],
    open_timeout_s: float,
    on_retry: Callable[[Mapping[str, Any]], None] | None,
    ssl_context: ssl.SSLContext | None,
) -> dict[str, Any] | None:
    """The stream itself; the end frame, or ``None`` if it closed without one."""
    from websockets.asyncio.client import connect

    headers = {"Authorization": auth_header} if auth_header else {}
    try:
        ws = await connect(
            url,
            additional_headers=headers,
            open_timeout=open_timeout_s,
            max_size=_MAX_FRAME,
            ssl=ssl_context,
        )
    except TimeoutError:
        # Only the open is bounded (TCP + TLS + the HTTP upgrade, which the
        # controller answers after one auth read); once open the stream may
        # stay quiet for as long as the job runs.
        raise ControllerError(
            f"the log stream did not open within {open_timeout_s:g}s - the "
            "controller may be overloaded or the network slow; retry, or raise "
            "[cli] timeout_s",
            failure_type=FailureType.TIMEOUT,
        ) from None
    async with ws:
        async for raw in ws:
            frame = _frame(raw)
            if frame.get("end") is True:
                return frame
            if frame.get("retry") is True:
                if on_retry is not None:
                    on_retry(frame)
                continue
            out = outputs.get(str(frame.get("stream")))
            if out is None:
                continue
            try:
                data = base64.b64decode(str(frame.get("data", "")), validate=True)
            except (binascii.Error, ValueError):
                raise _bad_frame() from None
            out.write(data)
            out.flush()
    return None
