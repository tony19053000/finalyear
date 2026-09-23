"""Thin typed HTTP client for the controller's read-only ``/v1`` surface (0.3.12).

Why the payloads stay plain ``dict``: ``meshforge.cli`` may import only ``common``
and ``sdk`` (lint-imports), so it cannot reuse
``meshforge.controller.api.models``. Re-declaring those Pydantic models here would
create a second copy of the contract that nothing can compare against the first -
it would drift silently, and the renderer is the only consumer. So the client
validates the *envelope* (is this JSON? is it an object? did it fail?) and hands
the body through as a mapping; the renderer reads the handful of fields it needs
defensively and ``--json`` passes the controller's bytes through unchanged. A field
added server-side therefore appears in ``--json`` immediately and is ignored by the
table until someone renders it.

Every failure leaves this module as one :class:`ControllerError` carrying a
:class:`~meshforge.common.errors.FailureType` and a short, safe message - no
traceback, no httpx type, no server-side driver text. ``exit_code`` maps it to the
process exit code (``UNAVAILABLE`` for unreachable/timed out, D18).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Final, Self

import httpx

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError

__all__ = ["ControllerClient", "ControllerError", "exit_code_for"]

_MAX_ERROR_MESSAGE = 200
"""Server-supplied text is truncated: it reaches a terminal, and V0.3's REST
surface is unauthenticated, so it is not fully trusted input."""

_UNREACHABLE: Final = frozenset({FailureType.NETWORK_ERROR, FailureType.TIMEOUT})


class ControllerError(MeshForgeError):
    """The CLI could not get an answer it could use from the controller."""

    default_failure_type = FailureType.NETWORK_ERROR


def exit_code_for(error: ControllerError) -> ExitCode:
    """Map a client failure to a process exit code.

    ``UNAVAILABLE`` (D18) means "the controller could not be reached or did not
    answer in time" - distinct from a bad request (``FAILURE``) so a polling
    script can retry the first and stop on the second.
    """
    if error.failure_type in _UNREACHABLE:
        return ExitCode.UNAVAILABLE
    return ExitCode.FAILURE


def _clean(text: object) -> str:
    """One short printable line: control characters and newlines cannot reach a TTY."""
    if not isinstance(text, str):
        return ""
    flat = "".join(ch if ch.isprintable() else " " for ch in text).strip()
    return flat[:_MAX_ERROR_MESSAGE]


class ControllerClient:
    """Synchronous client over ``{controller_url}/v1``.

    Synchronous on purpose: ``compute`` makes one request per render, so an event
    loop would buy nothing and cost a traceback path on Ctrl-C.
    """

    def __init__(
        self,
        controller_url: str,
        *,
        timeout_s: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.controller_url = controller_url.rstrip("/")
        self.timeout_s = timeout_s
        self._client = httpx.Client(
            base_url=self.controller_url,
            timeout=timeout_s,
            transport=transport,
            follow_redirects=False,
            headers={"accept": "application/json"},
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------- endpoints

    def health(self) -> Mapping[str, Any]:
        return self._get("/healthz")

    def nodes(self) -> Mapping[str, Any]:
        return self._get("/v1/nodes")

    def node(self, node_id: str) -> Mapping[str, Any]:
        return self._get(f"/v1/nodes/{node_id}")

    def sessions(self, *, limit: int | None = None) -> Mapping[str, Any]:
        params = {} if limit is None else {"limit": str(limit)}
        return self._get("/v1/sessions", params=params)

    # -------------------------------------------------------------- internals

    def _get(
        self, path: str, *, params: Mapping[str, str] | None = None
    ) -> Mapping[str, Any]:
        try:
            response = self._client.get(path, params=dict(params or {}))
        except httpx.TimeoutException as exc:
            raise ControllerError(
                f"the controller at {self.controller_url} did not answer within "
                f"{self.timeout_s:g}s",
                failure_type=FailureType.TIMEOUT,
                details={"path": path},
            ) from exc
        except httpx.HTTPError as exc:
            # ConnectError, ConnectTimeout's siblings, DNS failures, protocol
            # errors: all "no usable answer from the endpoint". The httpx message
            # can be long and platform-specific, so only its type is kept.
            raise ControllerError(
                f"cannot reach the controller at {self.controller_url} "
                f"({type(exc).__name__})",
                failure_type=FailureType.NETWORK_ERROR,
                details={"path": path},
            ) from exc
        return self._decode(response, path)

    def _decode(self, response: httpx.Response, path: str) -> Mapping[str, Any]:
        body: Any = None
        try:
            body = json.loads(response.text) if response.content else None
        except ValueError:
            body = None

        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise self._envelope_error(response, body, path)
        if not isinstance(body, dict):
            raise ControllerError(
                f"the controller answered {path} with a body that is not a JSON "
                "object; is this URL really a MeshForge controller?",
                failure_type=FailureType.INTERNAL_ERROR,
                details={"status": response.status_code},
            )
        return body

    def _envelope_error(
        self, response: httpx.Response, body: Any, path: str
    ) -> ControllerError:
        """Turn the typed ``{"error": {...}}`` envelope into a typed local error."""
        code, message = "", ""
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                code, message = _clean(error.get("code")), _clean(error.get("message"))
        failure_type = _failure_type(code)
        status = response.status_code
        detail = message or f"HTTP {status}"
        if failure_type is FailureType.NETWORK_ERROR:
            # 503: the controller is up but its database is not - to a caller this
            # is still "the fabric cannot answer right now", hence UNAVAILABLE.
            return ControllerError(
                f"the controller cannot answer right now: {detail}",
                failure_type=FailureType.NETWORK_ERROR,
                details={"status": status, "path": path},
            )
        return ControllerError(
            f"the controller refused the request ({status}): {detail}",
            failure_type=failure_type,
            details={"status": status, "path": path, "code": code or None},
        )


def _failure_type(code: str) -> FailureType:
    """The envelope's ``code`` is a :class:`FailureType` value.

    Anything else (an empty body, a proxy's own error page, a future code this
    build does not know) becomes ``INTERNAL_ERROR``, which exits ``FAILURE`` - not
    ``UNAVAILABLE``, because a polling script must stop rather than hammer an
    endpoint that is answering something it cannot use. A new ``FailureType`` is a
    contract change and is not invented here.
    """
    try:
        return FailureType(code)
    except ValueError:
        return FailureType.INTERNAL_ERROR
