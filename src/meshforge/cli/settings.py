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

from cryptography import x509
from pydantic import Field, SecretStr, field_validator, model_validator

from meshforge.cli.credentials import StoredLogin
from meshforge.common import pki
from meshforge.common.config import MeshForgeSettings, SectionModel
from meshforge.common.errors import ConfigError
from meshforge.common.fabric_token import (
    CONTROLLER_DIR_NAME,
    FabricTokenError,
    default_token_path,
    parse_token,
    read_token_file,
)
from meshforge.common.local_node import IDENTITY_DIR_NAME
from meshforge.common.netaddr import is_loopback_host

__all__ = [
    "DEFAULT_CONTROLLER_URL",
    "MAX_TIMEOUT_S",
    "CliSection",
    "CliSettings",
    "normalise_controller_url",
    "resolve_ca",
    "resolve_token",
]

DEFAULT_CONTROLLER_URL: Final = "https://127.0.0.1:47110"
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
            "A bearer token sent as 'Authorization: Bearer' instead of the one "
            "`compute login` stored: an API token, or the legacy fabric token "
            "(D44; accepted only while [controller.auth] legacy_fabric_token is "
            "on). Env: MESHFORGE_CLI__TOKEN. Never logged, never put in a URL."
        ),
    )
    token_file: Path | None = Field(
        default=None,
        description=(
            "A file holding a bearer token (used when `token` is unset). "
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

    ca_file: Path | None = Field(
        default=None,
        description=(
            "V0.7 G3: the fabric CA certificate (ca.crt) the controller's HTTPS "
            "certificate must chain to. Empty = this laptop's agent enrolment "
            "(<data_dir>/identity/ca.crt), else the local controller's "
            "(<data_dir>/controller/ca/ca.crt). `compute --ca PATH` overrides. "
            "Env: MESHFORGE_CLI__CA_FILE."
        ),
    )

    credentials_dir: Path | None = Field(
        default=None,
        description=(
            "V0.7 G4: where `compute login` keeps the API token and the "
            "controller's ca.crt (private files). Empty = <config dir>/compute. "
            "Env: MESHFORGE_CLI__CREDENTIALS_DIR."
        ),
    )

    @field_validator("controller_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return normalise_controller_url(value)

    @field_validator("ca_file", "credentials_dir", mode="before")
    @classmethod
    def _ca_file(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("must not be empty (it would mean the current directory)")
        return value

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

    @model_validator(mode="after")
    def _scheme_matches_security(self) -> CliSettings:
        """D139: mtls speaks only HTTPS; insecure-dev only to loopback."""
        parts = urlsplit(self.cli.controller_url)
        if not self.security.insecure:
            if parts.scheme != "https":
                raise ValueError(
                    "cli.controller_url must be https:// - the controller's REST "
                    "port is HTTPS since V0.7 ([security] mode = 'mtls'); write "
                    f"https://{parts.netloc}"
                )
            return self
        if not is_loopback_host(parts.hostname or ""):
            raise ValueError(
                "[security] mode = 'insecure-dev' needs a loopback "
                f"cli.controller_url, got {self.cli.controller_url}"
            )
        return self


def resolve_ca(
    settings: CliSettings,
    ca_file: Path | None = None,
    *,
    stored: StoredLogin | None = None,
) -> x509.Certificate:
    """The fabric CA ``compute`` trusts (V0.7 G3, D139), highest precedence first:
    ``--ca``, ``[cli] ca_file``, the CA ``compute login`` stored (V0.7 G4,
    ``stored``), this laptop's agent enrolment, the local controller's CA.
    Nothing found or unreadable -> :class:`ConfigError`."""
    named = ca_file or settings.cli.ca_file
    if named is None and stored is not None and stored.ca_file is not None:
        named = stored.ca_file
    candidates = (
        [named]
        if named is not None
        else [
            settings.data_dir / IDENTITY_DIR_NAME / "ca.crt",
            settings.data_dir / CONTROLLER_DIR_NAME / "ca" / "ca.crt",
        ]
    )
    for path in candidates:
        if named is None and not path.exists():
            continue
        try:
            return pki.read_cert(path, what="fabric CA certificate")
        except pki.PkiError as exc:
            raise ConfigError(
                f"cannot use {path} as the fabric CA certificate: {exc.message}",
                details={"path": str(path)},
            ) from None
    raise ConfigError(
        "no fabric CA certificate to verify the controller with: run `compute "
        "login --fingerprint FP` (the admin reads FP from `meshforge-controller "
        "fabric show`), or pass --ca PATH / set [cli] ca_file; on a laptop with "
        "an enrolled agent it is found automatically"
    )


def resolve_token(
    settings: CliSettings,
    token_file: Path | None = None,
    *,
    stored: StoredLogin | None = None,
) -> SecretStr | None:
    """Which bearer token ``compute`` sends, highest precedence first.

    1. ``--token-file`` (``token_file`` here);
    2. ``[cli] token`` / ``MESHFORGE_CLI__TOKEN``;
    3. ``[cli] token_file`` / ``MESHFORGE_CLI__TOKEN_FILE``;
    4. the API token ``compute login`` stored (V0.7 G4, ``stored``: only when
       it was issued by the controller being called);
    5. the local controller's own ``<data_dir>/controller/fabric.token`` if this
       laptop runs the controller (read-only; silently skipped when absent) -
       accepted only while the controller's legacy switch is on (D141).

    ``None`` means "send no token": the call gets the controller's
    ``unauthorized`` answer ("run compute login"). An explicitly named file
    that is missing or malformed is a :class:`ConfigError` - a typo in a path
    must not quietly fall through to "no token".
    """
    if token_file is not None:
        return _read_named(token_file)
    if settings.cli.token is not None:
        return settings.cli.token
    if settings.cli.token_file is not None:
        return _read_named(settings.cli.token_file)
    if stored is not None:
        return stored.token
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
