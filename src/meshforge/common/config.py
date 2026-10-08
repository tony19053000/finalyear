"""Configuration loading shared by all MeshForge processes.

Components subclass :class:`MeshForgeSettings` (``ControllerSettings``,
``AgentSettings`` arrive with their components) and add their own section models.

**Source precedence** (highest first):

1. keyword arguments (``MeshForgeSettings.load(log={"level": "DEBUG"})``);
2. environment variables — prefix ``MESHFORGE_``, nested delimiter ``__``
   (``MESHFORGE_LOG__LEVEL=DEBUG``, ``MESHFORGE_DATA_DIR=/srv/mf``);
3. a TOML file — the ``config_file`` argument of :meth:`MeshForgeSettings.load`,
   else ``$MESHFORGE_CONFIG``, else ``<config dir>/config.toml`` if it exists;
4. field defaults.

**Entry point.** :meth:`MeshForgeSettings.load` is the only supported way to build
settings: it selects the TOML file for this call. Calling the class directly
(``MeshForgeSettings()``) bypasses the explicit ``config_file`` argument.

**Empty values.** An *empty* environment variable (``MESHFORGE_DATA_DIR=""``, e.g.
a failed secret injection in CI/Docker) is treated as **unset**, so the next
source/default applies. An empty ``data_dir`` given explicitly (TOML or keyword)
is a :class:`~meshforge.common.errors.ConfigError` — ``Path("")`` would silently
mean "the current directory".

**TOML on Windows.** In TOML double-quoted strings ``\\`` starts an escape, so
``data_dir = "C:\\Users\\me"`` (single backslashes) is a syntax error. Use forward
slashes (``"C:/Users/me"``) or a single-quoted literal string
(``'C:\\Users\\me'``). A UTF-8 byte-order mark (Notepad's default) is accepted.

Nested sections are merged across sources, so TOML ``[log] format = "console"`` and
``MESHFORGE_LOG__LEVEL=DEBUG`` combine. ``.env`` files and secret directories are
deliberately not read (one less place for secrets to hide; add later if needed).

**File layout.** One file may be shared by every process on a machine: common keys
at the top level, component keys in their own table (``[controller]``, ``[agent]``).
Therefore unknown *top-level* keys are ignored, while section models forbid unknown
keys so typos inside a section are reported.

**Errors.** Missing explicitly-requested files, TOML syntax errors and validation
failures raise :class:`~meshforge.common.errors.ConfigError` with a readable message.
Offending *input values are never included* (they may be secrets).

**Secrets** are typed ``pydantic.SecretStr`` in subclasses: masked in ``repr`` and
redacted by the logging pipeline.

**Default directories** (no extra dependency, see :func:`platform_dirs`):

=========  ==========================================  =====================================
Platform   config dir                                  data dir
=========  ==========================================  =====================================
Linux      ``$XDG_CONFIG_HOME/meshforge``              ``$XDG_DATA_HOME/meshforge``
           (default ``~/.config/meshforge``)           (default ``~/.local/share/meshforge``)
Windows    ``%APPDATA%\\MeshForge``                     ``%LOCALAPPDATA%\\MeshForge``
macOS      ``~/Library/Application Support/MeshForge``  same
=========  ==========================================  =====================================
"""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Mapping
from contextvars import ContextVar
from enum import StrEnum
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, ClassVar, Final, Literal, NamedTuple, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from meshforge.common.errors import ConfigError

__all__ = [
    "APP_NAME",
    "CONFIG_ENV_VAR",
    "ENV_PREFIX",
    "INSECURE_DEV_BANNER",
    "LoggingSettings",
    "MeshForgeSettings",
    "PlatformDirs",
    "SectionModel",
    "SecurityMode",
    "SecuritySettings",
    "default_config_dir",
    "default_config_file",
    "default_data_dir",
    "platform_dirs",
    "resolve_config_file",
]

APP_NAME = "meshforge"
_WINDOWS_APP_NAME = "MeshForge"
ENV_PREFIX = "MESHFORGE_"
CONFIG_ENV_VAR = "MESHFORGE_CONFIG"
CONFIG_FILE_NAME = "config.toml"

type StrPath = str | os.PathLike[str]


