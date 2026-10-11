"""Test users for REST tests (V0.7 G4, D141).

* :class:`StaticUsers` - an in-memory :class:`~meshforge.controller.api.auth.UserDirectory`
  for unit tests that mount the real routers without a database: fixed API
  tokens map to fixed principals.
* :func:`create_user` / :func:`login` - the real thing for component tests: a
  user written with the real :class:`~meshforge.controller.auth.users.UserStore`
  and a token from the real ``POST /v1/auth/login``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from pydantic import SecretStr

from meshforge.common.api_token import new_api_token
from meshforge.controller.api.auth import ApiAuth
from meshforge.controller.auth.principal import Principal
from meshforge.controller.store.models.auth import UserRole

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = [
    "ADMIN",
    "ADMIN_TOKEN",
    "ALICE",
    "ALICE_TOKEN",
    "BOB",
    "BOB_TOKEN",
    "StaticUsers",
    "bearer",
    "create_user",
    "login",
    "static_auth",
]

ADMIN: Final = Principal(
    user_id="usr_01J0ADM1N00000000000000000",
    name="admin",
    role=UserRole.ADMIN,
    token_id="tok_01J0ADM1N00000000000000000",
)
ALICE: Final = Principal(
    user_id="usr_01J0A11CE00000000000000000",
    name="alice",
    role=UserRole.USER,
    token_id="tok_01J0A11CE00000000000000000",
)
BOB: Final = Principal(
    user_id="usr_01J0B0B0000000000000000000",
    name="bob",
    role=UserRole.USER,
    token_id="tok_01J0B0B0000000000000000000",
)
ADMIN_TOKEN: Final = new_api_token().get_secret_value()
ALICE_TOKEN: Final = new_api_token().get_secret_value()
BOB_TOKEN: Final = new_api_token().get_secret_value()


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


class StaticUsers:
    """A fixed token -> principal table; records legacy uses and output owners."""

    def __init__(
        self,
        principals: Mapping[str, Principal] | None = None,
        *,
        outputs: Mapping[str, str] | None = None,
    ) -> None:
        self.principals = dict(
            principals
            if principals is not None
            else {ADMIN_TOKEN: ADMIN, ALICE_TOKEN: ALICE, BOB_TOKEN: BOB}
        )
        self.outputs = dict(outputs or {})
        """digest hex -> owner user id."""
        self.legacy_sources: list[str | None] = []
        self.fail: Exception | None = None

    async def authenticate(self, token: str) -> Principal | None:
        if self.fail is not None:
            raise self.fail
        return self.principals.get(token)

    async def note_legacy_use(self, source: str | None) -> None:
        self.legacy_sources.append(source)

    async def owns_output(self, user_id: str, digest_hex: str) -> bool:
        return self.outputs.get(digest_hex) == user_id


def static_auth(users: StaticUsers | None = None, **fields: object) -> ApiAuth:
    """An :class:`ApiAuth` over :class:`StaticUsers` (no fabric token)."""
    return ApiAuth(verifier=None, users=users or StaticUsers(), **fields)  # type: ignore[arg-type]


async def create_user(
    engine: AsyncEngine, name: str, password: str, *, admin: bool = False
) -> str:
    """A real user row (real scrypt hash); returns its id."""
    from meshforge.controller.auth.passwords import PasswordHasher
    from meshforge.controller.auth.users import UserStore

    pw_hash = await PasswordHasher(max_concurrent=1).hash(SecretStr(password))
    record = await UserStore(engine).create_user(
        name,
        pw_hash=pw_hash,
        role=UserRole.ADMIN if admin else UserRole.USER,
        actor="test",
    )
    return record.id


async def login(client: httpx.AsyncClient, name: str, password: str) -> str:
    """A token from the real ``POST /v1/auth/login`` (asserts 200)."""
    response = await client.post(
        "/v1/auth/login", json={"name": name, "password": password}
    )
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    assert isinstance(token, str)
    return token
