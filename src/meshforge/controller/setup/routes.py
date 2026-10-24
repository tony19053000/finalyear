"""``/v1/setup/*`` - the "create fabric" page's API (V0.17 G2, D392, Q20).

Setup mode (:func:`create_setup_app`, plain HTTP on loopback only, behind
:class:`~meshforge.controller.web.host_guard.LoopbackHostGuard`):

* ``GET  /v1/setup/state`` -> ``{"state": "pending", "session": bool}`` (no
  secret in it; the page uses it to tell "open your setup link" from the form);
* ``POST /v1/setup/session`` ``{"token": ...}`` -> sets the HttpOnly
  ``mf_setup`` cookie (``SameSite=Strict``, path ``/v1/setup``); 401 for a
  wrong token;
* ``GET  /v1/setup/info`` (cookie) -> the checks (``doctor``), the address
  laptops will join, the dashboard URL;
* ``POST /v1/setup/complete`` (cookie) ``{"admin": ..., "password": ...}`` ->
  the fabric, the admin, the first invite (code shown once); 400 for a weak
  password, 401 without a session.

Every POST must come from the page itself: JSON body, and an ``Origin`` (when
sent) naming this loopback listener. After a successful ``complete`` - and
always in the normal mode (:data:`gone_router`) - every endpoint answers **410
Gone**.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Final
from urllib.parse import urlsplit

from fastapi import APIRouter, Cookie, Depends, FastAPI, Request, Response, status
from pydantic import Field, SecretStr
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from meshforge import __version__
from meshforge.common.errors import FailureType
from meshforge.controller.api.errors import ApiError, install_error_handlers
from meshforge.controller.api.models import ApiModel
from meshforge.controller.setup.service import SetupError, SetupService
from meshforge.controller.setup.state import SetupState
from meshforge.controller.web.host_guard import LOOPBACK_HOST_NAMES
from meshforge.controller.web.spa import (
    DEFAULT_DIST,
    SecurityHeadersMiddleware,
    SpaMiddleware,
)

__all__ = [
    "SESSION_COOKIE",
    "SETUP_PATHS",
    "SETUP_STATE_KEY",
    "create_setup_app",
    "gone_router",
    "setup_router",
]

SETUP_STATE_KEY: Final = "meshforge_setup"
SESSION_COOKIE: Final = "mf_setup"
_COOKIE_PATH: Final = "/v1/setup"
_COOKIE_MAX_AGE_S: Final = 3600
_GONE: Final = "this controller is already set up; sign in on its dashboard"
SETUP_PATHS: Final = frozenset(
    {
        ("GET", "/v1/setup/state"),
        ("POST", "/v1/setup/session"),
        ("GET", "/v1/setup/info"),
        ("POST", "/v1/setup/complete"),
    }
)
"""The (method, path) pairs; open routes of the normal app too (they are 410)."""

setup_router = APIRouter(prefix="/v1/setup", tags=["setup"])
gone_router = APIRouter(prefix="/v1/setup", tags=["setup"])


class SetupStateOut(ApiModel):
    state: str
    session: bool = False


class SessionIn(ApiModel):
    token: str = Field(min_length=1, max_length=128)


class CompleteIn(ApiModel):
    admin: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=1024)


def _service(request: Request) -> SetupService:
    service: SetupService | None = getattr(request.app.state, SETUP_STATE_KEY, None)
    if service is None:
        raise ApiError(FailureType.CONFIG_ERROR, "setup is not configured")
    return service


Service = Annotated[SetupService, Depends(_service)]
SessionCookie = Annotated[str | None, Cookie(alias=SESSION_COOKIE)]


def _gone() -> ApiError:
    return ApiError(FailureType.CONFLICT, _GONE, http_status=status.HTTP_410_GONE)


def _api_error(exc: SetupError) -> ApiError:
    if exc.details.get("reason") == "gone":
        return _gone()
    return ApiError(exc.failure_type, exc.message)


def _same_origin(request: Request) -> None:
    """A POST from another site (or not JSON) is refused before any work."""
    if "json" not in request.headers.get("content-type", "").lower():
        raise ApiError(
            FailureType.INVALID_INPUT,
            "send the request as JSON",
            http_status=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
    origin = request.headers.get("origin")
    if origin is None:
        return
    host = (urlsplit(origin).hostname or "").lower()
    if f"[{host}]" not in LOOPBACK_HOST_NAMES and host not in LOOPBACK_HOST_NAMES:
        raise ApiError(
            FailureType.UNAUTHORIZED,
            "cross-site setup request refused",
            http_status=status.HTTP_403_FORBIDDEN,
        )


async def _require_session(service: SetupService, session: str | None) -> None:
    state = await service.state()
    if state is not SetupState.PENDING:
        raise _gone()
    if not service.session_valid(session):
        raise ApiError(
            FailureType.UNAUTHORIZED,
            "open the setup link printed when the controller was installed "
            "(sudo meshforge-controller setup url)",
        )


# ------------------------------------------------------------ setup mode
@setup_router.get("/state", response_model=SetupStateOut)
async def get_state(service: Service, session: SessionCookie = None) -> dict[str, Any]:
    state = await service.state()
    if state is not SetupState.PENDING:
        raise _gone()
    return {"state": state.value, "session": service.session_valid(session)}


@setup_router.post("/session", response_model=SetupStateOut)
async def open_session(
    request: Request, body: SessionIn, response: Response, service: Service
) -> dict[str, Any]:
    _same_origin(request)
    try:
        session = await service.open_session(body.token)
    except SetupError as exc:
        raise _api_error(exc) from None
    response.set_cookie(
        SESSION_COOKIE,
        session,
        max_age=_COOKIE_MAX_AGE_S,
        path=_COOKIE_PATH,
        httponly=True,
        samesite="strict",
        secure=False,  # plain HTTP on loopback by design (setup mode only)
    )
    return {"state": SetupState.PENDING.value, "session": True}


@setup_router.get("/info")
async def get_info(service: Service, session: SessionCookie = None) -> dict[str, Any]:
    await _require_session(service, session)
    return await asyncio.to_thread(service.info)


@setup_router.post("/complete")
async def complete(
    request: Request,
    body: CompleteIn,
    response: Response,
    service: Service,
    session: SessionCookie = None,
) -> dict[str, Any]:
    _same_origin(request)
    await _require_session(service, session)
    try:
        result = await service.complete(body.admin, body.password)
    except SetupError as exc:
        raise _api_error(exc) from None
    response.delete_cookie(SESSION_COOKIE, path=_COOKIE_PATH)
    return result.as_dict()


# ----------------------------------------------------------- normal mode
@gone_router.get(
    "/state", status_code=status.HTTP_410_GONE, responses={410: {"description": "Gone"}}
)
async def gone_state() -> None:
    """The fabric exists: setup is over (410 forever, D392)."""
    raise _gone()


@gone_router.post(
    "/session",
    status_code=status.HTTP_410_GONE,
    responses={410: {"description": "Gone"}},
)
async def gone_session() -> None:
    raise _gone()


@gone_router.get(
    "/info", status_code=status.HTTP_410_GONE, responses={410: {"description": "Gone"}}
)
async def gone_info() -> None:
    raise _gone()


@gone_router.post(
    "/complete",
    status_code=status.HTTP_410_GONE,
    responses={410: {"description": "Gone"}},
)
async def gone_complete() -> None:
    raise _gone()


# ----------------------------------------------------------- the app
class _SetupRedirect:
    """``/`` and ``/login`` lead to ``/setup`` while the controller is not set
    up (the dashboard's other pages need a fabric)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") in ("/", "/login"):
            await RedirectResponse("/setup", status_code=307)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _SetupApp(FastAPI):
    def build_middleware_stack(self) -> ASGIApp:
        return SecurityHeadersMiddleware(
            _SetupRedirect(super().build_middleware_stack())
        )


def create_setup_app(service: SetupService, *, web_dist: Any = DEFAULT_DIST) -> FastAPI:
    """The setup-mode app: setup routes + ``/healthz`` + the dashboard files.
    Serve it behind ``LoopbackHostGuard`` on a loopback socket only."""
    app = _SetupApp(
        title="MeshForge controller setup",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    setattr(app.state, SETUP_STATE_KEY, service)
    install_error_handlers(app)
    if web_dist is not None:
        app.add_middleware(SpaMiddleware, dist=web_dist)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "setup"}

    app.include_router(setup_router)
    return app
