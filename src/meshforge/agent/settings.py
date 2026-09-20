"""Agent settings (``[agent]`` section, roadmap 0.3.8 / 0.3.9).

Layout follows :mod:`meshforge.common.config`: one TOML file may serve every process
on a machine, so agent keys live in their own ``[agent]`` table
(``MESHFORGE_AGENT__CONTROLLER_ADDRESS=192.168.1.20:47100`` from the environment).
Common keys (``data_dir``, ``[log]``) are inherited from
:class:`~meshforge.common.config.MeshForgeSettings`.

Where things live (ARCHITECTURE §23): the node identity is ``data_dir/identity``
(:attr:`AgentSettings.identity_dir`); the controller is reached at
``agent.controller_address`` or, when that is empty, found on the LAN via mDNS
(``agent.discovery``). Reconnect timing comes from ADR-0002 Updates §3, gRPC
keepalive from the same section; the replay ring size from Updates §4.

This module imports no agent-extra package so ``meshforge-agent --version`` works
on a base install.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import Field, field_validator, model_validator

from meshforge import __version__
from meshforge.common.config import MeshForgeSettings, SectionModel
from meshforge.common.errors import ConfigError

__all__ = [
    "DEFAULT_SERVICE_TYPE",
    "IDENTITY_DIR_NAME",
    "AgentSection",
    "AgentSettings",
    "DiscoverySettings",
    "KeepaliveSettings",
    "ReconnectSettings",
    "parse_controller_address",
]

DEFAULT_SERVICE_TYPE: Final = "_meshforge-ctl._tcp.local."
"""mDNS service type the controller advertises and the agent browses for."""

IDENTITY_DIR_NAME: Final = "identity"
"""Sub-directory of ``data_dir`` holding ``node.key`` / ``node.json`` (ADR-0013)."""

_PORT_MAX: Final = 65535
_AGENT_VERSION_MAX_LENGTH: Final = 64
"""Matches the controller's ``Hello.agent_version`` cap (``node_session.agent_version``)."""


def parse_controller_address(text: str) -> tuple[str, int]:
    """Parse ``"host:port"`` / ``"[v6]:port"`` into ``(host, port)``.

    The agent *dials out*, so a hostname is acceptable here (the controller's bind
    rules in :mod:`meshforge.common.netaddr` are about listening). Raises
    :class:`~meshforge.common.errors.ConfigError` on a malformed value.
    """
    raw = text.strip()
    if raw.startswith("["):
        host, sep, rest = raw[1:].partition("]")
        if not sep or not rest.startswith(":"):
            raise ConfigError(
                f"controller_address must look like [ipv6]:port, got {text!r}",
                details={"controller_address": text},
            )
        port_text = rest[1:]
    else:
        host, sep, port_text = raw.rpartition(":")
        if not sep or ":" in host:
            raise ConfigError(
                f"controller_address must look like host:port (bracket IPv6), got {text!r}",
                details={"controller_address": text},
            )
    if not host:
        raise ConfigError(
            f"controller_address has an empty host: {text!r}",
            details={"controller_address": text},
        )
    try:
        port = int(port_text)
    except ValueError:
        raise ConfigError(
            f"controller_address has a non-numeric port: {text!r}",
            details={"controller_address": text},
        ) from None
    if not 1 <= port <= _PORT_MAX:
        raise ConfigError(
            f"controller_address port must be 1..{_PORT_MAX}, got {port}",
            details={"controller_address": text},
        )
    return host, port


class ReconnectSettings(SectionModel):
    """``[agent.reconnect]`` - full-jitter exponential backoff (ADR-0002 Updates §3).

    ``sleep = uniform(0, min(cap_s, base_s * 2**n))``; ``n`` resets to 0 after a
    stream that lived at least ``reset_after_s``. ``cap_s`` must stay <= 15 s: V0.3
    criterion 3 requires reconnection within 15 s of a controller restart.
    """

    base_s: float = Field(default=0.5, gt=0, description="First backoff ceiling.")
    cap_s: float = Field(
        default=15.0,
        gt=0,
        le=15.0,
        description="Upper bound of the backoff ceiling (<= 15 s, V0.3 criterion 3).",
    )
    reset_after_s: float = Field(
        default=30.0,
        ge=0,
        description="A stream that lived this long resets the backoff exponent.",
    )

    @model_validator(mode="after")
    def _cap_not_below_base(self) -> ReconnectSettings:
        if self.cap_s < self.base_s:
            raise ValueError("reconnect.cap_s must be >= reconnect.base_s")
        return self


