"""V0.7 G4 (D141) units: password hashing, API token shape, login backoff, the
owner rule, and the scrubber for the new secrets. No database, no socket."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")

from mf_users import ADMIN, ALICE, BOB
from pydantic import SecretStr

from meshforge.common.api_token import (
    API_TOKEN_CHARS,
    api_token_hash,
    is_api_token,
    new_api_token,
)
from meshforge.common.logging import scrub_text
from meshforge.controller.auth import passwords
from meshforge.controller.auth.login import (
    LoginPolicy,
    LoginRefusedError,
    LoginResult,
    LoginService,
    LoginThrottle,
    login_policy,
)
from meshforge.controller.auth.passwords import (
    PasswordError,
    PasswordHasher,
    check_password_policy,
    hash_password,
    verify_password,
)
from meshforge.controller.auth.principal import LEGACY_PRINCIPAL
from meshforge.controller.auth.users import (
    LoginRecord,
    TokenRecord,
    UserError,
    check_user_name,
)
from meshforge.controller.settings import AuthSettings
from meshforge.controller.store.models.auth import UserRole

pytestmark = pytest.mark.unit

PASSWORD = SecretStr("correct horse battery")


# ------------------------------------------------------------------ passwords


@pytest.fixture(scope="module")
def stored() -> str:
    return hash_password(PASSWORD)


def test_hash_has_the_d141_parameters_and_verifies(stored: str) -> None:
    assert stored.startswith("scrypt$32768$8$1$")
    assert 16 <= len(stored) <= 255  # the app_user.pw_hash CHECK
    assert verify_password(PASSWORD, stored)
    assert not verify_password(SecretStr("correct horse battery!"), stored)
    assert PASSWORD.get_secret_value() not in stored


def test_two_hashes_of_one_password_differ_by_salt(stored: str) -> None:
    assert hash_password(PASSWORD) != stored


def _swap(stored: str, index: int, value: str) -> str:
    parts = stored.split("$")
    parts[index] = value
    return "$".join(parts)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda s: "",
        lambda s: "bcrypt" + s[6:],
        lambda s: s + "$extra",
        lambda s: _swap(s, 1, "32769"),  # n not a power of two
        lambda s: _swap(s, 1, str(2**21)),  # n above the bound (memory DoS)
        lambda s: _swap(s, 1, "1"),
        lambda s: _swap(s, 2, "17"),  # r above the bound
        lambda s: _swap(s, 3, "5"),  # p above the bound
        lambda s: _swap(s, 3, "x"),
        lambda s: _swap(s, 4, "!!notbase64"),
        lambda s: _swap(s, 4, base64.b64encode(b"short").decode()),
        lambda s: _swap(s, 5, base64.b64encode(b"k" * 31).decode()),
    ],
)
def test_a_malformed_or_out_of_range_stored_hash_never_verifies(
    stored: str, mangle: object
) -> None:
    assert callable(mangle)
    assert not verify_password(PASSWORD, mangle(stored))


def test_password_policy_bounds() -> None:
    check_password_policy(SecretStr("x" * 8))
    check_password_policy(SecretStr("x" * passwords.MAX_PASSWORD_CHARS))
    for bad in ("x" * 7, "x" * (passwords.MAX_PASSWORD_CHARS + 1), "abcdefg\x00h"):
        with pytest.raises(PasswordError) as caught:
            check_password_policy(SecretStr(bad))
        assert bad not in str(caught.value)


def test_an_over_long_presented_password_is_refused_without_hashing(
    stored: str,
) -> None:
    assert not verify_password(
        SecretStr("x" * (passwords.MAX_PASSWORD_CHARS + 1)), stored
    )


@pytest.mark.asyncio
async def test_hasher_runs_at_most_max_concurrent_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = 0
    peak = 0

    def slow_verify(password: SecretStr, stored: str) -> bool:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        import time

        time.sleep(0.02)
        running -= 1
        return False

    monkeypatch.setattr(passwords, "verify_password", slow_verify)
    hasher = PasswordHasher(max_concurrent=2)
    await asyncio.gather(*(hasher.verify(PASSWORD, "x") for _ in range(6)))
    assert peak == 2


@pytest.mark.asyncio
async def test_unknown_user_still_costs_one_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real = passwords.verify_password

    def counting(password: SecretStr, stored: str) -> bool:
        calls.append(stored)
        return real(password, stored)

    monkeypatch.setattr(passwords, "verify_password", counting)
    hasher = PasswordHasher()
    assert await hasher.verify_or_dummy(PASSWORD, None) is False
    assert len(calls) == 1
    assert calls[0].startswith("scrypt$32768$8$1$")


@pytest.mark.asyncio
async def test_hasher_refuses_a_weak_password_before_hashing() -> None:
    with pytest.raises(PasswordError):
        await PasswordHasher().hash(SecretStr("short"))


# ------------------------------------------------------------------ API tokens


def test_api_token_shape_and_hash() -> None:
    token = new_api_token().get_secret_value()
    assert token.startswith("mfat_")
    assert len(token) == API_TOKEN_CHARS == 48
    assert is_api_token(token)
    assert new_api_token().get_secret_value() != token
    digest = api_token_hash(token)
    assert len(digest) == 64
    assert digest == api_token_hash(token)
    assert token not in digest


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "mfat_",
        "mfat_" + "a" * 42,
        "mfat_" + "a" * 44,
        "mfAt_" + "a" * 43,
        "mfat_" + "a" * 42 + "!",
        "x" * 48,
    ],
)
def test_not_an_api_token(text: str | None) -> None:
    assert not is_api_token(text)


def test_the_scrubber_masks_an_api_token_anywhere_in_text() -> None:
    """Checklist 20 - fails if ``_API_TOKEN_RE`` is removed from the scrubber."""
    token = new_api_token().get_secret_value()
    for text in (
        f"using {token} now",
        f"({token})",
        f"url=https://x/?q={token}",
        token,
    ):
        assert token[5:] not in scrub_text(text)
    assert "mfat_[REDACTED]" in scrub_text(f"using {token} now")


def test_password_and_token_fields_are_redacted_in_log_records() -> None:
    token = new_api_token()
    from meshforge.common.logging import redact_processor

    event = redact_processor(
        None,
        "info",
        {
            "event": f"login with {token.get_secret_value()}",
            "password": "hunter2hunter2",
            "token": token,
            "body": {"name": "a", "password": "hunter2hunter2"},
        },
    )
    rendered = repr(event)
    assert "hunter2hunter2" not in rendered
    assert token.get_secret_value() not in rendered


# ------------------------------------------------------------------ names


@pytest.mark.parametrize("name", ["a", "alice", "a.b-c_d", "x" * 64, "0day"])
def test_good_user_names(name: str) -> None:
    assert check_user_name(name) == name


@pytest.mark.parametrize(
    "name", ["", "Alice", "-x", ".x", "x" * 65, "a b", "a/b", "ä", "a\x00"]
)
def test_bad_user_names(name: str) -> None:
    with pytest.raises(UserError):
        check_user_name(name)


# ------------------------------------------------------------------ owner rule


def test_owner_rule() -> None:
    assert ALICE.may_see_job(ALICE.user_id)
    assert not ALICE.may_see_job(BOB.user_id)
    assert not ALICE.may_see_job(None)  # legacy / pre-0018 jobs: admin only
    assert ADMIN.may_see_job(ALICE.user_id)
    assert ADMIN.may_see_job(None)
    assert LEGACY_PRINCIPAL.is_admin
    assert LEGACY_PRINCIPAL.may_see_job(BOB.user_id)
    assert LEGACY_PRINCIPAL.actor == "rest:fabric-token"
    assert ALICE.actor == f"user:{ALICE.user_id}"


# ------------------------------------------------------------------ throttle


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _policy(**kw: object) -> LoginPolicy:
    base: dict[str, object] = {
        "rate_per_minute": 600.0,
        "rate_burst": 100,
        "free_failures": 3,
        "backoff_base_s": 1.0,
        "backoff_max_s": 8.0,
    }
    base.update(kw)
    return LoginPolicy(**base)  # type: ignore[arg-type]


def test_backoff_starts_after_the_free_failures_doubles_and_caps() -> None:
    clock = _Clock()
    throttle = LoginThrottle(_policy(), clock=clock)
    for _ in range(3):
        throttle.failed("alice")
        assert throttle.blocked_for("alice") == 0.0
    delays = []
    for _ in range(6):
        throttle.failed("alice")
        delays.append(throttle.blocked_for("alice"))
    assert delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]
    assert throttle.blocked_for("bob") == 0.0  # per name
    clock.now += 8.0
    assert throttle.blocked_for("alice") == 0.0
    throttle.succeeded("alice")
    throttle.failed("alice")
    assert throttle.blocked_for("alice") == 0.0  # a success resets the count


def test_the_name_table_is_bounded() -> None:
    throttle = LoginThrottle(_policy(max_tracked=16), clock=_Clock())
    for i in range(100):
        throttle.failed(f"user{i}")
    assert len(throttle._names) == 16


def test_source_bucket_shares_an_ipv6_slash_64_and_one_unknown_bucket() -> None:
    throttle = LoginThrottle(_policy(rate_per_minute=1.0, rate_burst=2), clock=_Clock())
    assert throttle.allow_source("2001:db8:1:2::1")
    assert throttle.allow_source("2001:db8:1:2:ffff::9")  # same /64
    assert not throttle.allow_source("2001:db8:1:2::77")
    assert throttle.allow_source("2001:db8:1:3::1")  # another /64
    assert throttle.allow_source("10.0.0.1")
    assert throttle.allow_source(None)
    assert throttle.allow_source("not an ip")  # same 'unknown' bucket ...
    assert not throttle.allow_source("")  # ... which is now empty


def test_login_policy_from_settings() -> None:
    policy = login_policy(AuthSettings(token_ttl_days=7, login_free_failures=2))
    assert policy.token_ttl_s == 7 * 86_400
    assert policy.free_failures == 2
    assert AuthSettings().legacy_fabric_token is False  # D141 default
    assert AuthSettings().token_ttl_days == 90


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
        import datetime as dt

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


def _service(
    store: _FakeStore, stored: str, **policy: object
) -> tuple[LoginService, _Clock]:
    clock = _Clock()
    store.users["alice"] = LoginRecord(
        user_id=str(ALICE.user_id),
        name="alice",
        role=UserRole.USER,
        pw_hash=stored,
        disabled=False,
    )
    store.users["dora"] = LoginRecord(
        user_id="usr_01J0D0RA00000000000000000",
        name="dora",
        role=UserRole.USER,
        pw_hash=stored,
        disabled=True,
    )
    service = LoginService(
        store,  # type: ignore[arg-type]
        policy=_policy(**policy),
        hasher=PasswordHasher(),
        clock=clock,
    )
    return service, clock


async def _login(service: LoginService, name: str, password: SecretStr) -> LoginResult:
    return await service.login(
        name, password, host="10.0.0.5", source="10.0.0.5:5000", token_name="t"
    )


@pytest.mark.asyncio
async def test_login_success_wrong_password_unknown_and_disabled(stored: str) -> None:
    store = _FakeStore()
    service, _ = _service(store, stored)
    ok = await _login(service, "Alice", PASSWORD)  # name is case-folded
    assert ok.principal.user_id == ALICE.user_id
    messages = set()
    for name, password in (
        ("alice", SecretStr("wrong password")),
        ("nobody", PASSWORD),
        ("dora", PASSWORD),  # right password, disabled user
    ):
        with pytest.raises(LoginRefusedError) as caught:
            await _login(service, name, password)
        assert caught.value.reason == "bad_credentials"
        messages.add(caught.value.message)
    assert len(messages) == 1  # one answer for all three
    assert store.failures == ["bad_password", "unknown_user", "disabled"]
    assert store.issued == 1


@pytest.mark.asyncio
async def test_a_blocked_user_is_refused_before_any_lookup(stored: str) -> None:
    store = _FakeStore()
    service, clock = _service(store, stored, free_failures=1)
    with pytest.raises(LoginRefusedError):
        await _login(service, "alice", SecretStr("wrong password"))
    with pytest.raises(LoginRefusedError):
        await _login(service, "alice", SecretStr("wrong password"))
    lookups = store.lookups
    with pytest.raises(LoginRefusedError) as caught:
        await _login(service, "alice", PASSWORD)  # even the right password
    assert caught.value.reason == "rate_limited_user"
    assert caught.value.retry_after_s is not None and caught.value.retry_after_s > 0
    assert store.lookups == lookups
    clock.now += 10
    await _login(service, "alice", PASSWORD)


@pytest.mark.asyncio
async def test_an_exhausted_source_is_refused_before_any_lookup(stored: str) -> None:
    store = _FakeStore()
    service, _ = _service(store, stored, rate_per_minute=1.0, rate_burst=1)
    await _login(service, "alice", PASSWORD)
    with pytest.raises(LoginRefusedError) as caught:
        await _login(service, "alice", PASSWORD)
    assert caught.value.reason == "rate_limited_source"
    assert store.lookups == 1
