"""The owner's live policy on the agent (V0.7 G5, roadmap 0.7.8, D142).

``[agent.policy]`` in the configuration is the **base**. ``meshforge-agent policy
set KEY=VALUE ...`` writes the owner's live changes to ``<data_dir>/policy.toml``
(atomically, owner-only - :func:`write_overlay`); the running agent looks at that
file every ``[agent.policy] reload_interval_s`` (:meth:`LivePolicy.watch`) and,
when the **effective** policy (base + file) changed, applies it at once and tells
the controller (a ``PolicyUpdate`` report). No restart.

* The effective policy is validated as a whole (the same
  :class:`~meshforge.agent.settings.PolicySettings` model as the configuration).
  A file that cannot be read or is not valid **fails closed**: the agent accepts
  nothing (``accept_jobs`` false, no slots) until the file is fixed, logs
  ``agent.policy.invalid`` with the reason, and ``policy show`` says so. A
  missing file = the base alone.
* The file must be private (POSIX: ``0600``, owned by the agent's user, a
  regular file - :func:`~meshforge.common.private_files.read_private`): another
  local user cannot loosen this laptop's policy.
* A tightened policy blocks the **next** ``StartAttempt``; attempts already
  running are not killed (the owner can cancel them; documented in OPERATIONS).
* Two changes need a restart: an agent **started** with ``accept_jobs = false``
  probed no runtime (D45: nothing is touched on a laptop that runs no jobs), and
  one started with ``allow_unsandboxed = false`` never built the subprocess/venv
  runtimes. Turning either on live is advertised but has no runtime to run on
  until the agent restarts (``policy set`` says so; the planner sees no ``exec``
  capability meanwhile). The restart probes from the **effective** policy -
  configuration + this file (:func:`startup_policy`, fix r1 T-8) - so "policy
  set, then restart once" really enables them. Turning them off and on again on
  an agent started with them on is live, like every other change.
* A limit set in the configuration is removed live with ``KEY=none``; the file
  stores it as the string ``"none"``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import sys
import tomllib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from meshforge.agent.settings import AgentSettings, PolicySettings
from meshforge.common import owner_policy
from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.errors import ConfigError
from meshforge.common.logging import get_logger
from meshforge.common.private_files import (
    PrivateFileError,
    PrivateFileReason,
    protect_private,
    read_private,
)

__all__ = [
    "LIVE_KEYS",
    "MAX_OVERLAY_BYTES",
    "POLICY_FILE_NAME",
    "LivePolicy",
    "PolicyState",
    "format_overlay",
    "not_enforced_caps",
    "parse_assignments",
    "policy_path",
    "read_overlay",
    "startup_policy",
    "write_overlay",
]

log = get_logger(__name__)

POLICY_FILE_NAME: Final = "policy.toml"
MAX_OVERLAY_BYTES: Final = 16 * 1024
LIVE_KEYS: Final = (
    "accept_jobs",
    "accept_gpu_sessions",
    "max_cpu_jobs",
    "max_gpu_jobs",
    "max_ram_bytes",
    "max_cpu_percent",
    "max_vram_bytes",
    "allow_unsandboxed",
    "allow_network",
    "allowed_users",
)
"""Keys ``policy set`` may change (``reload_interval_s`` is configuration only)."""
_BOOLS: Final = frozenset(
    {"accept_jobs", "accept_gpu_sessions", "allow_unsandboxed", "allow_network"}
)
_INTS: Final = frozenset(
    {
        "max_cpu_jobs",
        "max_gpu_jobs",
        "max_ram_bytes",
        "max_cpu_percent",
        "max_vram_bytes",
    }
)
_UNSET: Final = "none"
"""How the live file says "no limit" for a limit the configuration sets."""
_SIZE_UNITS: Final = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
}


def not_enforced_caps(
    policy: PolicySettings,
    *,
    gpu_count: int = 0,
    platform: str = sys.platform,
    cpu_cgroup: bool = False,
) -> tuple[str, ...]:
    """Owner caps no runtime limit enforces on this node, as
    ``<field>:<why>`` (D142 item 5: say it, never claim it). Computed from the
    *effective* policy, so it follows ``policy set`` live.

    * ``max_vram_bytes:no_runtime_limit`` - always: no runtime caps VRAM
      (placement + admission only).
    * With the unsandboxed runtimes allowed (Docker enforces RAM and CPUs):
      Linux - ``max_cpu_percent:unsandboxed_linux`` unless ``cpu_cgroup``
      (V0.7 G6: the agent runs delegated and each job gets a ``cpu.max``;
      without it only a lowered priority, not a share); Linux and Windows on a GPU node -
      ``max_ram_bytes:unsandboxed_gpu_jobs`` (``RLIMIT_AS`` / a job memory
      limit would break CUDA, so GPU runs do not get one); any other OS -
      ``max_ram_bytes:unsandboxed_runtime`` and
      ``max_cpu_percent:unsandboxed_runtime`` (no mechanism at all).
    """
    caps = ["max_vram_bytes:no_runtime_limit"]
    if not policy.allow_unsandboxed:
        return tuple(caps)
    linux = platform.startswith("linux")
    if not linux and platform != "win32":
        caps += [
            "max_ram_bytes:unsandboxed_runtime",
            "max_cpu_percent:unsandboxed_runtime",
        ]
        return tuple(caps)
    if linux and not cpu_cgroup:
        caps.append("max_cpu_percent:unsandboxed_linux")
    if gpu_count > 0:
        caps.append("max_ram_bytes:unsandboxed_gpu_jobs")
    return tuple(caps)


def policy_path(settings: AgentSettings) -> Path:
    """``<data_dir>/policy.toml`` - the live overlay ``policy set`` writes."""
    return settings.data_dir / POLICY_FILE_NAME


@dataclass(frozen=True, slots=True)
class PolicyState:
    """One effective policy: the owner policy, the slots, and why it is closed."""

    policy: owner_policy.OwnerPolicy
    cpu_slots: int
    gpu_slots: int
    invalid_reason: str | None = None
    """Set = the live file is broken and the agent fails closed."""
    overlay: Mapping[str, Any] | None = None
    """The live file's keys (None = no file)."""