class KeepaliveSettings(SectionModel):
    """``[agent.keepalive]`` - gRPC transport keepalive (ADR-0002 Updates §3).

    A dead TCP path is torn down by the transport after ``time_ms + timeout_ms``
    without waiting on the application lease.
    """

    time_ms: int = Field(
        default=10_000, ge=1_000, description="Interval between keepalive pings."
    )
    timeout_ms: int = Field(
        default=5_000, ge=100, description="Ping unanswered this long = dead path."
    )


class DiscoverySettings(SectionModel):
    """``[agent.discovery]`` - mDNS lookup of the controller (roadmap 0.3.9).

    Used only when ``agent.controller_address`` is empty. One browse lasts
    ``timeout_s``; a cycle that finds nothing ends in BACKOFF and the next cycle
    browses again (never a busy loop).
    """

    enabled: bool = Field(
        default=True,
        description="Browse mDNS when controller_address is empty. Off = fail fast.",
    )
    timeout_s: float = Field(
        default=5.0, gt=0, description="How long one browse waits for a record."
    )
    service_type: str = Field(
        default=DEFAULT_SERVICE_TYPE,
        description="DNS-SD service type; must match the controller's.",
    )

    @field_validator("service_type")
    @classmethod
    def _service_type_shape(cls, value: str) -> str:
        value = value.strip()
        if not value.endswith("._tcp.local.") or not value.startswith("_"):
            raise ValueError("service_type must look like _name._tcp.local.")
        return value


class AgentSection(SectionModel):
    """``[agent]`` section."""

    controller_address: str = Field(
        default="",
        description=(
            "host:port of the controller's gRPC listener (bracket IPv6). Empty = "
            "find it via mDNS (agent.discovery)."
        ),
    )
    reconnect: ReconnectSettings = Field(default_factory=ReconnectSettings)
    keepalive: KeepaliveSettings = Field(default_factory=KeepaliveSettings)
    replay_ring_size: int = Field(
        default=10_000,
        ge=1,
        description=(
            "Un-acked reports kept for replay after a reconnect; overflow drops the "
            "oldest and counts it (ADR-0002 Updates §4)."
        ),
    )
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    hello_timeout_s: float = Field(
        default=5.0,
        gt=0,
        description="How long to wait for HelloAck before the attempt counts as failed.",
    )
    agent_version: str = Field(
        default=__version__,
        description="Reported in Hello.agent_version; defaults to the package version.",
    )
    identity_lock_timeout_s: float = Field(
        default=10.0,
        gt=0,
        description=(
            "How long to wait for identity.lock held by another process before "
            "giving up with an IdentityError (ADR-0013 §6)."
        ),
    )

    @field_validator("controller_address")
    @classmethod
    def _address_shape(cls, value: str) -> str:
        value = value.strip()
        if value:
            parse_controller_address(value)
        return value

    @field_validator("agent_version")
    @classmethod
    def _version_length(cls, value: str) -> str:
        if not value or len(value) > _AGENT_VERSION_MAX_LENGTH:
            raise ValueError(
                f"agent_version must be 1..{_AGENT_VERSION_MAX_LENGTH} characters"
            )
        return value

    @model_validator(mode="after")
    def _some_way_to_find_the_controller(self) -> AgentSection:
        if not self.controller_address and not self.discovery.enabled:
            raise ValueError(
                "controller_address is empty and discovery.enabled is false: "
                "the agent has no way to find a controller"
            )
        return self

    @property
    def uses_discovery(self) -> bool:
        return not self.controller_address


class AgentSettings(MeshForgeSettings):
    """Everything the agent process needs (``[agent]`` + the common keys)."""

    agent: AgentSection = Field(default_factory=AgentSection)

    @property
    def identity_dir(self) -> Path:
        """``data_dir/identity`` - where :mod:`meshforge.agent.identity` keeps the node."""
        return self.data_dir / IDENTITY_DIR_NAME
