"""Agent <-> controller version compatibility (V0.17 G5, D393 second half, Q37).

The rule, decided once here and used by the controller's Hello admission, the
dashboard's Nodes page and the docs:

* a controller ``X.Y.*`` accepts agents of the **same** minor (``X.Y``) and of
  the **previous** minor (``X.(Y-1)``, or any ``(X-1).*`` when ``Y`` is 0);
* an **older** agent is refused: "upgrade the agent on <laptop>";
* a **newer** agent (a higher minor than the controller) is refused too:
  "upgrade the controller first" - a controller cannot promise to understand
  messages invented after it was built;
* only the ``major.minor`` pair counts: patch, pre-release and ``.devN`` parts
  never decide (``0.17.0.dev3`` is a ``0.17`` agent);
* ``[controller] min_agent_version`` may raise the floor (never above the
  controller's own minor); empty means the previous-minor default.

A refusal travels as ``HelloAck{REJECT_CODE_PROTOCOL_VERSION}`` whose
``reject_reason`` starts with :data:`VERSION_UNSUPPORTED` + ``":"``. The
existing code is reused on purpose: agents too old to know a new reject code
are exactly the ones this check refuses, and every agent since V0.3 treats
PROTOCOL_VERSION as "stop, do not retry" (no restart loop). New agents read the
reason word and show a typed ``version_unsupported`` stop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "VERSION_UNSUPPORTED",
    "Compatibility",
    "MinorVersion",
    "VersionVerdict",
    "check_agent_version",
    "default_min_agent",
    "parse_minor",
    "reason_is_version_unsupported",
]

VERSION_UNSUPPORTED: Final = "version_unsupported"
"""The reason word of a version refusal (``reject_reason`` prefix, console
stop code, troubleshooting entry)."""

_MAX_NODE_CHARS: Final = 64
"""The node name in a refusal is capped (it comes from the agent's Hello)."""
_LEADING: Final = re.compile(r"^\s*v?(\d{1,6})\.(\d{1,6})(?:[.\-+~a-zA-Z0-9]*)\s*$")


@dataclass(frozen=True, slots=True, order=True)
class MinorVersion:
    """``major.minor`` - the only part of a version compatibility looks at."""

    major: int
    minor: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"


def parse_minor(version: str) -> MinorVersion | None:
    """``"0.17.2"`` / ``"0.17.0.dev3"`` / ``"0.17"`` -> ``0.17``; ``None`` when
    the text does not start with ``<int>.<int>`` (a hand-set ``agent_version``
    like ``"test"``)."""
    match = _LEADING.match(version)
    if match is None:
        return None
    return MinorVersion(int(match.group(1)), int(match.group(2)))


def default_min_agent(controller: MinorVersion) -> MinorVersion:
    """The previous minor (Q37). For ``X.0`` that is "any ``(X-1).*``": the
    floor becomes ``(X-1).0``."""
    if controller.minor > 0:
        return MinorVersion(controller.major, controller.minor - 1)
    return MinorVersion(max(controller.major - 1, 0), 0)


class Compatibility(StrEnum):
    SAME = "same"
    """Same minor as the controller."""
    BEHIND = "behind"
    """Older, still accepted (the dashboard shows a badge: upgrade soon)."""
    TOO_OLD = "too_old"
    TOO_NEW = "too_new"
    UNKNOWN = "unknown"
    """The agent's version text cannot be read: refused."""


@dataclass(frozen=True, slots=True)
class VersionVerdict:
    compatibility: Compatibility
    agent_version: str
    controller_version: str
    minimum: MinorVersion

    @property
    def accepted(self) -> bool:
        return self.compatibility in (Compatibility.SAME, Compatibility.BEHIND)

    def reason(self, node: str = "") -> str:
        """The ``reject_reason`` (and log/console text) of a refusal: the reason
        word, both versions and the fix. Never a secret (versions and a node
        name only)."""
        node = "".join(c for c in node if c.isprintable())[:_MAX_NODE_CHARS]
        where = f" on {node}" if node else ""
        agent, ctl = self.agent_version, self.controller_version
        if self.compatibility is Compatibility.TOO_OLD:
            text = (
                f"agent {agent} is too old for controller {ctl} (it accepts "
                f"agents {self.minimum} or newer); upgrade the MeshForge agent{where}"
            )
        elif self.compatibility is Compatibility.TOO_NEW:
            text = (
                f"agent {agent} is newer than controller {ctl}; upgrade the "
                "controller first, then restart this agent"
            )
        elif self.compatibility is Compatibility.UNKNOWN:
            text = (
                f"agent version {agent!r} cannot be read (controller {ctl} needs "
                f"<major>.<minor>); remove [agent] agent_version from the agent's "
                f"configuration{where}, or reinstall the agent"
            )
        else:
            text = f"agent {agent} is accepted by controller {ctl}"
        return f"{VERSION_UNSUPPORTED}: {text}"


def check_agent_version(
    agent_version: str,
    controller_version: str,
    *,
    min_agent_version: str = "",
) -> VersionVerdict:
    """The verdict on one agent (module docstring). ``controller_version`` must
    parse (it is our own ``__version__``); ``min_agent_version`` is the
    validated setting (empty = :func:`default_min_agent`)."""
    controller = parse_minor(controller_version)
    if controller is None:
        raise ValueError(f"controller version {controller_version!r} is not X.Y[...]")
    if min_agent_version:
        minimum = parse_minor(min_agent_version)
        if minimum is None:
            raise ValueError(f"min_agent_version {min_agent_version!r} is not X.Y")
    else:
        minimum = default_min_agent(controller)
    agent = parse_minor(agent_version)
    if agent is None:
        compat = Compatibility.UNKNOWN
    elif agent > controller:
        compat = Compatibility.TOO_NEW
    elif agent < minimum:
        compat = Compatibility.TOO_OLD
    elif agent == controller:
        compat = Compatibility.SAME
    else:
        compat = Compatibility.BEHIND
    return VersionVerdict(compat, agent_version, controller_version, minimum)


def reason_is_version_unsupported(reason: str) -> bool:
    """A ``reject_reason`` / stream detail whose reason word is ours."""
    return reason.strip().split(":", 1)[0].strip() == VERSION_UNSUPPORTED
