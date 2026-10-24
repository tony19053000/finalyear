"""The first admin's password rule (V0.17 G2, D392 "strength check").

The first admin of a fabric can run code on every laptop in it, so the setup
page asks more than the general user rule (8+ characters,
:func:`~meshforge.controller.auth.passwords.check_password_policy`): at least 12
characters, at least 6 different characters, not containing the user name, and
not one of the commonest passwords. Pure; the message never echoes the password.
"""

from __future__ import annotations

from typing import Final

from pydantic import SecretStr

from meshforge.controller.auth.passwords import PasswordError, check_password_policy

__all__ = ["ADMIN_MIN_CHARS", "ADMIN_MIN_DISTINCT", "check_admin_password"]

ADMIN_MIN_CHARS: Final = 12
ADMIN_MIN_DISTINCT: Final = 6
_COMMON: Final = frozenset(
    {
        "password1234",
        "passwordpassword",
        "123456789012",
        "qwertyuiopas",
        "administrator",
        "adminadminadmin",
        "letmeinletmein",
        "meshforge123",
        "meshforgeadmin",
        "changemechangeme",
        "iloveyou1234",
        "welcome12345",
    }
)


def check_admin_password(name: str, password: SecretStr) -> None:
    """Raise :class:`PasswordError` (``invalid_input``) unless ``password`` is
    acceptable for the first admin ``name`` (module docstring)."""
    check_password_policy(password)
    value = password.get_secret_value()
    if len(value) < ADMIN_MIN_CHARS:
        raise PasswordError(
            f"the admin password needs at least {ADMIN_MIN_CHARS} characters"
        )
    if len(set(value)) < ADMIN_MIN_DISTINCT:
        raise PasswordError(
            f"the admin password needs at least {ADMIN_MIN_DISTINCT} different "
            "characters"
        )
    lowered = value.lower()
    if name and name.lower() in lowered:
        raise PasswordError("the admin password must not contain the user name")
    if lowered in _COMMON:
        raise PasswordError("this password is too common; choose another")