# --------------------------------------------------------------------------- dirs


class PlatformDirs(NamedTuple):
    config_dir: PurePath
    data_dir: PurePath


def _env_abs(
    environ: Mapping[str, str], name: str, path_cls: type[PurePath]
) -> PurePath | None:
    value = environ.get(name, "").strip()
    if not value:
        return None
    path = path_cls(value)
    return path if path.is_absolute() else None  # XDG spec: relative values are ignored


def platform_dirs(
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | None = None,
) -> PlatformDirs:
    """Compute default config/data dirs for ``platform`` (defaults: this machine).

    Returns pure paths so another platform's layout can be computed (and tested)
    on any OS; public helpers convert to :class:`~pathlib.Path`.
    """
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ

    if platform.startswith("win"):
        win_home = PureWindowsPath(
            home or environ.get("USERPROFILE") or str(Path.home())
        )
        roaming = (
            _env_abs(environ, "APPDATA", PureWindowsPath)
            or win_home / "AppData" / "Roaming"
        )
        local = (
            _env_abs(environ, "LOCALAPPDATA", PureWindowsPath)
            or win_home / "AppData" / "Local"
        )
        return PlatformDirs(roaming / _WINDOWS_APP_NAME, local / _WINDOWS_APP_NAME)

    posix_home = PurePosixPath(home or environ.get("HOME") or str(Path.home()))
    if platform == "darwin":
        base = posix_home / "Library" / "Application Support" / _WINDOWS_APP_NAME
        return PlatformDirs(base, base)
    config = (
        _env_abs(environ, "XDG_CONFIG_HOME", PurePosixPath) or posix_home / ".config"
    )
    data = (
        _env_abs(environ, "XDG_DATA_HOME", PurePosixPath)
        or posix_home / ".local" / "share"
    )
    return PlatformDirs(config / APP_NAME, data / APP_NAME)


def default_config_dir() -> Path:
    return Path(platform_dirs().config_dir)


def default_data_dir() -> Path:
    return Path(platform_dirs().data_dir)


def default_config_file() -> Path:
    return default_config_dir() / CONFIG_FILE_NAME


def resolve_config_file(
    config_file: StrPath | None = None, environ: Mapping[str, str] | None = None
) -> tuple[Path | None, bool]:
    """Return ``(path, explicit)``: which TOML file to read and whether it was requested.

    An explicit file (argument or ``$MESHFORGE_CONFIG``) must exist; the default file
    is optional.
    """
    environ = os.environ if environ is None else environ
    if config_file is not None:
        return Path(config_file), True
    from_env = environ.get(CONFIG_ENV_VAR, "").strip()
    if from_env:
        return Path(from_env), True
    default = default_config_file()
    return (default, False) if default.is_file() else (None, False)


# ------------------------------------------------------------------------ sources

# Explicit file chosen by ``load()`` for the settings object being built right now.
_explicit_file: ContextVar[Path | None] = ContextVar(
    "meshforge_config_file", default=None
)


_UTF8_BOM = b"\xef\xbb\xbf"
_WINDOWS_PATH_HINT = (
    " (hint: in double-quoted TOML strings a backslash starts an escape; write "
    'Windows paths with forward slashes, "C:/Users/me", or as a single-quoted '
    "literal string, 'C:\\Users\\me')"
)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        text = raw.removeprefix(_UTF8_BOM).decode("utf-8")  # Notepad writes a BOM
        return tomllib.loads(text)
    except UnicodeDecodeError:
        raise ConfigError(
            f"config file {path} is not valid UTF-8 text",
            details={"config_file": str(path)},
        ) from None
    except tomllib.TOMLDecodeError as exc:
        reason = str(exc)
        hint = (
            _WINDOWS_PATH_HINT
            if "escape" in reason.lower() or "hex" in reason.lower()
            else ""
        )
        raise ConfigError(
            f"config file {path} is not valid TOML: {reason}{hint}",
            details={"config_file": str(path)},
        ) from None
    except OSError as exc:
        raise ConfigError(
            f"cannot read config file {path}: {exc.strerror or exc}",
            details={"config_file": str(path)},
        ) from None


