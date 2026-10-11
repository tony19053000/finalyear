"""V0.7 G5 item 0: login names are ASCII (D158 T-5) and the login backoff is
keyed by (name, source bucket) (D159 N1). No database, no socket."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")

from mf_users import ALICE
from pydantic import SecretStr

from meshforge.common.api_token import new_api_token
from meshforge.controller.auth.login import (
    LoginPolicy,
    LoginRefusedError,
    LoginService,
    login_key,
)
from meshforge.controller.auth.passwords import PasswordHasher, hash_password
from meshforge.controller.auth.users import (
    LoginRecord,
    TokenRecord,
    UserError,
    check_user_name,
)
from meshforge.controller.store.models.auth import UserRole

pytestmark = pytest.mark.unit

PASSWORD = SecretStr("correct horse battery")
KELVIN = chr(0x212A)
FULLWIDTH = "".join(chr(c) for c in (0xFF4B, 0xFF45, 0xFF56, 0xFF49, 0xFF4E))


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class _FakeStore:
    users: dict[str, LoginRecord] = field(default_factory=dict)
    lookups: int = 0
    failures: list[str] = field(default_factory=list)
    issued: int = 0

    async def login_record(self, name: str) -> LoginRecord | None:
        self.lookups += 1
        return self.users.get(name)

    async def record_login_failure(
        self, *, user_id: str | None, reason: str, source: str | None
    ) -> None:
        self.failures.append(reason)

    async def issue_token(
        self, user: LoginRecord, *, ttl_s: int, token_name: str, source: str | None
    ) -> tuple[SecretStr, TokenRecord]:
        self.issued += 1
        now = dt.datetime.now(dt.UTC)
        return new_api_token(), TokenRecord(
            id="tok_01J0T0KEN00000000000000000",
            user_id=user.user_id,
            user_name=user.name,
            name=token_name,
            created_at=now,
            expires_at=now + dt.timedelta(seconds=ttl_s),
            last_used_at=None,
            revoked_at=None,
            now=now,
        )


@pytest.fixture(scope="module")
def stored() -> str:
    return hash_password(PASSWORD)


def _service(stored: str, **kw: object) -> tuple[LoginService, _FakeStore, _Clock]:
    store = _FakeStore()
    store.users["kevin"] = LoginRecord(
        user_id=str(ALICE.user_id),
        name="kevin",
        role=UserRole.USER,
        pw_hash=stored,
        disabled=False,
    )
    clock = _Clock()
    policy = LoginPolicy(
        rate_per_minute=600.0,
        rate_burst=100,
        free_failures=2,
        backoff_base_s=30.0,
        backoff_max_s=60.0,
        **kw,  # type: ignore[arg-type]
    )
    service = LoginService(
        store,  # type: ignore[arg-type]
        policy=policy,
        hasher=PasswordHasher(),
        clock=clock,
    )
    return service, store, clock


async def _login(
    service: LoginService, name: str, password: SecretStr, host: str = "10.0.0.5"
) -> None:
    await service.login(
        name, password, host=host, source=f"{host}:5000", token_name="t"
    )


# ------------------------------------------------------------------ T-5 names


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("kevin", "kevin"),
        ("  KEVIN ", "kevin"),
        ("Kevin", "kevin"),
    ],
)
def test_ascii_names_are_case_folded(name: str, expected: str) -> None:
    assert login_key(name) == (expected, expected)


@pytest.mark.parametrize(
    "name",
    [
        KELVIN + "evin",  # lower() and NFKC both map U+212A to k/K
        chr(0x130) + "kevin",  # dotted capital I
        "kev\u200bin",  # zero width space
        FULLWIDTH,  # fullwidth "kevin" (NFKC -> kevin)
        "kevin" + chr(0xFEFF),
        "\U0001f600",
    ],
)
def test_a_non_ascii_spelling_is_never_a_user_name(name: str) -> None:
    throttle, user = login_key(name)
    assert user is None
    assert throttle.startswith("\x00")  # its own counter, never a real name's


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [KELVIN + "evin", FULLWIDTH])
async def test_a_look_alike_is_the_unknown_user_answer_without_a_lookup(
    stored: str, name: str
) -> None:
    service, store, _ = _service(stored)
    with pytest.raises(LoginRefusedError) as caught:
        await _login(service, name, PASSWORD)
    assert caught.value.reason == "bad_credentials"
    assert store.lookups == 0
    assert store.failures == ["unknown_user"]
    assert store.issued == 0


@pytest.mark.parametrize(
    "name", [KELVIN + "evin", "k" + chr(0xE9) + "vin", chr(0xFF4B) + "evin"]
)
def test_user_creation_refuses_non_ascii_names(name: str) -> None:
    with pytest.raises(UserError):
        check_user_name(name)


# ------------------------------------------------------------------ N1 backoff key


@pytest.mark.asyncio
async def test_a_stranger_elsewhere_cannot_lock_out_a_known_user(stored: str) -> None:
    service, store, _ = _service(stored)
    wrong = SecretStr("wrong password")
    for _ in range(5):
        with pytest.raises(LoginRefusedError):
            await _login(service, "kevin", wrong, host="192.0.2.66")
    with pytest.raises(LoginRefusedError) as caught:
        await _login(service, "kevin", PASSWORD, host="192.0.2.66")
    assert caught.value.reason == "rate_limited_user"  # the stranger is blocked
    # The real user, from their own address, is not.
    await _login(service, "kevin", PASSWORD, host="10.0.0.5")
    assert store.issued == 1


@pytest.mark.asyncio
async def test_the_backoff_is_shared_inside_one_ipv6_slash_64(stored: str) -> None:
    service, _, _ = _service(stored)
    wrong = SecretStr("wrong password")
    for i in range(3):
        with pytest.raises(LoginRefusedError):
            await _login(service, "kevin", wrong, host=f"2001:db8:1:2::{i + 1}")
    with pytest.raises(LoginRefusedError) as caught:
        # another address in the same /64 (privacy address): same block
        await _login(service, "kevin", PASSWORD, host="2001:db8:1:2:ffff::9")
    assert caught.value.reason == "rate_limited_user"
    await _login(service, "kevin", PASSWORD, host="2001:db8:1:3::1")


@pytest.mark.asyncio
async def test_spellings_of_one_ascii_name_share_one_counter_per_source(
    stored: str,
) -> None:
    service, _, _ = _service(stored)
    wrong = SecretStr("wrong password")
    for spelling in ("kevin", "KEVIN", " Kevin "):
        with pytest.raises(LoginRefusedError):
            await _login(service, spelling, wrong)
    with pytest.raises(LoginRefusedError) as caught:
        await _login(service, "kEvIn", PASSWORD)
    assert caught.value.reason == "rate_limited_user"
