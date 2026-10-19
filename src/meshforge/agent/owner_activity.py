"""Is the laptop's owner using it right now? (V0.9 G5, roadmap 0.9.7, D219/D246).

Benchmark campaigns run unattended on the team's own laptops; they must not
annoy the owner. :class:`OwnerActivity` samples two things every
``[agent.bench] poll_s`` and nothing else:

* **input idle time** - how long since the last keyboard/mouse input, from the
  OS: Windows ``GetLastInputInfo`` (:class:`WindowsIdle`), Linux logind's
  ``IdleHint`` / ``IdleSinceHintMonotonic`` of the graphical sessions through
  the ``loginctl`` command (:class:`LogindIdle`; no D-Bus library). Where
  neither answers - a headless box, no desktop setting the hint, macOS - the
  source is ``unknown`` and **only the CPU rule applies** (never guessed);
* **CPU of processes that are not the fabric's** - per-process CPU time
  (psutil), excluding the agent's own process tree (its subprocess/venv jobs
  and load generators are its children), container shims' trees (Docker
  jobs) and, while this agent runs an attempt, the Windows WSL VM process
  (Docker Desktop jobs run there). Idle/system pseudo-processes are skipped.

Never read: keystrokes, window titles, file or process contents. Only the
resulting bit, a short reason (``idle`` / ``cpu``) and the idle source leave
the node (the heartbeat, :mod:`meshforge.agent.session.client`).

**The rule** (:class:`ActivityRule`, pure, injectable clock): active when idle
< ``idle_threshold_s`` (0 = ignore idle), or non-fabric CPU >
``foreground_cpu_percent`` for ``sustain_s``; once active it clears only after
``clear_after_s`` without either (hysteresis), so a pause in typing does not
flap benchmarks on and off.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Final, Protocol

import psutil  # type: ignore[import-untyped]

from meshforge.agent.settings import BenchSettings
from meshforge.common.logging import get_logger

__all__ = [
    "ActivityRule",
    "ActivityState",
    "CpuSampler",
    "IdleReader",
    "LogindIdle",
    "OwnerActivity",
    "UnknownIdle",
    "WindowsIdle",
    "default_idle_reader",
]

log = get_logger(__name__)

LOGINCTL_TIMEOUT_S: Final = 2.0
SESSION_CACHE_S: Final = 30.0
_SHIM_PREFIX: Final = "containerd-shim"
_WSL_VM_NAMES: Final = frozenset({"vmmem", "vmmemwsl", "vmmem.exe"})
_SKIP_PIDS: Final = frozenset({0, 4})
"""Windows' System Idle Process (its CPU time *is* idle time) and System."""


class IdleReader(Protocol):
    name: str

    def idle_s(self) -> float | None:
        """Seconds since the last input; None = could not tell now."""


class UnknownIdle:
    """No idle source on this OS / machine: the CPU rule only."""

    name = "unknown"

    def idle_s(self) -> float | None:
        return None


