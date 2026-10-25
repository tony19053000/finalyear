"""The console's HTTP surface: ``/console/v1/...`` (JSON) + the built UI.

Every request passes :class:`GuardMiddleware` first:

* ``Host`` must be exactly ``127.0.0.1:<port>`` - else ``421`` (a DNS-rebinding
  page sends its own name; nothing is served to it, not even the UI);
* every response gets the dashboard's security headers
  (:mod:`meshforge.common.web_security`) and API answers ``no-store``.

Then, per route (:func:`_session`): the ``mf_console`` cookie must name a live
session (``401``), and a write must carry ``Origin: http://127.0.0.1:<port>``
(``403``, CSRF). Two routes have their own rule: ``POST /console/v1/session``
(trade the launcher's one-time code or the token for the cookie; Origin
required, throttled) and ``POST /console/v1/launch-code`` (``Authorization:
Bearer <token>``, for the launcher; a browser ``Origin`` is refused).
``GET /console/v1/health`` needs only the Host check (the launcher waits on it).

Errors are ``{"error": {"code", "message", "fix"}}`` in plain words.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, cast

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from starlette.responses import FileResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from meshforge import __version__
from meshforge.agent.console.auth import (
    COOKIE_NAME,
    ConsoleAuth,
    host_ok,
    origin_ok,
)
from meshforge.agent.console.checks import DOCKER_ACCESS_COMMAND, run_checks
from meshforge.agent.console.joining import JoinProblemError, join_from_console
from meshforge.agent.console.logbuffer import LogBuffer
from meshforge.agent.console.models import (
    ChecksView,
    CheckView,
    ControllerView,
    DiscoverView,
    DockerAccessView,
    ExchangeRequest,
    HealthView,
    JoinRequest,
    JoinView,
    LaunchCodeView,
    LeaveRequest,
    LogLineView,
    LogsView,
    NodeNameRequest,
    NodeNameView,
    OkView,
    PolicyRequest,
    PolicyView,
    StatusView,
    StopReasonView,
)
from meshforge.agent.console.node_name import (
    node_name_path,
    read_node_name,
    write_node_name,
)
from meshforge.agent.console.setup_state import (
    clear_setup,
    mark_setup_complete,
    setup_complete,
)
from meshforge.agent.console.supervisor import FabricPhase, Supervisor
from meshforge.agent.settings import AgentSettings
from meshforge.common.config import InstallMode, install_mode
from meshforge.common.errors import ConfigError, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.common.web_security import SECURITY_HEADERS

__all__ = ["API_PREFIX", "DEFAULT_DIST", "ConsoleContext", "create_app"]

log = get_logger(__name__)

API_PREFIX: Final = "/console/v1"
DEFAULT_DIST: Final = Path(__file__).resolve().parent / "dist"
DASHBOARD_PORT: Final = 47110
_UNSAFE: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_NOT_NULLABLE: Final = frozenset(
    {
        "accept_jobs",
        "accept_gpu_sessions",
        "allow_unsandboxed",
        "allow_network",
        "pause_benchmarks",
        "max_cpu_jobs",
        "allowed_users",
    }
)
"""Policy keys a PUT may not set to ``null`` (only limits can be removed)."""
_IMMUTABLE: Final = "public, max-age=31536000, immutable"
NOT_BUILT_HTML: Final = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>MeshForge</title></head>
<body><h1>MeshForge console not built</h1>
<p>The agent is running, but this installation has no built console UI.
Developers: <code>cd frontend &amp;&amp; npm ci &amp;&amp; npm run build</code>.</p>
</body></html>
"""


@dataclass(slots=True)
class ConsoleContext:
    supervisor: Supervisor
    auth: ConsoleAuth
    logs: LogBuffer | None = None
    dist: Path = DEFAULT_DIST
    cpu_threads: Callable[[], int | None] = os.cpu_count

    @property
    def settings(self) -> AgentSettings:
        return self.supervisor.settings