def read_overlay(path: Path) -> dict[str, Any] | None:
    """The live file's keys (``None`` = no file); :class:`ConfigError` when it
    cannot be read safely or is not a table of :data:`LIVE_KEYS`."""
    try:
        raw = read_private(path, limit=MAX_OVERLAY_BYTES, what="live policy file")
    except PrivateFileError as exc:
        if exc.reason is PrivateFileReason.MISSING:
            return None
        raise ConfigError(exc.message, details={"reason": exc.reason.value}) from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(
            f"the live policy file {path} is not valid TOML: {exc}",
            details={"reason": "toml"},
        ) from None
    unknown = sorted(set(data) - set(LIVE_KEYS))
    if unknown:
        raise ConfigError(
            f"the live policy file {path} has unknown keys: {', '.join(unknown)}",
            details={"reason": "unknown_key"},
        )
    return {
        key: None if key in _INTS and value == _UNSET else value
        for key, value in data.items()
    }


def effective_settings(
    base: PolicySettings, overlay: Mapping[str, Any] | None
) -> PolicySettings:
    """``base`` with ``overlay`` applied, validated as a whole (raises
    :class:`ConfigError`)."""
    if not overlay:
        return base
    try:
        return PolicySettings.model_validate({**base.model_dump(), **overlay})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "policy"
        raise ConfigError(
            f"the live policy is not valid: {where}: {first.get('msg', 'invalid')}",
            details={"reason": "invalid_value"},
        ) from None


def startup_policy(settings: AgentSettings) -> PolicySettings:
    """The effective policy the runtimes are probed from at agent start
    (configuration + ``<data_dir>/policy.toml``; V0.7 G5 fix r1, T-8).

    A live file that cannot be read or is not valid gives the configuration
    alone (``agent.policy.invalid_at_start``): what is probed only decides
    which runtimes *exist*; :class:`LivePolicy` fails the same file closed, so
    nothing runs until it is fixed - and once fixed, it applies live."""
    path = policy_path(settings)
    try:
        return effective_settings(settings.agent.policy, read_overlay(path))
    except ConfigError as exc:
        log.warning(
            "agent.policy.invalid_at_start",
            path=str(path),
            reason=exc.details.get("reason") if exc.details else None,
            error=exc.message,
        )
        return settings.agent.policy


