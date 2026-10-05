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

from pathlib import Path
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator

from meshforge.common.config import MeshForgeSettings, SectionModel
from meshforge.common.errors import ConfigError
from meshforge.common.fabric_token import (
    FabricTokenError,
    default_token_path,
    parse_token,
    read_token_file,
)

__all__ = [
    "DEFAULT_CONTROLLER_URL",
    "MAX_TIMEOUT_S",
    "CliSection",
    "CliSettings",
    "normalise_controller_url",
    "resolve_token",
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

    token: SecretStr | None = Field(
        default=None,
        description=(
            "The fabric token (D44) sent as 'Authorization: Bearer' on job and blob "
            "calls; `meshforge-controller token show` prints it. Env: "
            "MESHFORGE_CLI__TOKEN. Never logged, never put in a URL."
        ),
    )
    token_file: Path | None = Field(
        default=None,
        description=(
            "A file holding the fabric token (used when `token` is unset). "
            "Env: MESHFORGE_CLI__TOKEN_FILE; `compute --token-file` overrides both."
        ),
    )
    seed_local: bool = Field(
        default=True,
        description=(
            "V0.6 D109: when this laptop runs an enrolled agent, `compute run` "
            "also copies the project's files into that agent's cache, so running "
            "the job here (Local) needs no download. Skipped (logged) when the "
            "cache is not writable, the disk is short or the project is larger "
            "than seed_max_bytes. Env: MESHFORGE_CLI__SEED_LOCAL."
        ),
    )
    seed_max_bytes: int = Field(
        default=2 * 1024**3,
        ge=0,
        le=1024**4,
        description=(
            "Largest project (bytes) `compute run` copies into the local agent's "
            "cache; larger projects are not seeded (the agent downloads them if "
            "the job runs here). 0 = never seed. Env: MESHFORGE_CLI__SEED_MAX_BYTES."
        ),
    )
    seed_free_disk_floor_mb: int = Field(
        default=1024,
        ge=0,
        le=1024**2,
        description=(
            "Seeding stops (and is skipped) rather than leave less than this "
            "much free on the agent's disk - the agent's own floor. Env: "
            "MESHFORGE_CLI__SEED_FREE_DISK_FLOOR_MB."
        ),
    )

    @field_validator("controller_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return normalise_controller_url(value)

    @field_validator("token")
    @classmethod
    def _token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            return parse_token(value.get_secret_value())
        except FabricTokenError as exc:
            raise ValueError(exc.message) from None

    @field_validator("token_file", mode="before")
    @classmethod
    def _token_file(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("must not be empty (it would mean the current directory)")
        return value


class CliSettings(MeshForgeSettings):
    """Settings behind the ``compute`` console script."""

    cli: CliSection = Field(default_factory=CliSection)


def resolve_token(
    settings: CliSettings, token_file: Path | None = None
) -> SecretStr | None:
    """Which fabric token ``compute`` sends, highest precedence first.

    1. ``--token-file`` (``token_file`` here);
    2. ``[cli] token`` / ``MESHFORGE_CLI__TOKEN``;
    3. ``[cli] token_file`` / ``MESHFORGE_CLI__TOKEN_FILE``;
    4. the local controller's own ``<data_dir>/controller/fabric.token`` if this
       laptop runs the controller (read-only; silently skipped when absent).

    ``None`` means "send no token": the open node/session reads still work and a
    protected call gets the controller's ``unauthorized`` answer. An explicitly
    named file that is missing or malformed is a :class:`ConfigError` - a typo in
    a path must not quietly fall through to "no token".
    """
    if token_file is not None:
        return _read_named(token_file)
    if settings.cli.token is not None:
        return settings.cli.token
    if settings.cli.token_file is not None:
        return _read_named(settings.cli.token_file)
    local = default_token_path(settings.data_dir)
    try:
        return read_token_file(local)
    except FabricTokenError:
        return None


def _read_named(path: Path) -> SecretStr:
    try:
        return read_token_file(path)
    except FabricTokenError as exc:
        raise ConfigError(exc.message, details=exc.details) from None