# ------------------------------------------------------------------ errors


class ConsoleHTTPError(Exception):
    def __init__(self, status: int, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.fix = status, code, message, fix


def _error(status: int, code: str, message: str, fix: str = "") -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "fix": fix}}, status_code=status
    )


# -------------------------------------------------------------- middleware


class GuardMiddleware:
    """Host check + security headers (module docstring)."""

    def __init__(self, app: ASGIApp, *, port: Callable[[], int]) -> None:
        self.app = app
        self.port = port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":  # no WebSockets here
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        host = None
        for key, value in scope.get("headers", ()):
            if key == b"host":
                host = value.decode("latin-1")
                break
        is_api = str(scope.get("path", "")).startswith(API_PREFIX)

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for name, value in SECURITY_HEADERS:
                    if name.encode() not in present:
                        headers.append((name.encode(), value.encode()))
                if is_api and b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        if not host_ok(host, self.port()):
            response = _error(
                421,
                "wrong_host",
                "The MeshForge console answers only at http://127.0.0.1 on this "
                "laptop.",
                "Open it from the MeshForge entry in the app menu.",
            )
            await response(scope, receive, with_headers)
            return
        await self.app(scope, receive, with_headers)


# ----------------------------------------------------------- dependencies


def _ctx(request: Request) -> ConsoleContext:
    ctx: ConsoleContext = request.app.state.console
    return ctx


def _session(request: Request) -> ConsoleContext:
    """Origin first (a cross-site write learns nothing about the session, not
    even whether the cookie is valid), then the session."""
    ctx = _ctx(request)
    if request.method in _UNSAFE and not origin_ok(
        request.headers.get("origin"), ctx.auth.port
    ):
        raise ConsoleHTTPError(
            403, "bad_origin", "This request did not come from the console page."
        )
    if not ctx.auth.session_ok(request.cookies.get(COOKIE_NAME)):
        raise ConsoleHTTPError(
            401,
            "signed_out",
            "You are not signed in to this console.",
            "Open MeshForge from the app menu.",
        )
    return ctx


_Session = Annotated[ConsoleContext, Depends(_session)]


# ------------------------------------------------------------------ helpers


def _hostname() -> str:
    return socket.gethostname() or "this laptop"


def _dashboard_url(controller: str | None) -> str | None:
    if not controller:
        return None
    host, sep, _port = controller.rpartition(":")
    if not sep or not host:
        return None
    return f"https://{host}:{DASHBOARD_PORT}/"


def _identity_facts(settings: AgentSettings) -> dict[str, Any]:
    """node id + enrolment, read-only (blocking: call in a thread)."""
    from meshforge.agent.enrollment import JoinError, load
    from meshforge.agent.identity import read_identity

    facts: dict[str, Any] = {
        "node_id": None,
        "fabric_id": None,
        "cert_not_after": None,
        "joined": False,
        "identity_problem": None,
    }
    if not (settings.identity_dir / "node.key").exists():
        return facts
    try:
        identity = read_identity(settings.identity_dir)
    except MeshForgeError as exc:
        facts["identity_problem"] = exc.message
        return facts
    facts["node_id"] = str(identity.node_id)
    try:
        enrollment = load(identity)
    except JoinError as exc:
        facts["identity_problem"] = exc.message
        return facts
    if enrollment is not None:
        facts.update(
            joined=True,
            fabric_id=str(enrollment.fabric_id),
            cert_not_after=enrollment.not_after.isoformat(),
        )
    return facts