class WindowsIdle:  # pragma: no cover - Windows
    """``GetLastInputInfo`` - the interactive session's last input tick."""

    name = "windows"

    def idle_s(self) -> float | None:
        import ctypes
        from ctypes import wintypes

        class _LastInput(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = _LastInput()
        info.cbSize = ctypes.sizeof(info)
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        ticks = int(kernel32.GetTickCount()) & 0xFFFFFFFF
        return ((ticks - int(info.dwTime)) & 0xFFFFFFFF) / 1000.0


type Runner = Callable[[list[str]], str | None]
"""Runs one ``loginctl`` command: stdout, or None (failed / timed out)."""


def _run_loginctl(args: list[str]) -> str | None:
    try:
        done = subprocess.run(  # noqa: S603 - our own arguments
            ["loginctl", *args],  # noqa: S607 - logind's tool, from PATH
            capture_output=True,
            text=True,
            timeout=LOGINCTL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _monotonic_us() -> float:
    return time.clock_gettime(time.CLOCK_MONOTONIC) * 1e6  # type: ignore[attr-defined,unused-ignore]


class LogindIdle:
    """logind's idle hint of the **graphical** sessions (x11 / wayland / mir),
    read with ``loginctl``. The desktop sets ``IdleHint`` after its own idle
    delay; ``IdleSinceHintMonotonic`` is when it last changed. Idle = the
    smallest over the active graphical sessions (``IdleHint=no`` = 0 s). No
    graphical session, or a hint never set (``IdleSinceHintMonotonic=0``) =
    None: the CPU rule decides."""

    name = "logind"

    def __init__(
        self,
        run: Runner = _run_loginctl,
        *,
        monotonic_us: Callable[[], float] = _monotonic_us,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._run = run
        self._now_us = monotonic_us
        self._clock = clock
        self._sessions: list[str] = []
        self._listed_at: float | None = None

    def _graphical_sessions(self) -> list[str]:
        now = self._clock()
        if self._listed_at is not None and now - self._listed_at < SESSION_CACHE_S:
            return self._sessions
        out = self._run(["list-sessions", "--no-legend", "--no-pager"])
        self._listed_at = now
        ids = (
            []
            if out is None
            else [ln.split()[0] for ln in out.splitlines() if ln.split()]
        )
        found: list[str] = []
        for sid in ids[:16]:
            if not (sid.isascii() and sid.isalnum() and len(sid) <= 32):
                continue
            props = self._props(sid, ["Type", "Active"])
            if (
                props.get("Type") in ("x11", "wayland", "mir")
                and props.get("Active") == "yes"
            ):
                found.append(sid)
        self._sessions = found
        return found

    def _props(self, sid: str, names: Iterable[str]) -> dict[str, str]:
        args = ["show-session", sid, "--no-pager"]
        for name in names:
            args += ["-p", name]
        out = self._run(args) or ""
        props: dict[str, str] = {}
        for line in out.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                props[key.strip()] = value.strip()
        return props

    def idle_s(self) -> float | None:
        idles: list[float] = []
        for sid in self._graphical_sessions():
            props = self._props(sid, ["IdleHint", "IdleSinceHintMonotonic"])
            try:
                since = int(props.get("IdleSinceHintMonotonic", "0"))
            except ValueError:
                continue
            if since <= 0:
                continue  # the desktop never set a hint: not a measurement
            if props.get("IdleHint") == "yes":
                idles.append(max(0.0, (self._now_us() - since) / 1e6))
            elif props.get("IdleHint") == "no":
                idles.append(0.0)
        return min(idles) if idles else None


def default_idle_reader(platform: str = sys.platform) -> IdleReader:
    """The idle source of this OS (see module docstring)."""
    if platform.startswith("win"):
        return WindowsIdle()  # pragma: no cover - Windows
    if platform.startswith("linux"):
        reader = LogindIdle()
        if _run_loginctl(["--version"]) is not None:
            return reader
    return UnknownIdle()


class CpuSampler:
    """Percent of the whole machine used by non-fabric processes since the
    previous call (None on the first call: no window yet)."""

    def __init__(
        self,
        *,
        root_pid: int | None = None,
        attempts_running: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root = os.getpid() if root_pid is None else root_pid
        self._busy = attempts_running
        self._clock = clock
        self._cpus = psutil.cpu_count() or 1
        self._last: dict[tuple[int, float], float] = {}
        self._at: float | None = None

    def _fabric(self, table: dict[int, tuple[int, str]]) -> set[int]:
        roots = {self._root}
        busy = self._busy()
        for pid, (_ppid, name) in table.items():
            lowered = name.lower()
            if lowered.startswith(_SHIM_PREFIX) or (busy and lowered in _WSL_VM_NAMES):
                roots.add(pid)
        children: dict[int, list[int]] = {}
        for pid, (ppid, _name) in table.items():
            children.setdefault(ppid, []).append(pid)
        out: set[int] = set()
        stack = list(roots)
        while stack:
            pid = stack.pop()
            if pid in out:
                continue
            out.add(pid)
            stack.extend(children.get(pid, ()))
        return out

    def sample(self) -> float | None:
        table: dict[int, tuple[int, str]] = {}
        times: dict[tuple[int, float], float] = {}
        for proc in psutil.process_iter(["pid", "ppid", "name", "create_time"]):
            with contextlib.suppress(psutil.Error, TypeError, ValueError):
                info = proc.info
                pid = int(info["pid"])
                if pid in _SKIP_PIDS:
                    continue
                table[pid] = (int(info["ppid"] or 0), str(info["name"] or ""))
                cpu = proc.cpu_times()
                times[(pid, float(info["create_time"] or 0.0))] = float(
                    cpu.user
                ) + float(cpu.system)
        fabric = self._fabric(table)
        now = self._clock()
        used = 0.0
        for key, total in times.items():
            if key[0] in fabric:
                continue
            before = self._last.get(key)
            if before is not None:
                used += max(0.0, total - before)
        previous, self._at, self._last = self._at, now, times
        if previous is None or now <= previous:
            return None
        return min(100.0, 100.0 * used / ((now - previous) * self._cpus))


@dataclass(frozen=True, slots=True)
class ActivityState:
    active: bool = False
    reason: str = ""
    """``idle`` / ``cpu`` / ``idle+cpu`` (the latest evidence), "" when clear."""
    idle_source: str = ""


class ActivityRule:
    """The hysteresis rule of the module docstring (pure)."""

    def __init__(self, settings: BenchSettings) -> None:
        self._s = settings
        self._cpu_since: float | None = None
        self._last_seen: float | None = None
        self.active = False
        self.reason = ""

    def observe(
        self, now: float, *, idle_s: float | None, cpu_percent: float | None
    ) -> bool:
        """Feed one sample; True when :attr:`active` changed."""
        s = self._s
        idle_hit = (
            idle_s is not None
            and s.idle_threshold_s > 0
            and idle_s < s.idle_threshold_s
        )
        if cpu_percent is not None and cpu_percent > s.foreground_cpu_percent:
            if self._cpu_since is None:
                self._cpu_since = now
        elif cpu_percent is not None:
            self._cpu_since = None
        cpu_hit = self._cpu_since is not None and now - self._cpu_since >= s.sustain_s
        before = self.active
        if idle_hit or cpu_hit:
            self._last_seen = now
            self.active = True
            self.reason = (
                "idle+cpu" if idle_hit and cpu_hit else ("idle" if idle_hit else "cpu")
            )
        elif self.active and (
            self._last_seen is None or now - self._last_seen >= s.clear_after_s
        ):
            self.active = False
            self.reason = ""
        return self.active != before


class OwnerActivity:
    """Samples, applies :class:`ActivityRule`, calls ``on_change`` on every
    flip. One event loop; the sampling runs in a worker thread."""

    def __init__(
        self,
        settings: BenchSettings,
        *,
        idle: IdleReader | None = None,
        cpu: CpuSampler | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_change: Callable[[ActivityState], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self._idle = idle or UnknownIdle()
        self._cpu = cpu or CpuSampler(clock=clock)
        self._clock = clock
        self._rule = ActivityRule(settings)
        self.on_change = on_change
        self.on_poll: Callable[[ActivityState], Awaitable[None]] | None = None
        """Called after every sample (the guard's pending-retry hook)."""
        self.poll_s = settings.poll_s
        self.state = ActivityState(idle_source=self._idle.name)

    def _sample(self) -> tuple[float | None, float | None]:
        idle: float | None
        try:
            idle = self._idle.idle_s()
        except Exception:  # noqa: BLE001 - an unreadable source is "unknown now"
            idle = None
        return idle, self._cpu.sample()

    async def step(self) -> bool:
        """One sample (tests drive this); True when the state flipped."""
        idle, cpu = await asyncio.to_thread(self._sample)
        changed = self._rule.observe(self._clock(), idle_s=idle, cpu_percent=cpu)
        self.state = ActivityState(
            active=self._rule.active,
            reason=self._rule.reason,
            idle_source=self._idle.name,
        )
        if changed:
            log.info(
                "agent.bench.owner_activity",
                owner_active=self.state.active,
                reason=self.state.reason or None,
                idle_source=self._idle.name,
            )
            if self.on_change is not None:
                await self.on_change(self.state)
        if self.on_poll is not None:  # fix r2 (D251 B1): retries ride on polls
            await self.on_poll(self.state)
        return changed

    async def run(self) -> None:
        """Until cancelled; supervised (a failing sample is logged, never fatal)."""
        if self._idle.name == "unknown":
            log.warning("agent.bench.idle_source_unknown", rule="cpu_only")
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("agent.bench.owner_activity_failed")
            await asyncio.sleep(self._settings.poll_s)
