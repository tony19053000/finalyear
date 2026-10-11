"""V0.7 G4 (D141): :class:`UserStore` on real PostgreSQL (migrated to head).

Pinned, one test per authentication branch (checklist 19): a live token
authenticates; an unknown well-formed token, an expired one, a revoked one and
one of a disabled user do not; ``last_used_at`` is written at most once a
minute; a disabled user / a password reset revoke every live token in the same
transaction; tokens are stored hashed; audit rows carry no secret; duplicate
names are a typed conflict; another user's token cannot be revoked (404).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.api_token import api_token_hash, new_api_token
from meshforge.controller.auth.passwords import hash_password
from meshforge.controller.auth.users import (
    LoginRecord,
    UserConflictError,
    UserNotFoundError,
    UserStore,
)
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import upgrade_to_head
from meshforge.controller.store.models.auth import UserRole
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

PW_HASH = hash_password(SecretStr("a-good-password-1"))


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(upgrade_to_head, pg_settings)
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def store(engine: AsyncEngine) -> UserStore:
    return UserStore(engine)


async def _user(
    store: UserStore, name: str, role: UserRole = UserRole.USER
) -> LoginRecord:
    await store.create_user(name, pw_hash=PW_HASH, role=role, actor="test")
    found = await store.login_record(name)
    assert found is not None
    return found


async def test_a_live_token_authenticates_and_nothing_else_does(
    store: UserStore, engine: AsyncEngine
) -> None:
    alice = await _user(store, "alice")
    token, record = await store.issue_token(alice, ttl_s=3600, source="10.0.0.1:5")
    principal = await store.authenticate(token.get_secret_value())
    assert principal is not None
    assert (principal.user_id, principal.name, principal.role) == (
        alice.user_id,
        "alice",
        UserRole.USER,
    )
    assert principal.token_id == record.id
    # Unknown (well-formed) and malformed tokens.
    assert await store.authenticate(new_api_token().get_secret_value()) is None
    assert await store.authenticate("mfat_short") is None
    assert await store.authenticate(token.get_secret_value() + "x") is None
    # Stored hashed, never in clear.
    async with engine.connect() as db:
        rows = (await db.execute(text("SELECT token_hash FROM api_token"))).all()
        audit = (await db.execute(text("SELECT details::text FROM audit_event"))).all()
    assert [r[0] for r in rows] == [api_token_hash(token.get_secret_value())]
    dumped = json.dumps([r[0] for r in audit])
    assert token.get_secret_value() not in dumped
    assert token.get_secret_value()[5:] not in dumped


async def test_expired_revoked_and_disabled_are_refused(
    store: UserStore, engine: AsyncEngine
) -> None:
    alice = await _user(store, "alice")
    expired, _ = await store.issue_token(alice, ttl_s=3600)
    async with engine.begin() as db:
        await db.execute(
            text(
                "UPDATE api_token SET created_at = now() - interval '2 hours', "
                "expires_at = now() - interval '1 second'"
            )
        )
    assert await store.authenticate(expired.get_secret_value()) is None

    revoked, record = await store.issue_token(alice, ttl_s=3600)
    assert await store.authenticate(revoked.get_secret_value()) is not None
    principal = await store.authenticate(revoked.get_secret_value())
    assert principal is not None
    _, already = await store.revoke_token(record.id, by=principal)
    assert already is False
    assert await store.authenticate(revoked.get_secret_value()) is None
    _, already = await store.revoke_token(record.id, by=principal)
    assert already is True  # idempotent

    live, _ = await store.issue_token(alice, ttl_s=3600)
    _, count = await store.set_disabled("alice", disabled=True, actor="test")
    assert count == 1  # only the live one (not the expired, not the revoked)
    assert await store.authenticate(live.get_secret_value()) is None
    await store.set_disabled("alice", disabled=False, actor="test")
    # Re-enabling does not bring the revoked token back.
    assert await store.authenticate(live.get_secret_value()) is None


async def test_a_password_reset_revokes_every_live_token(store: UserStore) -> None:
    alice = await _user(store, "alice")
    one, _ = await store.issue_token(alice, ttl_s=3600)
    two, _ = await store.issue_token(alice, ttl_s=3600)
    _, count = await store.reset_password("alice", pw_hash=PW_HASH, actor="test")
    assert count == 2
    assert await store.authenticate(one.get_secret_value()) is None
    assert await store.authenticate(two.get_secret_value()) is None


async def test_last_used_is_coarse(store: UserStore, engine: AsyncEngine) -> None:
    alice = await _user(store, "alice")
    token, _ = await store.issue_token(alice, ttl_s=3600)

    async def last_used() -> object:
        async with engine.connect() as db:
            return (
                await db.execute(text("SELECT last_used_at FROM api_token"))
            ).scalar_one()

    assert await last_used() is None
    await store.authenticate(token.get_secret_value())
    first = await last_used()
    assert first is not None
    await store.authenticate(token.get_secret_value())
    assert await last_used() == first  # within the minute: no second write


async def test_users_tokens_and_their_owners(store: UserStore) -> None:
    alice = await _user(store, "alice")
    bob = await _user(store, "bob")
    admin = await _user(store, "root", UserRole.ADMIN)
    with pytest.raises(UserConflictError):
        await store.create_user("alice", pw_hash=PW_HASH, role=UserRole.USER, actor="t")
    a_token, a_record = await store.issue_token(alice, ttl_s=3600)
    b_token, _ = await store.issue_token(bob, ttl_s=3600)
    r_token, _ = await store.issue_token(admin, ttl_s=3600)
    bob_p = await store.authenticate(b_token.get_secret_value())
    admin_p = await store.authenticate(r_token.get_secret_value())
    assert bob_p is not None and admin_p is not None
    with pytest.raises(UserNotFoundError):
        await store.revoke_token(a_record.id, by=bob_p)
    assert await store.authenticate(a_token.get_secret_value()) is not None
    await store.revoke_token(a_record.id, by=admin_p)  # an admin may
    assert await store.authenticate(a_token.get_secret_value()) is None
    assert {t.user_name for t in await store.list_tokens(None)} == {
        "alice",
        "bob",
        "root",
    }
    assert {t.user_name for t in await store.list_tokens(bob.user_id)} == {"bob"}
    assert [u.name for u in await store.list_users()] == ["alice", "bob", "root"]
    with pytest.raises(UserNotFoundError):
        await store.set_disabled("nobody", disabled=True, actor="t")


async def test_legacy_use_is_audited_once_per_run(
    store: UserStore, engine: AsyncEngine
) -> None:
    for _ in range(3):
        await store.note_legacy_use("10.0.0.9:1234")
    assert store.legacy_uses == 3
    async with engine.connect() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT actor, source FROM audit_event "
                    "WHERE action = 'legacy_token_used'"
                )
            )
        ).all()
    assert [tuple(r) for r in rows] == [("rest:fabric-token", "10.0.0.9:1234")]