def _status(ctx: ConsoleContext, facts: dict[str, Any]) -> StatusView:
    sup = ctx.supervisor
    client = sup.client
    settings = sup.settings
    try:
        stored = read_node_name(node_name_path(settings))
    except ConfigError:
        stored = None
    name = settings.agent.node_name if stored is None else stored
    controller = client.controller_target if client is not None else None
    return StatusView(
        version=__version__,
        install_mode="system" if install_mode() is InstallMode.SYSTEM else "user",
        phase=sup.phase.value,
        phase_since=sup.phase_since,
        stop_reason=None
        if sup.stop_reason is None
        else StopReasonView(
            code=sup.stop_reason.code,
            message=sup.stop_reason.message,
            failure_type=sup.stop_reason.failure_type,
        ),
        joined=bool(facts["joined"]),
        setup_complete=setup_complete(settings),
        node_id=facts["node_id"],
        node_name=name,
        hostname=_hostname(),
        fabric_id=facts["fabric_id"],
        cert_not_after=facts["cert_not_after"],
        identity_problem=facts["identity_problem"],
        connection=None if client is None else client.state.value,
        controller=controller or None,
        session_id=None if client is None else (client.session_id or None),
        dashboard_url=_dashboard_url(controller),
    )


def _policy_view(ctx: ConsoleContext, restart: tuple[str, ...] = ()) -> PolicyView:
    from meshforge.agent.policy import policy_view

    view = policy_view(ctx.settings)
    return PolicyView(
        policy=view["policy"],
        max_cpu_jobs=view["max_cpu_jobs"],
        max_gpu_jobs=view["max_gpu_jobs"],
        live_keys=view["live_keys"],
        invalid=view["invalid"],
        deprecated_keys=view["deprecated_keys"],
        restart_needed=list(restart),
        owner_activity_guard=ctx.settings.agent.bench.activity,
        cpu_threads=ctx.cpu_threads(),
    )


def _enrolled(settings: AgentSettings) -> bool:
    from meshforge.agent.enrollment import is_enrolled

    try:
        return is_enrolled(settings.identity_dir)
    except OSError:
        return False


def _peer_endpoint(sup: Supervisor) -> str | None:
    client = sup.client
    if client is None or client.peer_endpoint is None:
        return None
    try:
        return client.peer_endpoint() or None
    except Exception:  # noqa: BLE001 - a status read never fails the page
        return None


# --------------------------------------------------------------------- app


