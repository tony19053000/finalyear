"""Job secret names and values: the one set of rules (V0.7 G6, roadmap 0.7.10, D144).

Used by the controller (REST, store), the CLI (``compute secret``), the manifest
(``secrets:``) and the agent (file and env-var names), so all four agree.

**Names** are normalised like login names (V0.7 G5 T-5): surrounding white
space is stripped, the name must be **ASCII before** it is case-folded (so no
Unicode spelling - a Kelvin sign, a full-width letter - can alias another
name), then it is upper-cased and must match ``[A-Z_][A-Z0-9_]{0,63}``. The
first character is never a digit: the name is also a file name
(``/run/secrets/NAME``) and, with ``as_env``, an environment variable name.

**Values** are bytes, at least :data:`MIN_SECRET_BYTES` and at most
:data:`MAX_SECRET_BYTES`. The minimum exists because every log line is scrubbed
of each secret value (and its encodings) before it is stored or streamed: a
1-3 byte value would match all over ordinary output and destroy the logs.
"""

from __future__ import annotations

import re
from typing import Final

from meshforge.common.errors import FailureType, MeshForgeError

__all__ = [
    "MAX_SECRET_BYTES",
    "MAX_SECRET_NAME_CHARS",
    "MIN_SECRET_BYTES",
    "RESERVED_ENV_NAMES",
    "RESERVED_ENV_PREFIXES",
    "SECRET_NAME_PATTERN",
    "SecretNameError",
    "SecretValueError",
    "check_env_name",
    "check_secret_value",
    "normalise_secret_name",
]

MAX_SECRET_NAME_CHARS: Final = 64
SECRET_NAME_PATTERN: Final = "^[A-Z_][A-Z0-9_]{0,63}$"  # noqa: S105 - a pattern
_NAME_RE: Final = re.compile(SECRET_NAME_PATTERN)
MIN_SECRET_BYTES: Final = 4
MAX_SECRET_BYTES: Final = 64 * 1024
RESERVED_ENV_PREFIXES: Final = ("MESHFORGE_",)
"""``as_env`` may not set a variable the agent itself sets for the job."""
RESERVED_ENV_NAMES: Final = frozenset(
    {"PATH", "HOME", "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONHOME"}
)


class SecretNameError(MeshForgeError, ValueError):
    """A secret name that breaks the rules (module docstring)."""

    default_failure_type = FailureType.INVALID_INPUT


class SecretValueError(MeshForgeError, ValueError):
    """A secret value that is empty, too short or too large."""

    default_failure_type = FailureType.INVALID_INPUT


def normalise_secret_name(raw: object) -> str:
    """The canonical name, or :class:`SecretNameError` (never echoes the input:
    a user may paste a value where the name goes)."""
    if not isinstance(raw, str):
        raise SecretNameError("a secret name must be a string")
    name = raw.strip()
    if not name.isascii():
        raise SecretNameError(
            "a secret name must be ASCII: letters, digits and _ (1-64 characters)"
        )
    name = name.upper()
    if not _NAME_RE.fullmatch(name):
        raise SecretNameError(
            "a secret name must be 1-64 characters of A-Z, 0-9 and _, "
            "not starting with a digit"
        )
    return name


def check_env_name(name: str) -> str:
    """``as_env``: the (normalised) name must not override what the agent sets."""
    if name in RESERVED_ENV_NAMES or name.startswith(RESERVED_ENV_PREFIXES):
        raise SecretNameError(
            f"secret {name} cannot be an environment variable (reserved name); "
            "read it from its file instead"
        )
    return name


def check_secret_value(value: bytes) -> bytes:
    """``value`` if it is a storable secret, else :class:`SecretValueError`."""
    if not isinstance(value, bytes | bytearray):
        raise SecretValueError("a secret value must be bytes")
    if len(value) < MIN_SECRET_BYTES:
        raise SecretValueError(
            f"a secret value must be at least {MIN_SECRET_BYTES} bytes (shorter "
            "values cannot be scrubbed from logs without destroying them)"
        )
    if len(value) > MAX_SECRET_BYTES:
        raise SecretValueError(
            f"a secret value must be at most {MAX_SECRET_BYTES} bytes"
        )
    return bytes(value)
