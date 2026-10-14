"""``POST /v1/auth/login``: name + password -> API token, with brute-force limits
(V0.7 G4, D141).

Two independent limits, both checked **before** any password work:

* **per source** - a token bucket per client IP, bucketed like enrolment (D152:
  the IPv4 address, the IPv6 /64, one shared ``unknown`` bucket;
  :func:`~meshforge.controller.enrollment.listener.ip_rate_key`), charged on
  every attempt;
* **per user name and source** - after ``free_failures`` consecutive failures
  the pair (name, source bucket - the IPv4 address or the IPv6 /64, as above) is
  blocked for ``backoff_base_s * 2**k`` seconds (``k`` = failures beyond the free
  ones, capped at ``backoff_max_s``); a success clears it. Applied to every
  name, existing or not, so the limit says nothing about which names exist.
  Keyed by the pair, not the name alone (G4 reviewer N1, D159): a stranger who
  knows a user's name can only lock that name out **from the stranger's own
  address**; the user logging in from their own laptop is never blocked by it.
  The price: a guesser with many source buckets gets ``free_failures`` per
  bucket (each bucket is also rate limited) - accepted in THREAT_MODEL RE-S2.

Either limit answers 429 with ``Retry-After`` and does not look at the password
(so a correct password during a block is refused too - the price of a backoff
that a guesser cannot probe through). A wrong name, a wrong password and a
disabled user are one answer (401, one message) after the same scrypt work.
Tokens already issued keep working during a block, so a guesser can delay a
user's *next* login, never cut their running session.

**Names are ASCII** (G4 tester r2 T-5, D158): the name as sent (stripped) must be
ASCII before it is lower-cased; anything else is an unknown user (same 401, no
query). Case-folding a non-ASCII spelling could alias a real user - KELVIN SIGN
U+212A lower-cases to ``k`` - and so would NFKC normalisation (it maps U+212A to
``K``), so the ASCII check is made on the raw text, before any folding.
"""

from __future__ import annotations

import collections
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from pydantic import SecretStr

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.controller.auth.passwords import MAX_PASSWORD_CHARS, PasswordHasher
from meshforge.controller.auth.principal import Principal
from meshforge.controller.auth.users import LoginRecord, TokenRecord, UserStore
from meshforge.controller.enrollment.listener import SourceRateLimiter, ip_rate_key
from meshforge.controller.settings import AuthSettings
from meshforge.controller.store.models.auth import MAX_USER_NAME_CHARS, TokenKind

__all__ = [
    "LoginPolicy",
    "LoginRefusedError",
    "LoginResult",
    "LoginService",
    "LoginThrottle",
    "login_key",
    "login_policy",
]

log = get_logger(__name__)

_BAD_CREDENTIALS: Final = "wrong user name or password"
_NOT_A_NAME: Final = "\x00"
"""Prefix of a throttle key for a name that is not ASCII: no valid name starts
with NUL, so such a spelling never shares (or dodges) a real name's counter."""


def login_key(name: str) -> tuple[str, str | None]:
    """``(throttle_name, user_name)`` of a login name as sent (D158 T-5).

    ``user_name`` is the stripped, lower-cased name when the stripped text is
    ASCII (and NFKC-stable, which ASCII always is), else ``None`` - an unknown
    user, never an alias of an ASCII name. ``throttle_name`` is the same text
    for an ASCII name and a NUL-prefixed key of its own otherwise."""
    raw = name.strip()
    if raw.isascii() and unicodedata.normalize("NFKC", raw) == raw:
        key = raw.lower()[:MAX_USER_NAME_CHARS]
        return key, key
    return _NOT_A_NAME + raw[:MAX_USER_NAME_CHARS], None


@dataclass(frozen=True, slots=True)
class LoginPolicy:
    """``[controller.auth]`` login knobs (see the settings for meanings)."""

    token_ttl_s: int = 90 * 86_400
    session_ttl_s: int = 7 * 86_400
    """D170: absolute life of a browser session."""
    rate_per_minute: float = 10.0
    rate_burst: int = 10
    free_failures: int = 5
    backoff_base_s: float = 1.0
    backoff_max_s: float = 300.0
    max_tracked: int = 4096


def login_policy(settings: AuthSettings) -> LoginPolicy:
    """The :class:`LoginPolicy` of ``[controller.auth]``."""
    return LoginPolicy(
        token_ttl_s=settings.token_ttl_days * 86_400,
        session_ttl_s=settings.session_max_days * 86_400,
        rate_per_minute=settings.login_rate_per_minute,
        rate_burst=settings.login_rate_burst,
        free_failures=settings.login_free_failures,
        backoff_base_s=settings.login_backoff_base_s,
        backoff_max_s=settings.login_backoff_max_s,
    )


class LoginRefusedError(MeshForgeError):
    """A refused login: ``reason`` is ``bad_credentials``, ``rate_limited_source``
    or ``rate_limited_user``; ``retry_after_s`` is set for the last two."""

    default_failure_type = FailureType.UNAUTHORIZED
    exit_code = ExitCode.FAILURE

    def __init__(
        self, message: str, *, reason: str, retry_after_s: float | None = None
    ) -> None:
        super().__init__(message, details={"reason": reason})
        self.reason = reason
        self.retry_after_s = retry_after_s


@dataclass(frozen=True, slots=True)
class LoginResult:
    token: SecretStr
    record: TokenRecord
    principal: Principal


