"""The setup steps (V0.17 G2, D392): sessions, facts for the page, "create fabric".

:meth:`SetupService.complete` does, under one lock and in this order:

1. validate the admin name and password (nothing written on a refusal);
2. re-check the state is still ``pending`` (a second browser tab or the CLI
   cannot create a second fabric);
3. create the fabric CA and the server certificate (same code as
   ``meshforge-controller fabric create``);
4. create the admin user - if that fails (database down), the CA made in step 3
   is removed again, so the page can simply be retried;
5. make the first invite (same code as ``fabric invite``); a failure here does
   not undo the fabric - the admin makes one from the dashboard instead (the
   result says so);
6. delete the setup token, forget every setup session, and ask the controller to
   restart into its normal mode (after the response went out).

Sessions are random ids in memory (the cookie value); they die with the process.
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

from pydantic import SecretStr

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.controller.setup.policy import check_admin_password
from meshforge.controller.setup.state import SetupState, setup_state
from meshforge.controller.setup.token import (
    read_setup_token,
    remove_setup_token,
    setup_token_matches,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from meshforge.controller.settings import ControllerSettings

__all__ = ["SESSION_BYTES", "SetupError", "SetupResult", "SetupService"]

log = get_logger(__name__)

SESSION_BYTES: Final = 32
_MAX_SESSIONS: Final = 16
SETUP_ACTOR: Final = "setup-page"


class SetupError(MeshForgeError):
    """A setup step refused or failed; ``failure_type`` maps to the HTTP status."""

    default_failure_type = FailureType.INVALID_INPUT


@dataclass(frozen=True, slots=True)
class SetupResult:
    admin: str
    fabric_id: str
    ca_fingerprint: str
    join_fingerprint: str
    invite: dict[str, object] | None
    invite_problem: str | None
    dashboard_url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "admin": self.admin,
            "fabric_id": self.fabric_id,
            "ca_fingerprint": self.ca_fingerprint,
            "join_fingerprint": self.join_fingerprint,
            "invite": self.invite,
            "invite_problem": self.invite_problem,
            "dashboard_url": self.dashboard_url,
            "restarting": True,
        }


class SetupService:
    """See the module docstring. ``on_done`` is called once, after a
    successful :meth:`complete` (the runtime restarts itself)."""

    def __init__(
        self,
        settings: ControllerSettings,
        engine: AsyncEngine,
        *,
        on_done: Callable[[], None],
        checks: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        from meshforge.controller.auth.users import UserStore
        from meshforge.controller.enrollment.invites import InviteStore

        self.settings = settings
        self.users = UserStore(engine)
        self.invites = InviteStore(engine)
        self._on_done = on_done
        self._checks = checks
        # Insertion-ordered (dict keys): over the cap the *oldest* session is
        # evicted, so the newest browser tab keeps working.
        self._sessions: dict[str, None] = {}
        self._lock = asyncio.Lock()
        self._done = False

    # ----------------------------------------------------------------- state
    async def state(self) -> SetupState:
        from meshforge.controller.fabric_ca import fabric_present

        if self._done:
            return SetupState.DONE
        ca_present = await asyncio.to_thread(fabric_present, self.settings.ca_dir)
        users_exist = bool(await self.users.list_users())
        token = await asyncio.to_thread(
            read_setup_token, self.settings.setup_token_path
        )
        return setup_state(
            ca_present=ca_present,
            users_exist=users_exist,
            token_present=token is not None,
        )

    async def require_pending(self) -> None:
        if await self.state() is not SetupState.PENDING:
            raise SetupError(
                "this controller is already set up",
                failure_type=FailureType.CONFLICT,
                details={"reason": "gone"},
            )

    # -------------------------------------------------------------- sessions
    async def open_session(self, presented: str) -> str:
        """A new session id for a correct token (``unauthorized`` otherwise)."""
        await self.require_pending()
        stored = await asyncio.to_thread(
            read_setup_token, self.settings.setup_token_path
        )
        if not setup_token_matches(stored, presented):
            log.warning("controller.setup.token_refused")
            raise SetupError(
                "this setup link is not valid: run `sudo meshforge-controller setup "
                "url` on the controller for the current one",
                failure_type=FailureType.UNAUTHORIZED,
            )
        session = secrets.token_urlsafe(SESSION_BYTES)
        while len(self._sessions) >= _MAX_SESSIONS:
            del self._sessions[next(iter(self._sessions))]
        self._sessions[session] = None
        log.info("controller.setup.session_opened")
        return session

    def session_valid(self, session: str | None) -> bool:
        if not session:
            return False
        return any(
            secrets.compare_digest(session.encode(), known.encode())
            for known in self._sessions
        )

    # ------------------------------------------------------------------ facts
    def info(self) -> dict[str, object]:
        """What the page shows before the form (blocking: the doctor checks)."""
        from meshforge.common.netaddr import parse_bind
        from meshforge.controller.enrollment.codes import CodeAddressError, code_address

        address: str | None
        address_problem: str | None = None
        try:
            host, port = code_address(self.settings)
            address = f"{host}:{port}"
        except CodeAddressError as exc:
            address, address_problem = None, exc.message
        http = parse_bind(self.settings.controller.http_bind, allow_any=True)
        loopback_only = http.host in {"127.0.0.1", "::1", "localhost"}
        return {
            "state": SetupState.PENDING.value,
            "hostname": socket.gethostname(),
            "enroll_address": address,
            "address_problem": address_problem,
            "dashboard_url": _dashboard_url(self.settings),
            "loopback_only": loopback_only,
            "mdns": self.settings.controller.discovery.advertise,
            "config_file": "/etc/meshforge/controller.toml",
            "checks": self._checks() if self._checks is not None else None,
        }

    # --------------------------------------------------------------- complete
    async def complete(self, name: str, password: SecretStr) -> SetupResult:
        from meshforge.controller.auth.passwords import PasswordError, PasswordHasher
        from meshforge.controller.auth.users import (
            UserError,
            check_user_name,
        )

        try:
            checked = check_user_name(name)
            check_admin_password(checked, password)
        except (UserError, PasswordError) as exc:
            raise SetupError(
                exc.message, failure_type=FailureType.INVALID_INPUT
            ) from None
        async with self._lock:
            await self.require_pending()
            pw_hash = await PasswordHasher(max_concurrent=1).hash(password)
            fabric = await asyncio.to_thread(self._create_fabric)
            try:
                await self._create_admin(checked, pw_hash)
            except BaseException:
                await asyncio.to_thread(self._remove_fabric)
                raise
            invite, problem = await self._first_invite(fabric)
            await asyncio.to_thread(remove_setup_token, self.settings.setup_token_path)
            self._done = True
            self._sessions.clear()
        log.warning(
            "controller.setup.completed",
            fabric_id=fabric.fabric_id,
            ca_fingerprint=fabric.short_fingerprint,
            invite_made=invite is not None,
        )
        asyncio.get_running_loop().call_later(0.5, self._on_done)
        return SetupResult(
            admin=checked,
            fabric_id=str(fabric.fabric_id),
            ca_fingerprint=fabric.fingerprint,
            join_fingerprint=fabric.short_fingerprint,
            invite=invite,
            invite_problem=problem,
            dashboard_url=_dashboard_url(self.settings),
        )

    def _create_fabric(self) -> Any:
        from meshforge.controller.fabric_ca import (
            FabricCaError,
            create_fabric,
            ensure_server_cert,
        )

        settings = self.settings
        try:
            fabric = create_fabric(settings.ca_dir)
            ensure_server_cert(
                settings.tls_dir,
                fabric,
                hosts=settings.server_cert_hosts,
                lifetime=timedelta(days=settings.controller.tls.server_cert_days),
            )
        except FabricCaError as exc:
            self._remove_fabric()
            raise SetupError(
                f"the fabric CA could not be created: {exc.message}",
                failure_type=FailureType.CONFIG_ERROR,
            ) from None
        return fabric

    def _remove_fabric(self) -> None:
        for folder in (self.settings.ca_dir, self.settings.tls_dir):
            shutil.rmtree(folder, ignore_errors=True)
        log.warning("controller.setup.fabric_rolled_back")

    async def _create_admin(self, name: str, pw_hash: str) -> None:
        from meshforge.controller.store.models.auth import UserRole

        try:
            await self.users.create_user(
                name, pw_hash=pw_hash, role=UserRole.ADMIN, actor=SETUP_ACTOR
            )
        except MeshForgeError as exc:
            raise SetupError(
                f"the admin user could not be created ({exc.failure_type.value}); "
                "nothing was changed - try again",
                failure_type=FailureType.NETWORK_ERROR,
            ) from None

    async def _first_invite(
        self, fabric: Any
    ) -> tuple[dict[str, object] | None, str | None]:
        from meshforge.controller.enrollment.codes import code_address, issue_code

        try:
            host, port = code_address(self.settings)
            new = await self.invites.create(
                ttl_s=self.settings.controller.enroll.invite_ttl_s, actor=SETUP_ACTOR
            )
        except MeshForgeError as exc:
            log.warning(
                "controller.setup.invite_failed",
                failure_type=exc.failure_type.value,
                error=exc.message,
            )
            return None, (
                f"{exc.message} - make an invite on the dashboard's Invites page "
                "once you signed in"
            )
        return issue_code(new, fabric=fabric, host=host, port=port).as_dict(), None


def _dashboard_url(settings: ControllerSettings) -> str:
    from meshforge.common.netaddr import parse_bind

    advertised = settings.controller.api.advertise_url
    if advertised:
        return advertised
    bind = parse_bind(settings.controller.http_bind, allow_any=True)
    host = f"[{bind.host}]" if ":" in bind.host else bind.host
    scheme = "http" if settings.security.insecure else "https"
    return f"{scheme}://{host}:{bind.port}/"