_Stamp = tuple[int, int, int, int, int, int]
"""``(mtime_ns, size, inode, mode, uid, ctime_ns)`` of the live file."""


class LivePolicy:
    """The agent's current policy (module docstring). One event loop."""

    def __init__(
        self,
        base: PolicySettings,
        path: Path,
        *,
        gpu_count: int,
        max_concurrent_attempts: int,
        not_enforced: tuple[str, ...]
        | Callable[[PolicySettings], tuple[str, ...]] = (),
    ) -> None:
        """``not_enforced``: the caps no runtime enforces - fixed, or computed
        from each effective policy (:func:`not_enforced_caps`)."""
        self._base = base
        self.path = path
        self._gpu_count = gpu_count
        self._max_attempts = max_concurrent_attempts
        self._not_enforced = not_enforced
        self._stamp: _Stamp | bool | None = False
        self.revision = 0
        self.changes = 0
        self.state = self._compute()

    @property
    def policy(self) -> owner_policy.OwnerPolicy:
        return self.state.policy

    @property
    def slots(self) -> tuple[int, int]:
        return self.state.cpu_slots, self.state.gpu_slots

    def _file_stamp(self) -> _Stamp | None:
        """What a reload compares: content (mtime, size, inode) **and** the
        file's mode, owner and ctime (fix r1, T-6) - a ``chmod``/``chown`` that
        leaves the content alone must be re-checked by :func:`read_private`."""
        try:
            info = os.stat(self.path, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError:
            return (-1, -1, -1, -1, -1, -1)  # unreadable: re-read (fail closed) once
        return (
            info.st_mtime_ns,
            info.st_size,
            info.st_ino,
            info.st_mode,
            info.st_uid,
            info.st_ctime_ns,
        )

    def _compute(self) -> PolicyState:
        self._stamp = self._file_stamp()
        try:
            overlay = read_overlay(self.path)
            effective = effective_settings(self._base, overlay)
            not_enforced = (
                self._not_enforced(effective)
                if callable(self._not_enforced)
                else self._not_enforced
            )
            policy = effective.to_policy(
                not_enforced=not_enforced, revision=self.revision
            )
        except (ConfigError, owner_policy.PolicyError) as exc:
            log.error(  # noqa: TRY400 - typed reason, no traceback
                "agent.policy.invalid",
                path=str(self.path),
                reason=exc.details.get("reason") if exc.details else None,
                error=exc.message,
            )
            return PolicyState(
                policy=owner_policy.DENY_ALL,
                cpu_slots=0,
                gpu_slots=0,
                invalid_reason=exc.message,
            )
        cpu, gpu = effective.slots(
            gpu_count=self._gpu_count, max_concurrent_attempts=self._max_attempts
        )
        if effective.max_cpu_jobs + (effective.max_gpu_jobs or 0) > self._max_attempts:
            message = (
                "the live policy's max_cpu_jobs + max_gpu_jobs exceed "
                "[agent.execution] max_concurrent_attempts"
            )
            log.error("agent.policy.invalid", path=str(self.path), error=message)
            return PolicyState(
                policy=owner_policy.DENY_ALL,
                cpu_slots=0,
                gpu_slots=0,
                invalid_reason=message,
            )
        return PolicyState(policy=policy, cpu_slots=cpu, gpu_slots=gpu, overlay=overlay)

    def reload(self) -> bool:
        """Re-read the live file if it changed; True when the effective policy
        (or slots, or validity) changed."""
        if self._file_stamp() == self._stamp:
            return False
        before = self.state
        state = self._compute()
        if (
            state.policy == before.policy
            and state.cpu_slots == before.cpu_slots
            and state.gpu_slots == before.gpu_slots
            and state.invalid_reason == before.invalid_reason
        ):
            self.state = state
            return False
        self.revision += 1
        self.changes += 1
        if state.invalid_reason is None:
            state = dataclasses.replace(
                state, policy=dataclasses.replace(state.policy, revision=self.revision)
            )
        self.state = state
        log.info(
            "agent.policy.changed",
            revision=self.revision,
            accept_jobs=state.policy.accept_jobs,
            cpu_slots=state.cpu_slots,
            gpu_slots=state.gpu_slots,
            invalid=state.invalid_reason is not None,
        )
        return True

    async def watch(
        self,
        on_change: Callable[[PolicyState], Awaitable[None]],
        *,
        interval_s: float,
    ) -> None:
        """Poll the live file; call ``on_change`` after each effective change.
        Supervised: a failing callback is logged, the watch goes on."""
        while True:
            await asyncio.sleep(interval_s)
            try:
                changed = await asyncio.to_thread(self.reload)
                if changed:
                    await on_change(self.state)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("agent.policy.watch_failed", path=str(self.path))


def _parse_bool(key: str, text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in ("true", "yes", "on", "1"):
        return True
    if lowered in ("false", "no", "off", "0"):
        return False
    raise ConfigError(f"{key} must be true or false, got {text!r}")


def _parse_int(key: str, text: str) -> int | None:
    value = text.strip().lower().replace("_", "")
    if value in ("", "none", "unset"):
        return None
    number = value.rstrip("abcdefghijklmnopqrstuvwxyz")
    unit = value[len(number) :]
    if key not in ("max_ram_bytes", "max_vram_bytes") and unit:
        raise ConfigError(f"{key} must be a whole number, got {text!r}")
    if unit not in _SIZE_UNITS or not number:
        raise ConfigError(f"{key}: cannot read {text!r} (e.g. 8GiB, 512MiB, 1000000)")
    try:
        amount = float(number)
    except ValueError:
        raise ConfigError(f"{key}: cannot read {text!r}") from None
    result = amount * _SIZE_UNITS[unit]
    if result != int(result) or result < 0:
        raise ConfigError(f"{key} must be a whole, non-negative number of bytes")
    return int(result)


def parse_assignments(items: list[str]) -> dict[str, Any]:
    """``["accept_jobs=true", "max_ram_bytes=8GiB", "allowed_users=a,b"]`` ->
    typed values (``none`` unsets a limit; an empty ``allowed_users=`` = all)."""
    out: dict[str, Any] = {}
    for item in items:
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep:
            raise ConfigError(f"expected KEY=VALUE, got {item!r}")
        if key not in LIVE_KEYS:
            raise ConfigError(
                f"unknown policy key {key!r}; one of: {', '.join(LIVE_KEYS)}"
            )
        if key in _BOOLS:
            out[key] = _parse_bool(key, value)
        elif key in _INTS:
            out[key] = _parse_int(key, value)
        else:
            out[key] = [n.strip() for n in value.split(",") if n.strip()]
    return out


def format_overlay(values: Mapping[str, Any]) -> bytes:
    """The live file's TOML: flat keys; a removed limit (``None``) is written as
    the string ``"none"``."""
    lines = ["# Written by `meshforge-agent policy set`; edit with that command.\n"]
    for key in LIVE_KEYS:
        if key not in values:
            continue
        value = values[key]
        if value is None:
            lines.append(f'{key} = "{_UNSET}"\n')
        elif isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}\n")
        elif isinstance(value, int):
            lines.append(f"{key} = {value}\n")
        elif isinstance(value, list | tuple):
            names = ", ".join(f'"{n}"' for n in value)
            lines.append(f"{key} = [{names}]\n")
    return "".join(lines).encode("utf-8")


def write_overlay(path: Path, values: Mapping[str, Any]) -> None:
    """Atomically replace the live file with ``values`` (owner-only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, format_overlay(values), mode=0o600)
    protect_private(path)


def clear_overlay(path: Path) -> bool:
    """Remove the live file (back to the configuration); True if one existed."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
        return True
    return False
