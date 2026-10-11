"""Password hashing for REST users (V0.7 G4, D141): stdlib scrypt, no new dependency.

* **Parameters** ``n = 2**15, r = 8, p = 1`` (D141) with a 16-byte salt from
  :mod:`secrets` and a 32-byte key: about 32 MiB and tens of milliseconds per
  hash, which is what makes an offline guess expensive.
* **Format** (self-describing, so the parameters can be raised later without a
  migration): ``scrypt$<n>$<r>$<p>$<salt b64>$<key b64>``.
* **Verify** in constant time (:func:`hmac.compare_digest`); a stored string with
  a malformed shape or out-of-range parameters never verifies (fail closed), and
  the bounds stop a tampered row from making one check cost gigabytes.
* **Off the event loop.** scrypt is CPU and memory bound, so :class:`PasswordHasher`
  runs it in a worker thread, at most ``max_concurrent`` at a time - a burst of
  logins queues instead of allocating 32 MiB each at once.
* A login for a name that does not exist still runs one verification against a
  fixed dummy hash (:meth:`PasswordHasher.verify_or_dummy`), so its timing does not
  say whether the name exists.

The password itself is only ever a :class:`~pydantic.SecretStr` here; it is never
logged, never stored and never part of an error message.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import secrets
from typing import Final

from pydantic import SecretStr

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError

__all__ = [
    "MAX_PASSWORD_CHARS",
    "MIN_PASSWORD_CHARS",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "PasswordError",
    "PasswordHasher",
    "check_password_policy",
    "hash_password",
    "verify_password",
]

SCRYPT_N: Final = 2**15
SCRYPT_R: Final = 8
SCRYPT_P: Final = 1
_SALT_BYTES: Final = 16
_KEY_BYTES: Final = 32
_MAX_N: Final = 2**20
_MAX_R: Final = 16
_MAX_P: Final = 4
MIN_PASSWORD_CHARS: Final = 8
MAX_PASSWORD_CHARS: Final = 1024
"""Longer is refused before hashing (scrypt cost grows with input only a
little, but an unbounded field is still unbounded input)."""
_PREFIX: Final = "scrypt"


class PasswordError(MeshForgeError):
    """A password that does not meet the policy (the message never echoes it)."""

    default_failure_type = FailureType.INVALID_INPUT
    exit_code = ExitCode.USAGE_ERROR


def check_password_policy(password: SecretStr) -> None:
    """Length 8-1024 characters, no NUL; raise :class:`PasswordError` otherwise."""
    value = password.get_secret_value()
    if not MIN_PASSWORD_CHARS <= len(value) <= MAX_PASSWORD_CHARS:
        raise PasswordError(
            f"a password is {MIN_PASSWORD_CHARS}-{MAX_PASSWORD_CHARS} characters"
        )
    if "\x00" in value:
        raise PasswordError("a password must not contain a NUL character")


def _maxmem(n: int, r: int) -> int:
    # OpenSSL needs 128 * n * r bytes plus a little; hashlib's default limit
    # (32 MiB) is exactly the D141 parameters' need and would refuse them.
    return 128 * n * r * 2


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8", "surrogatepass"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=_maxmem(n, r),
        dklen=_KEY_BYTES,
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: SecretStr) -> str:
    """The stored form of ``password`` (blocking; see :class:`PasswordHasher`)."""
    salt = secrets.token_bytes(_SALT_BYTES)
    key = _derive(password.get_secret_value(), salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"{_PREFIX}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(key)}"


def _parse(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != _PREFIX:
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4], validate=True)
        key = base64.b64decode(parts[5], validate=True)
    except (ValueError, binascii.Error):
        return None
    if not (2 <= n <= _MAX_N and n & (n - 1) == 0):
        return None
    if not (1 <= r <= _MAX_R and 1 <= p <= _MAX_P):
        return None
    if len(salt) < 8 or len(key) != _KEY_BYTES:
        return None
    return n, r, p, salt, key


def verify_password(password: SecretStr, stored: str) -> bool:
    """True only when ``password`` matches ``stored`` (blocking; constant-time)."""
    parsed = _parse(stored)
    if parsed is None:
        return False
    n, r, p, salt, key = parsed
    value = password.get_secret_value()
    if len(value) > MAX_PASSWORD_CHARS:
        return False
    try:
        candidate = _derive(value, salt, n, r, p)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(candidate, key)


class PasswordHasher:
    """Async front of :func:`hash_password` / :func:`verify_password`: worker
    thread, at most ``max_concurrent`` at once."""

    def __init__(self, *, max_concurrent: int = 4) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be positive")
        self._slots = asyncio.Semaphore(max_concurrent)
        self._dummy: str | None = None

    async def hash(self, password: SecretStr) -> str:
        check_password_policy(password)
        async with self._slots:
            return await asyncio.to_thread(hash_password, password)

    async def verify(self, password: SecretStr, stored: str) -> bool:
        async with self._slots:
            return await asyncio.to_thread(verify_password, password, stored)

    async def verify_or_dummy(self, password: SecretStr, stored: str | None) -> bool:
        """:meth:`verify`, or - with no stored hash (unknown user) - the same
        work against a dummy hash, returning False."""
        if stored is not None:
            return await self.verify(password, stored)
        if self._dummy is None:
            async with self._slots:
                self._dummy = await asyncio.to_thread(
                    hash_password, SecretStr(secrets.token_urlsafe(16))
                )
        await self.verify(password, self._dummy)
        return False
