"""``compute`` settings (the ``[cli]`` section, roadmap 0.3.12).

Layout follows :mod:`meshforge.common.config`: one TOML file may serve every
process on a machine, so the CLI's keys live in their own ``[cli]`` table and each
one also has an environment override
(``MESHFORGE_CLI__CONTROLLER_URL=http://192.168.1.20:47110``). Common keys
(``data_dir``, ``[log]``) are inherited from
:class:`~meshforge.common.config.MeshForgeSettings`.

The CLI speaks **only** the controller's read-only REST surface (``/v1``, D15): it
never opens a database connection and never dials gRPC, which is what keeps
``meshforge.cli`` importing nothing but ``common`` and ``sdk``.

This module imports no optional dependency, so ``compute --version`` still works on
a base install.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator

from meshforge.common.config import MeshForgeSettings, SectionModel
from meshforge.common.errors import ConfigError

__all__ = [
    "DEFAULT_CONTROLLER_URL",
    "MAX_TIMEOUT_S",
    "CliSection",
    "CliSettings",
    "normalise_controller_url",
]

DEFAULT_CONTROLLER_URL: Final = "http://127.0.0.1:47110"
"""The controller's default REST bind (ARCHITECTURE §23: 47100 gRPC, 47110 REST).

Loopback, because V0.3's REST surface is unauthenticated (D14): pointing the CLI at
another laptop is an explicit choice the operator makes.
"""

MAX_TIMEOUT_S: Final = 300.0
"""Upper bound on ``[cli] timeout_s``: a CLI that hangs longer than this is a bug,
not patience. ``--watch`` uses the same timeout per refresh."""

_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})


def normalise_controller_url(text: str) -> str:
    """Validate ``text`` as an absolute http(s) base URL; strip a trailing slash.

    Raises:
        ConfigError: no scheme, an unsupported scheme, or no host. A bare
            ``127.0.0.1:47110`` is rejected on purpose - ``urlsplit`` would read
            ``127.0.0.1`` as the *scheme*, so guessing would be worse than asking.
    """
    raw = text.strip()
    if not raw:
        raise ConfigError("cli.controller_url must not be empty")
    parts = urlsplit(raw)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ConfigError(
            "cli.controller_url needs an http:// or https:// scheme "
            f"(got {parts.scheme or '<none>'!r}; write http://host:47110)",
            details={"scheme": parts.scheme},
        )
    if not parts.hostname:
        raise ConfigError("cli.controller_url has no host (write http://host:47110)")
    if parts.query or parts.fragment:
        raise ConfigError("cli.controller_url must not carry a query or a fragment")
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc, parts.path.rstrip("/"), "", "")
    )


class CliSection(SectionModel):
    """``[cli]`` - how ``compute`` reaches the controller."""

    controller_url: str = Field(
        default=DEFAULT_CONTROLLER_URL,
        description=(
            "Base URL of the controller's REST surface, scheme included "
            "(the CLI appends /v1/...). Env: MESHFORGE_CLI__CONTROLLER_URL."
        ),
    )
    timeout_s: float = Field(
        default=5.0,
        gt=0,
        le=MAX_TIMEOUT_S,
        description=(
            "Per-request timeout (connect and read) in seconds; on expiry the "
            "command exits UNAVAILABLE instead of hanging. Env: "
            "MESHFORGE_CLI__TIMEOUT_S."
        ),
    )
    watch_interval_s: float = Field(
        default=2.0,
        ge=0.5,
        le=60.0,
        description=(
            "How often `compute nodes --watch` re-renders. Floored at 0.5 s so a "
            "watch loop cannot flood the controller. Env: "
            "MESHFORGE_CLI__WATCH_INTERVAL_S."
        ),
    )

    @field_validator("controller_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return normalise_controller_url(value)


class CliSettings(MeshForgeSettings):
    """Settings behind the ``compute`` console script."""

    cli: CliSection = Field(default_factory=CliSection)
