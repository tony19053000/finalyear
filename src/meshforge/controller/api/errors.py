"""One error envelope for every ``/v1`` response that is not a success (0.3.12).

Every failure leaves the API as::

    {"error": {"code": "<FailureType value>", "message": "<short, safe>"}}

``code`` is a value of :class:`~meshforge.common.errors.FailureType` - the same
vocabulary the gRPC side, the database and (later) the ML labels use - never a bare
FastAPI ``{"detail": ...}``. ``message`` is written by us: it never echoes a
peer-controlled value (a path parameter can be megabytes of anything) and never
carries a driver message that might name a host, a user or a password.

FastAPI's own handlers are replaced rather than wrapped, so a validation error
(``limit=0``), a 404 and an unexpected exception all look the same to a client.
The last one needs the catch-all ``Exception`` handler below: without it a bug in
a shaper (a ``KeyError``) or a ``ResponseValidationError`` leaves Starlette's
default ``text/plain`` ``Internal Server Error``, which has no ``code`` at all.
"""

from __future__ import annotations

from typing import Final, cast

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.controller.registry import RegistryError, UnknownNodeError
from meshforge.controller.store.errors import DatabaseUnavailableError

__all__ = [
    "GENERIC_5XX_MESSAGE",
    "ApiError",
    "error_body",
    "error_response",
    "install_error_handlers",
]

log = get_logger(__name__)

GENERIC_5XX_MESSAGE: Final = "the controller failed to handle this request"
"""What every 5xx says. An internal failure's own text can name a host, a user, a
path or a query value, so it is logged and never returned."""

_STATUS_FOR: Final[dict[FailureType, int]] = {
    FailureType.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    FailureType.INVALID_INPUT: status.HTTP_400_BAD_REQUEST,
    FailureType.UNAUTHORIZED: status.HTTP_401_UNAUTHORIZED,
    FailureType.CONFLICT: status.HTTP_409_CONFLICT,
    FailureType.NETWORK_ERROR: status.HTTP_503_SERVICE_UNAVAILABLE,
    FailureType.TIMEOUT: status.HTTP_504_GATEWAY_TIMEOUT,
    FailureType.CONFIG_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
    FailureType.INTERNAL_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
}

_STATUS_FALLBACK: Final[dict[int, FailureType]] = {
    status.HTTP_400_BAD_REQUEST: FailureType.INVALID_INPUT,
    status.HTTP_401_UNAUTHORIZED: FailureType.UNAUTHORIZED,
    status.HTTP_403_FORBIDDEN: FailureType.UNAUTHORIZED,
    status.HTTP_404_NOT_FOUND: FailureType.NOT_FOUND,
    status.HTTP_405_METHOD_NOT_ALLOWED: FailureType.INVALID_INPUT,
    status.HTTP_409_CONFLICT: FailureType.CONFLICT,
    422: FailureType.INVALID_INPUT,  # starlette renamed the constant; the code did not
    status.HTTP_503_SERVICE_UNAVAILABLE: FailureType.NETWORK_ERROR,
}