def create_app(ctx: ConsoleContext) -> FastAPI:
    app = FastAPI(
        title="MeshForge agent console",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.console = ctx
    app.add_middleware(GuardMiddleware, port=lambda: ctx.auth.port)

    @app.exception_handler(ConsoleHTTPError)
    async def _console_error(_request: Request, exc: ConsoleHTTPError) -> Response:
        return _error(exc.status, exc.code, exc.message, exc.fix)

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _invalid(_request: Request, exc: RequestValidationError) -> Response:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        return _error(
            422,
            "invalid",
            f"{where or 'request'}: {first.get('msg', 'invalid value')}",
        )

    # ------------------------------------------------------------ access
    @app.get(f"{API_PREFIX}/health", response_model=HealthView)
    async def health() -> HealthView:
        return HealthView(ok=True, version=__version__)

    @app.post(f"{API_PREFIX}/launch-code", response_model=LaunchCodeView)
    async def launch_code(request: Request) -> LaunchCodeView:
        auth = _ctx(request).auth
        if request.headers.get("origin") is not None:
            raise ConsoleHTTPError(403, "bad_origin", "Not for browsers.")
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        # G3-D1: the credential is checked FIRST; the throttle only answers
        # failed tries, so a stranger's wrong guesses never lock the owner out.
        if scheme.lower() != "bearer" or not auth.token_matches(token.strip()):
            if auth.throttled():
                raise ConsoleHTTPError(
                    429, "throttled", "Too many tries; wait a minute."
                )
            raise ConsoleHTTPError(401, "bad_token", "The console token is not valid.")
        code = auth.new_launch_code()
        return LaunchCodeView(
            code=code,
            url=f"http://127.0.0.1:{auth.port}/#c={code}",
            expires_in_s=ctx.settings.agent.console.launch_code_ttl_s,
        )

    # The body is read by hand (Origin first, before it is parsed), so its
    # schema is declared here to keep it in the OpenAPI contract.
    @app.post(
        f"{API_PREFIX}/session",
        response_model=OkView,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": ExchangeRequest.model_json_schema()}
                },
            }
        },
    )
    async def exchange(request: Request) -> Response:
        auth = _ctx(request).auth
        # Origin before anything else, also before the body is read.
        if not origin_ok(request.headers.get("origin"), auth.port):
            raise ConsoleHTTPError(
                403, "bad_origin", "This request did not come from the console page."
            )
        try:
            body = ExchangeRequest.model_validate_json(await request.body())
        except ValueError:
            raise ConsoleHTTPError(422, "invalid", 'Expected {"code": ...}.') from None
        # G3-D1: the code is checked first; only a failed try meets the throttle.
        if not auth.redeem_launch_code(body.code):
            if auth.throttled():
                raise ConsoleHTTPError(
                    429, "throttled", "Too many tries; wait a minute."
                )
            raise ConsoleHTTPError(
                401,
                "link_expired",
                "This console link has expired or was already used.",
                "Open MeshForge from the app menu again.",
            )
        session = auth.new_session()
        response = JSONResponse(OkView().model_dump())
        response.set_cookie(
            COOKIE_NAME,
            session,
            max_age=int(ctx.settings.agent.console.session_ttl_s),
            httponly=True,
            samesite="strict",
            secure=False,  # plain http on loopback; never leaves this laptop
            path="/",
        )
        log.info("agent.console.signed_in")
        return response

    @app.delete(f"{API_PREFIX}/session", response_model=OkView)
    async def sign_out(request: Request, c: _Session) -> Response:
        c.auth.end_session(request.cookies.get(COOKIE_NAME))
        response = JSONResponse(OkView().model_dump())
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    # ------------------------------------------------------------ status
    @app.get(f"{API_PREFIX}/status", response_model=StatusView)
    async def status(c: _Session) -> StatusView:
        facts = await asyncio.to_thread(_identity_facts, c.settings)
        return _status(c, facts)

    @app.get(f"{API_PREFIX}/discover", response_model=DiscoverView)
    async def discover(c: _Session) -> DiscoverView:
        from meshforge.agent.discovery import browse_controllers

        try:
            found = await browse_controllers(
                c.settings.agent.discovery,
                timeout_s=min(3.0, c.settings.agent.discovery.timeout_s),
            )
        except ConfigError as exc:
            return DiscoverView(controllers=[], problem=exc.message)
        return DiscoverView(
            controllers=[
                ControllerView(name=f.name, host=f.host, port=f.port) for f in found
            ]
        )

    @app.post(f"{API_PREFIX}/join", response_model=JoinView)
    async def join(body: JoinRequest, c: _Session) -> JoinView:
        if await asyncio.to_thread(_enrolled, c.settings):
            raise ConsoleHTTPError(
                409,
                "already_joined",
                "This laptop has already joined a fabric.",
                "To join another one, leave the current fabric first.",
            )
        try:
            result = await join_from_console(
                c.settings, body.code, host=body.controller_host or None
            )
        except JoinProblemError as exc:
            p = exc.problem
            raise ConsoleHTTPError(400, p.code, p.message, p.fix) from None
        c.supervisor.request("enrolled")
        e = result.enrollment
        log.info("agent.console.joined", fabric_id=e.fabric_id, node_id=e.node_id)
        return JoinView(
            fabric_id=str(e.fabric_id),
            node_id=str(e.node_id),
            controller=result.address,
            cert_not_after=e.not_after.isoformat(),
        )

    @app.post(f"{API_PREFIX}/setup-complete", response_model=OkView)
    async def setup_done(c: _Session) -> OkView:
        from meshforge.agent.policy import policy_path

        # Q26, server-enforced: the wizard's policy step cannot be skipped by
        # calling this directly - the owner's choice must have been saved.
        if not await asyncio.to_thread(policy_path(c.settings).is_file):
            raise ConsoleHTTPError(
                409,
                "policy_not_saved",
                "Choose what this laptop shares first.",
                "Save the settings in the 'What this laptop shares' step.",
            )
        try:
            await asyncio.to_thread(mark_setup_complete, c.settings)
        except OSError as exc:
            raise ConsoleHTTPError(
                500, "storage", f"Could not save: {exc.strerror or exc}"
            ) from None
        return OkView()

    # ------------------------------------------------------------ policy
    @app.get(f"{API_PREFIX}/policy", response_model=PolicyView)
    async def get_policy(c: _Session) -> PolicyView:
        return await asyncio.to_thread(_policy_view, c)

    @app.put(f"{API_PREFIX}/policy", response_model=PolicyView)
    async def put_policy(body: PolicyRequest, c: _Session) -> PolicyView:
        from meshforge.agent.policy import apply_changes

        changes = body.values.model_dump(exclude_unset=True)
        for key, value in changes.items():
            if value is None and key in _NOT_NULLABLE:
                raise ConsoleHTTPError(422, "invalid", f"{key} cannot be empty")
        if not changes:
            return await asyncio.to_thread(_policy_view, c)
        try:
            result = await asyncio.to_thread(apply_changes, c.settings, changes)
        except MeshForgeError as exc:
            raise ConsoleHTTPError(400, "invalid_policy", exc.message) from None
        except OSError as exc:
            raise ConsoleHTTPError(
                500, "storage", f"Could not save the settings: {exc.strerror or exc}"
            ) from None
        log.info("agent.console.policy_changed", keys=sorted(changes))
        return await asyncio.to_thread(_policy_view, c, result.restart_keys)

    # --------------------------------------------------------- node name
    def _name_view(c: ConsoleContext) -> NodeNameView:
        try:
            stored = read_node_name(node_name_path(c.settings))
        except ConfigError:
            stored = None
        if stored is not None:
            name, source = stored, "console"
        elif c.settings.agent.node_name:
            name, source = c.settings.agent.node_name, "config"
        else:
            name, source = "", "none"
        return NodeNameView(
            name=name,
            source=source,  # type: ignore[arg-type]
            hostname=_hostname(),
            applies_after_restart=c.supervisor.phase is FabricPhase.RUNNING,
        )

    @app.get(f"{API_PREFIX}/node-name", response_model=NodeNameView)
    async def get_name(c: _Session) -> NodeNameView:
        return await asyncio.to_thread(_name_view, c)

    @app.put(f"{API_PREFIX}/node-name", response_model=NodeNameView)
    async def put_name(body: NodeNameRequest, c: _Session) -> NodeNameView:
        try:
            await asyncio.to_thread(write_node_name, c.settings, body.name)
        except ValueError as exc:
            raise ConsoleHTTPError(422, "invalid_name", str(exc)) from None
        except OSError as exc:
            raise ConsoleHTTPError(
                500, "storage", f"Could not save the name: {exc.strerror or exc}"
            ) from None
        return await asyncio.to_thread(_name_view, c)

    # ------------------------------------------------------------ checks
    @app.get(f"{API_PREFIX}/checks", response_model=ChecksView)
    async def checks(c: _Session) -> ChecksView:
        found = await run_checks(c.settings, peer_endpoint=_peer_endpoint(c.supervisor))
        return ChecksView(
            checks=[CheckView(**check.as_dict()) for check in found]  # type: ignore[arg-type]
        )

    @app.get(f"{API_PREFIX}/docker-access", response_model=DockerAccessView)
    async def docker_access(c: _Session) -> DockerAccessView:
        system = install_mode() is InstallMode.SYSTEM
        return DockerAccessView(
            supported=system,
            menu_action="MeshForge: Give Docker access",
            command=DOCKER_ACCESS_COMMAND,
            explanation=(
                "Docker access lets MeshForge run jobs in containers. It is the "
                "same as full admin rights on this laptop, so only you can turn it "
                "on: use the app-menu entry (it asks for your password) or run the "
                "command in a terminal. MeshForge restarts by itself afterwards; "
                "open it again from the app menu."
            ),
        )

    # -------------------------------------------------------------- logs
    @app.get(f"{API_PREFIX}/logs", response_model=LogsView)
    async def logs(
        c: _Session,
        limit: Annotated[int, Query(ge=1, le=5000)] = 300,
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> LogsView:
        if c.logs is None:
            return LogsView(lines=[], capacity=0)
        lines = c.logs.tail(limit, after=after)
        return LogsView(
            lines=[LogLineView(**line.as_dict()) for line in lines],
            capacity=c.settings.agent.console.log_lines,
        )

    # ------------------------------------------------------ restart/leave
    @app.post(f"{API_PREFIX}/restart", response_model=OkView)
    async def restart(c: _Session) -> OkView:
        c.supervisor.request("restart")
        return OkView(message="MeshForge is restarting its connection to the fabric.")

    @app.post(f"{API_PREFIX}/leave", response_model=OkView)
    async def leave(_body: LeaveRequest, c: _Session) -> OkView:
        joined = await asyncio.to_thread(_enrolled, c.settings)
        if not joined and c.supervisor.phase is FabricPhase.WAITING_FOR_JOIN:
            raise ConsoleHTTPError(409, "not_joined", "This laptop is not in a fabric.")
        await asyncio.to_thread(clear_setup, c.settings)
        c.supervisor.request("leave")
        log.info("agent.console.leave_requested")
        return OkView(
            message="This laptop is leaving the fabric. It stops taking jobs now."
        )

    # ------------------------------------------------------- unknown API
    @app.api_route(
        API_PREFIX + "/{rest:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        include_in_schema=False,
    )
    async def unknown_api(rest: str) -> Response:
        return _error(404, "not_found", "No such console API.")

    # ---------------------------------------------------------------- UI
    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def ui(path: str) -> Response:
        return _serve_ui(ctx.dist, path)

    return app


def _serve_ui(dist: Path, path: str) -> Response:
    """A file of ``dist/`` that matches exactly (inside it, no hidden parts,
    no symlink out), else ``index.html`` for client routes; a missing *file*
    (dotted last segment) is a 404, never HTML served as script."""
    index = dist / "index.html"
    last = path.rsplit("/", 1)[-1]
    if not index.is_file():
        if "." in last:
            return _error(404, "not_found", "No such file.")
        return Response(
            NOT_BUILT_HTML,
            media_type="text/html; charset=utf-8",
            headers={"cache-control": "no-cache"},
        )
    parts = [p for p in path.split("/") if p]
    if parts and not any(p.startswith(".") or p == ".." for p in parts):
        candidate = dist.joinpath(*parts)
        try:
            resolved = candidate.resolve(strict=True)
            inside = resolved.is_relative_to(dist.resolve()) and resolved.is_file()
        except (OSError, ValueError):
            inside = False
        if inside:
            media = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
            cache = _IMMUTABLE if parts[0] == "assets" else "no-cache"
            return FileResponse(
                resolved, media_type=media, headers={"cache-control": cache}
            )
    if "." in last:
        return _error(404, "not_found", "No such file.")
    return FileResponse(
        index,
        media_type="text/html; charset=utf-8",
        headers={"cache-control": "no-cache"},
    )


def openapi_document() -> dict[str, Any]:
    """The console API's OpenAPI document - no server, no supervisor: the
    routes are built over a context no handler ever runs with."""
    from fastapi.openapi.utils import get_openapi

    ctx = ConsoleContext(
        supervisor=cast(Supervisor, None),
        auth=ConsoleAuth(port=1, session_ttl_s=60.0, launch_code_ttl_s=60.0),
    )
    app = create_app(ctx)
    return get_openapi(title=app.title, version=app.version, routes=app.routes)