class _TomlFileSource(PydanticBaseSettingsSource):
    """TOML source whose file is resolved at construction time (see module docs)."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        path, explicit = resolve_config_file(_explicit_file.get())
        self.path = path
        self._data: dict[str, Any] = {}
        if path is None:
            return
        if not path.is_file():
            if explicit:
                raise ConfigError(
                    f"config file not found: {path}", details={"config_file": str(path)}
                )
            return
        self._data = _read_toml(path)

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._data)


# ----------------------------------------------------------------------- settings


class SectionModel(BaseModel):
    """Base for config sections: unknown keys are errors (catches typos)."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class LoggingSettings(SectionModel):
    """``[log]`` section — consumed by :func:`meshforge.common.logging.configure_logging`."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: Literal["json", "console"] = "json"

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class SecurityMode(StrEnum):
    """``[security] mode`` (V0.7 G3, D139)."""

    MTLS = "mtls"
    """Every plane is TLS: mutual TLS on the agent session and the peer blob
    servers, HTTPS on the controller REST/blob port. The default."""
    INSECURE_DEV = "insecure-dev"
    """Plaintext everywhere, the agent's claimed ``node_id`` is trusted - for a
    developer's single machine only: every bind, the agent's controller address
    and the CLI's controller URL must be loopback, and a banner is logged at
    every start."""


INSECURE_DEV_BANNER: Final = (
    "SECURITY IS OFF: [security] mode = 'insecure-dev' - plaintext on every plane, "
    "node identity not verified, loopback only. Never use it on a network."
)


class SecuritySettings(SectionModel):
    """``[security]`` - shared by the controller, the agent and the CLI."""

    mode: SecurityMode = Field(
        default=SecurityMode.MTLS,
        description=(
            "'mtls' (default): TLS with fabric certificates on every plane. "
            "'insecure-dev': plaintext, loopback-only, for development on one "
            "machine (refused with any non-loopback address)."
        ),
    )

    @property
    def insecure(self) -> bool:
        return self.mode is SecurityMode.INSECURE_DEV


class MeshForgeSettings(BaseSettings):
    """Base settings shared by every MeshForge process. Subclass per component."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        env_ignore_empty=True,  # MESHFORGE_X="" means unset (see module docstring)
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
        validate_default=True,
    )

    source_description: ClassVar[str] = (
        f"init arguments, {ENV_PREFIX}* environment, TOML file"
    )

    log: LoggingSettings = Field(default_factory=LoggingSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    data_dir: Path = Field(
        default_factory=default_data_dir,
        description="Root for local state: blob cache, environments, identity, logs.",
    )

    @field_validator("data_dir", mode="before")
    @classmethod
    def _non_empty_path(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("must not be empty (it would mean the current directory)")
        return value

    def __init__(self, *args: Any, **values: Any) -> None:
        try:
            super().__init__(*args, **values)
        except ValidationError as exc:
            raise _config_error(type(self), exc) from None

    @classmethod
    def load(cls, config_file: StrPath | None = None, **overrides: Any) -> Self:
        """Build settings from all sources (see module docstring for precedence).

        Raises:
            ConfigError: missing explicit file, bad TOML, or validation failure.
        """
        token = _explicit_file.set(
            Path(config_file) if config_file is not None else None
        )
        try:
            return cls(**overrides)
        finally:
            _explicit_file.reset(token)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, env_settings, _TomlFileSource(settings_cls))


def _config_error(
    settings_cls: type[BaseSettings], exc: ValidationError
) -> ConfigError:
    problems = [
        {
            "loc": ".".join(str(part) for part in err["loc"]) or "<root>",
            "msg": err["msg"],
            "type": err["type"],
        }
        for err in exc.errors(include_input=False, include_url=False)
    ]
    path, _ = resolve_config_file(_explicit_file.get())
    summary = "; ".join(f"{p['loc']}: {p['msg']}" for p in problems)
    return ConfigError(
        f"invalid configuration for {settings_cls.__name__}: {summary} "
        f"(sources: init arguments, {ENV_PREFIX}* environment variables, "
        f"config file {path if path is not None else '<none>'})",
        details={"errors": problems, "config_file": str(path) if path else None},
    )