class ApiError(Exception):
    """A failure the API raises on purpose; carries its typed code and message."""

    def __init__(
        self,
        failure_type: FailureType,
        message: str,
        *,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.message = message
        self.http_status = http_status or _STATUS_FOR.get(
            failure_type, status.HTTP_500_INTERNAL_SERVER_ERROR
        )

    @classmethod
    def not_found(cls, what: str) -> ApiError:
        """``what`` is written by us, never a value taken from the request."""
        return cls(FailureType.NOT_FOUND, f"no such {what}")

    @classmethod
    def invalid(cls, message: str) -> ApiError:
        return cls(FailureType.INVALID_INPUT, message)


def error_body(failure_type: FailureType, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": failure_type.value, "message": message}}


def error_response(failure_type: FailureType, message: str, code: int) -> JSONResponse:
    return JSONResponse(status_code=code, content=error_body(failure_type, message))


def install_error_handlers(app: FastAPI) -> None:
    """Replace FastAPI's default error bodies with the envelope above."""

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: Exception) -> JSONResponse:
        # Starlette types every handler as ``(Request, Exception)``; the narrowing
        # is a cast, not a runtime check, so these handlers do not depend on
        # ``assert`` (which ``python -O`` removes).
        error = cast("ApiError", exc)
        return error_response(error.failure_type, error.message, error.http_status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: Exception) -> JSONResponse:
        # The location (query parameter name) is ours; the *value* is not, so only
        # the field name and our own constraint text are returned.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', ())[1:]) or '<request>'}: "
            f"{err.get('msg', 'invalid')}"
            for err in cast("RequestValidationError", exc).errors()[:5]
        )
        return error_response(
            FailureType.INVALID_INPUT,
            f"invalid request parameters: {problems}",
            status.HTTP_400_BAD_REQUEST,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: Exception) -> JSONResponse:
        http_exc = cast("StarletteHTTPException", exc)
        failure_type = _STATUS_FALLBACK.get(http_exc.status_code) or (
            # A status we have not mapped: fall back on the class of the code
            # rather than on INTERNAL_ERROR, so a 4xx never tells a client the
            # *controller* failed when the client's request was at fault. Every
            # raise site is ours today, so this only matters for a status added
            # later - which is exactly when a wrong `code` would mislead.
            FailureType.INVALID_INPUT
            if http_exc.status_code < status.HTTP_500_INTERNAL_SERVER_ERROR
            else FailureType.INTERNAL_ERROR
        )
        detail = http_exc.detail if isinstance(http_exc.detail, str) else ""
        return error_response(
            failure_type,
            detail or failure_type.value.replace("_", " "),
            http_exc.status_code,
        )

    @app.exception_handler(UnknownNodeError)
    async def _unknown_node(_request: Request, _exc: Exception) -> JSONResponse:
        return error_response(
            FailureType.NOT_FOUND, "no such node", status.HTTP_404_NOT_FOUND
        )

    @app.exception_handler(RegistryError)
    async def _registry(_request: Request, exc: Exception) -> JSONResponse:
        # A database that is down or refusing is *our* problem, and its driver
        # message can name the host and the user - so it is logged, not returned.
        registry_exc = cast("RegistryError", exc)
        log.warning(
            "controller.api.registry_unavailable",
            failure_type=registry_exc.failure_type.value,
            details=registry_exc.details,
        )
        return error_response(
            FailureType.NETWORK_ERROR,
            "the controller's database is unavailable; retry shortly",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.exception_handler(DatabaseUnavailableError)
    async def _database(_request: Request, _exc: Exception) -> JSONResponse:
        return error_response(
            FailureType.NETWORK_ERROR,
            "the controller's database is unavailable; retry shortly",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.exception_handler(MeshForgeError)
    async def _meshforge(_request: Request, exc: Exception) -> JSONResponse:
        error = cast("MeshForgeError", exc)
        code = _STATUS_FOR.get(
            error.failure_type, status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        log.warning(
            "controller.api.request_failed",
            failure_type=error.failure_type.value,
            error=error.message,
        )
        message = (
            error.message
            if code < status.HTTP_500_INTERNAL_SERVER_ERROR
            else GENERIC_5XX_MESSAGE
        )
        return error_response(error.failure_type, message, code)

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, _exc: Exception) -> JSONResponse:
        """Last resort: anything not handled above is still the envelope.

        Registering ``Exception`` does **not** shadow the handlers above: Starlette
        routes the ``Exception``/``500`` key to ``ServerErrorMiddleware`` (the
        outermost layer) and every other key to ``ExceptionMiddleware`` (inside the
        router), and the inner one resolves by walking ``type(exc).__mro__``. So a
        ``RegistryError`` still reaches ``_registry``; only what escapes that
        middleware arrives here.

        The traceback goes to the log, never to the client, and the message is the
        same generic 5xx text the ``MeshForgeError`` handler uses - an unexpected
        exception may carry a path, a query or a driver string.
        """
        # Starlette calls this from inside ``except Exception as exc``, so
        # ``sys.exc_info()`` is live and ``.exception`` records the traceback -
        # which is exactly why it must not be downgraded to ``.error``.
        log.exception("controller.api.unhandled")  # noqa: LOG004
        return error_response(
            FailureType.INTERNAL_ERROR,
            GENERIC_5XX_MESSAGE,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