class LoginThrottle:
    """Per-source token bucket + per-name exponential backoff. ``clock`` is
    monotonic; both tables are bounded (oldest forgotten first)."""

    def __init__(self, policy: LoginPolicy, *, clock: Callable[[], float]) -> None:
        self._policy = policy
        self._clock = clock
        self._sources = SourceRateLimiter(
            rate_per_minute=policy.rate_per_minute,
            burst=policy.rate_burst,
            max_sources=policy.max_tracked,
            clock=clock,
        )
        self._names: collections.OrderedDict[str, tuple[int, float]] = (
            collections.OrderedDict()
        )

    def allow_source(self, host: str | None) -> bool:
        """Charge one attempt to the client's bucket; False when it is empty."""
        return self._sources.allow(ip_rate_key(host))

    def source_retry_after_s(self) -> float:
        return 60.0 / self._policy.rate_per_minute

    def blocked_for(self, name: str) -> float:
        """Seconds until ``name`` may try again (0 = now)."""
        entry = self._names.get(name)
        if entry is None:
            return 0.0
        return max(0.0, entry[1] - self._clock())

    def failed(self, name: str) -> int:
        """Count a failure for ``name``; returns its consecutive failures."""
        failures = self._names.pop(name, (0, 0.0))[0] + 1
        blocked_until = 0.0
        extra = failures - self._policy.free_failures
        if extra > 0:
            delay = min(
                self._policy.backoff_max_s,
                self._policy.backoff_base_s * 2 ** min(extra - 1, 30),
            )
            blocked_until = self._clock() + delay
        self._names[name] = (failures, blocked_until)
        while len(self._names) > self._policy.max_tracked:
            self._names.popitem(last=False)
        return failures

    def succeeded(self, name: str) -> None:
        self._names.pop(name, None)


class LoginService:
    """Throttle -> user lookup -> scrypt verify -> token (+ audit)."""

    def __init__(
        self,
        store: UserStore,
        *,
        policy: LoginPolicy,
        hasher: PasswordHasher,
        clock: Callable[[], float],
    ) -> None:
        self.store = store
        self.policy = policy
        self._hasher = hasher
        self.throttle = LoginThrottle(policy, clock=clock)

    async def login(
        self,
        name: str,
        password: SecretStr,
        *,
        host: str | None,
        source: str | None,
        token_name: str,
    ) -> LoginResult:
        """``POST /v1/auth/login``: an API token (``compute login``)."""
        user = await self._verify(name, password, host=host, source=source)
        token, record = await self.store.issue_token(
            user, ttl_s=self.policy.token_ttl_s, token_name=token_name, source=source
        )
        return _result(user, token, record)

    async def open_session(
        self,
        name: str,
        password: SecretStr,
        *,
        host: str | None,
        source: str | None,
    ) -> LoginResult:
        """``POST /v1/session`` (V0.8 G1, D170): a browser session token, behind
        the **same** throttle, backoff and checks as :meth:`login` (D159) - a
        guesser gains nothing by switching between the two doors."""
        user = await self._verify(name, password, host=host, source=source)
        token, record = await self.store.issue_token(
            user,
            ttl_s=self.policy.session_ttl_s,
            token_name="dashboard",  # noqa: S106 - a label, not a secret
            source=source,
            kind=TokenKind.BROWSER_SESSION,
        )
        return _result(user, token, record)

    async def _verify(
        self,
        name: str,
        password: SecretStr,
        *,
        host: str | None,
        source: str | None,
    ) -> LoginRecord:
        """Throttle, look up, check the password; the user or
        :class:`LoginRefusedError`."""
        if not self.throttle.allow_source(host):
            log.warning("controller.auth.login_rate_limited", scope="source")
            raise LoginRefusedError(
                "too many login attempts from this address; wait a minute",
                reason="rate_limited_source",
                retry_after_s=self.throttle.source_retry_after_s(),
            )
        name_key, user_name = login_key(name)
        key = f"{name_key}|{ip_rate_key(host)}"  # N1: per (name, source bucket)
        wait = self.throttle.blocked_for(key)
        if wait > 0:
            log.warning("controller.auth.login_rate_limited", scope="user")
            raise LoginRefusedError(
                "too many failed logins for this user; wait and try again",
                reason="rate_limited_user",
                retry_after_s=wait,
            )
        user = None if user_name is None else await self.store.login_record(user_name)
        too_long = len(password.get_secret_value()) > MAX_PASSWORD_CHARS
        ok = await self._hasher.verify_or_dummy(
            password if not too_long else SecretStr(""),
            None if user is None else user.pw_hash,
        )
        reason = (
            "unknown_user"
            if user is None
            else "disabled"
            if user.disabled
            else "bad_password"
            if not ok or too_long
            else None
        )
        if reason is not None or user is None:
            failures = self.throttle.failed(key)
            await self.store.record_login_failure(
                user_id=None if user is None else user.user_id,
                reason=reason or "unknown_user",
                source=source,
            )
            log.warning(
                "controller.auth.login_failed",
                reason=reason,
                user_id=None if user is None else user.user_id,
                consecutive_failures=failures,
            )
            raise LoginRefusedError(_BAD_CREDENTIALS, reason="bad_credentials")
        self.throttle.succeeded(key)
        return user


def _result(user: LoginRecord, token: SecretStr, record: TokenRecord) -> LoginResult:
    return LoginResult(
        token=token,
        record=record,
        principal=Principal(
            user_id=user.user_id,
            name=user.name,
            role=user.role,
            token_id=record.id,
            token_kind=record.kind,
        ),
    )
