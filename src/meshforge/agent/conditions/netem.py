"""``netem:...`` - egress shaping toward the controller with ``tc netem`` (V0.9 G4, D217).

**Linux only, opt-in, root through one narrow sudoers rule.** The owner turns
it on with ``[agent.conditions] netem = true``, names the interface
(``netem_interface``, the one in the sudoers rule) and installs the rule
documented in ``lab/netem/README.md``; ``tc_command`` (default ``sudo -n
/usr/sbin/tc``) is the only program run. The agent advertises ``netem`` only
when a dry run (``<tc_command> qdisc show dev <interface>``) succeeds at start.
Every argument is built here from validated numbers (:meth:`~meshforge.common.
bench_conditions.Netem.tc_args`) - never from the user's text - and the only
commands are::

    <tc> qdisc show    dev <iface>
    <tc> qdisc replace dev <iface> root netem [delay Xms] [loss Y%] [rate Zmbit]
    <tc> qdisc del     dev <iface> root

**Never the wrong interface.** Loopback is refused (settings and here); before
applying, the interface that actually carries traffic to the controller
(the local address a UDP "connect" to it picks, mapped to its interface) must
be the configured one - otherwise the condition fails, nothing is shaped. An
interface whose root qdisc already is a netem the agent did not record is left
alone (someone else's shaping).

**Verified twice.** ``qdisc show`` must list our netem; with a delay, the
median TCP-connect time to the controller (:data:`RTT_SAMPLES` connects)
must grow by >= :data:`DELAY_VERIFY_FRACTION` x the delay (netem delays the
SYN). Measured: interface, RTT before/after, the qdisc line.

**Reverted always**: at the attempt's end, and - via the marker - at the next
agent start after a crash. A failed delete keeps the marker entry.

**Only our own netem is deleted** (G5 item 0.3, D244 residual): the marker
entry also stores the **expected parameters** (``netem_expect``: delay, loss,
rate); revert and the start-up sweep delete a root netem only when ``tc qdisc
show`` lists one whose parameters match them (:func:`netem_matches`, with the
rounding ``tc`` prints). A foreign netem installed after a crash, with other
parameters, survives. An entry without ``netem_expect`` (written by a G4 agent
before an upgrade) keeps the G4 rule: any root netem, when the installed bit
is set (logged).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import math
import re
import socket
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar, Final

import psutil  # type: ignore[import-untyped]

from meshforge.agent.conditions.base import (
    ConditionController,
    ConditionFailedError,
    kill_and_reap,
)
from meshforge.common.bench_conditions import Netem
from meshforge.common.logging import get_logger

__all__ = [
    "DELAY_VERIFY_FRACTION",
    "RTT_SAMPLES",
    "NetemController",
    "TcRunner",
    "delete_netem",
    "egress_interface",
    "expected_params",
    "is_loopback_interface",
    "netem_matches",
    "parse_netem_line",
    "valid_expect",
]

log = get_logger(__name__)

DELAY_VERIFY_FRACTION: Final = 0.5
RTT_SAMPLES: Final = 5
TC_TIMEOUT_S: Final = 10.0
_CONNECT_TIMEOUT_S: Final = 3.0
_IFACE_CHARS: Final = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-@:"
)


class TcError(Exception):
    pass


class TcRunner:
    """Runs the fixed-argument ``tc`` commands (``tc_command`` + args)."""

    def __init__(
        self, command: Sequence[str], *, timeout_s: float = TC_TIMEOUT_S
    ) -> None:
        if not command:
            raise ValueError("tc_command is empty")
        self.command = list(command)
        self._timeout_s = timeout_s

    async def run(self, *args: str) -> str:
        """stdout of ``tc args``; raises :class:`TcError` (exit != 0, timeout,
        not found) with stderr's first line."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.command,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise TcError(
                f"cannot run {self.command[0]}: {type(exc).__name__}"
            ) from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self._timeout_s)
        except TimeoutError:
            await kill_and_reap(proc)
            raise TcError(f"tc timed out after {self._timeout_s:g} s") from None
        except asyncio.CancelledError:
            # stopped (finish() cancels a running apply): the tc process must
            # be gone before the caller reverts, else its change lands after
            # (a second cancel cannot skip the reap: kill_and_reap holds it)
            await kill_and_reap(proc)
            raise
        if proc.returncode != 0:
            first = err.decode("utf-8", "replace").strip().splitlines()[:1]
            raise TcError(f"tc exited {proc.returncode}: {(first or [''])[0][:200]}")
        return out.decode("utf-8", "replace")

    async def show(self, iface: str) -> str:
        return await self.run("qdisc", "show", "dev", iface)

    async def replace(self, iface: str, netem: Netem) -> None:
        await self.run(
            "qdisc", "replace", "dev", iface, "root", "netem", *netem.tc_args()
        )

    async def delete(self, iface: str) -> None:
        await self.run("qdisc", "del", "dev", iface, "root")


def valid_interface_name(name: str) -> bool:
    return (
        0 < len(name) <= 15 and set(name) <= _IFACE_CHARS and not name.startswith("-")
    )


def is_loopback_interface(name: str) -> bool:
    """``lo`` or any interface that holds only loopback addresses."""
    if name == "lo" or name.startswith("lo:"):
        return True
    try:
        addresses = psutil.net_if_addrs().get(name, [])
    except Exception:  # noqa: BLE001 - unknown -> treat by name only
        return False
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for addr in addresses:
        with contextlib.suppress(ValueError):
            ips.append(ipaddress.ip_address(addr.address.split("%")[0]))
    return bool(ips) and all(ip.is_loopback for ip in ips)


def egress_interface(host: str, port: int) -> str | None:
    """The interface the kernel picks to reach ``host`` (a UDP "connect"
    sends nothing; the chosen local address is mapped to its interface)."""

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    except OSError:
        return None
    for family, kind, proto, _canon, address in infos:
        try:
            with socket.socket(family, kind, proto) as sock:
                sock.connect(address)
                local = sock.getsockname()[0]
        except OSError:
            continue
        for name, addrs in psutil.net_if_addrs().items():
            if any(a.address.split("%")[0] == local for a in addrs):
                return str(name)
    return None


def _connect_rtt_ms(host: str, port: int) -> float | None:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_S):
            pass
    except OSError:
        return None
    return (time.perf_counter() - started) * 1000.0


async def median_rtt_ms(
    host: str, port: int, samples: int = RTT_SAMPLES
) -> float | None:
    """Median TCP-connect time to ``host:port`` (None = could not connect)."""
    values: list[float] = []
    for _ in range(samples):
        value = await asyncio.to_thread(_connect_rtt_ms, host, port)
        if value is not None:
            values.append(value)
    return statistics.median(values) if values else None


type NetemParams = Mapping[str, float | None]
"""``{"delay_ms": x, "loss_pct": y, "rate_mbit": z | None}`` (0 / None = not set)."""

_NUM: Final = r"(\d+(?:\.\d+)?(?:e[+-]?\d+)?)"
_TIME_RE: Final = re.compile(_NUM + r"(ns|us|usec|ms|msec|s|sec|secs)?")
_TIME_MS: Final = {
    "ns": 1e-6,
    "us": 1e-3,
    "usec": 1e-3,
    "ms": 1.0,
    "msec": 1.0,
    "": 1.0,
    "s": 1000.0,
    "sec": 1000.0,
    "secs": 1000.0,
}
_PERCENT_RE: Final = re.compile(_NUM + "%")
_RATE_RE: Final = re.compile(_NUM + r"([kmgt]?)(bit|bps)")
_RATE_SCALE: Final = {"": 1.0, "k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}
_REL_TOLERANCE: Final = 0.01
"""``tc`` prints times with 3 significant digits (``1.23ms``) and rates in
whole units (``1500Kbit``): 1 % covers its rounding, not another netem."""
_PARAM_KEYS: Final = ("delay_ms", "loss_pct", "rate_mbit")
_VALUED_WORDS: Final = frozenset({"refcnt", "limit", "seed", "parent", "dev"})
"""Words of a ``tc qdisc show`` line (with one value) that are not netem
parameters of ours."""


def expected_params(netem: Netem) -> dict[str, float | None]:
    """What :func:`netem_matches` compares (stored in the marker entry)."""
    return {
        "delay_ms": float(netem.delay_ms),
        "loss_pct": float(netem.loss_pct),
        "rate_mbit": None if netem.rate_mbit is None else float(netem.rate_mbit),
    }


def _ms(token: str) -> float | None:
    found = _TIME_RE.fullmatch(token.lower())
    return None if found is None else float(found[1]) * _TIME_MS[found[2] or ""]


def parse_netem_line(line: str) -> dict[str, float | None] | None:
    """The delay / loss / rate of a ``tc qdisc show`` netem line (None = not
    a netem line, or one this agent never installs: with a jitter, a
    correlation, or **any word it does not know** - duplicate, reorder,
    corrupt, slot... - which our ``tc_args`` never set; fix r1 T1-2)."""
    words = line.split()
    if "netem" not in words:
        return None
    out: dict[str, float | None] = {"delay_ms": 0.0, "loss_pct": 0.0, "rate_mbit": None}
    i = first = words.index("netem") + 1
    while i < len(words):
        key = words[i]
        value = words[i + 1] if i + 1 < len(words) else ""
        after = words[i + 2] if i + 2 < len(words) else ""
        if key == "delay":
            delay = _ms(value)
            if delay is None or _ms(after) is not None:
                return None  # unreadable, or a jitter: not ours
            out["delay_ms"] = delay
            i += 2
        elif key == "loss":
            found = _PERCENT_RE.fullmatch(value)
            if found is None or _PERCENT_RE.fullmatch(after):
                return None  # a loss model / a correlation: not ours
            out["loss_pct"] = float(found[1])
            i += 2
        elif key == "rate":
            rate = _RATE_RE.fullmatch(value.lower())
            if rate is None:
                return None
            bits = float(rate[1]) * _RATE_SCALE[rate[2]]
            out["rate_mbit"] = bits * (8.0 if rate[3] == "bps" else 1.0) / 1e6
            i += 2
        elif key in _VALUED_WORDS and value:
            i += 2  # refcnt / limit / seed / parent / dev: never parameters of ours
        elif key == "root" or (i == first and key.endswith(":")):
            i += 1  # the root flag / the qdisc handle
        else:
            return None  # duplicate / reorder / corrupt / slot / ...: not ours
    return out


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _close(seen: float | None, want: float | None) -> bool:
    if not want:
        return not seen
    if seen is None:
        return False
    return abs(seen - want) <= max(_REL_TOLERANCE * abs(want), 1e-9)


def netem_matches(line: str, expect: NetemParams) -> bool:
    """``line`` is a netem with exactly the parameters ``expect`` names."""
    seen = parse_netem_line(line)
    if seen is None:
        return False
    return all(_close(seen[key], _number(expect.get(key))) for key in _PARAM_KEYS)


def valid_expect(value: object) -> bool:
    """A marker's ``netem_expect`` is well formed: exactly the three keys,
    finite non-negative numbers (``rate_mbit`` may be null), one of them set."""
    if not isinstance(value, dict) or set(value) != set(_PARAM_KEYS):
        return False
    numbers: list[float] = []
    for key in _PARAM_KEYS:
        raw = value[key]
        if raw is None and key == "rate_mbit":
            continue
        number = _number(raw)
        if number is None or number < 0:
            return False
        numbers.append(number)
    return any(numbers)


def _netem_line(show: str) -> str | None:
    for line in show.splitlines():
        if " netem " in f" {line} " and " root " in f" {line} ":
            return line.strip()[:200]
    return None


class NetemController(ConditionController):
    kind: ClassVar[str] = "netem"

    def __init__(
        self,
        netem: Netem,
        *,
        tc: TcRunner,
        interface: str,
        controller: Callable[[], tuple[str, int] | None],
        egress: Callable[[str, int], str | None] = egress_interface,
        rtt: Callable[[str, int], Any] = median_rtt_ms,
        on_change: Callable[[], None] = lambda: None,
    ) -> None:
        """``on_change``: rewrite the marker entry now (sync) - called right
        before ``tc replace`` so the entry carries ``netem_installed``."""
        super().__init__()
        self.netem = netem
        self._tc = tc
        self.interface = interface
        self._controller = controller
        self._egress = egress
        self._rtt = rtt
        self._on_change = on_change
        self._applied = False

    def marker_entry(self) -> Mapping[str, Any]:
        # ``netem_installed`` only once ``tc replace`` may have run: the sweep
        # deletes nothing for an entry without it (a refused pre-check, a
        # foreign netem, a crash before the replace)
        entry: dict[str, Any] = {
            "netem": self.interface,
            "netem_expect": expected_params(self.netem),
        }
        if self._applied:
            entry["netem_installed"] = True
        return entry

    async def apply(self) -> None:
        iface = self.interface
        if not valid_interface_name(iface) or is_loopback_interface(iface):
            raise ConditionFailedError(self.kind, f"refusing interface {iface!r}")
        target = self._controller()
        if target is None:
            raise ConditionFailedError(self.kind, "the controller's address is unknown")
        host, port = target
        egress = await asyncio.to_thread(self._egress, host, port)
        if egress != iface:
            raise ConditionFailedError(
                self.kind,
                f"traffic to the controller leaves through {egress!r}, not the "
                f"configured {iface!r}: nothing shaped",
            )
        try:
            before_show = await self._tc.show(iface)
        except TcError as exc:
            raise ConditionFailedError(
                self.kind, f"tc qdisc show failed: {exc}"
            ) from None
        if _netem_line(before_show) is not None:
            raise ConditionFailedError(
                self.kind, f"{iface} already has a netem qdisc the agent did not set"
            )
        before = await self._rtt(host, port) if self.netem.delay_ms else None
        try:
            self._applied = True  # from here on a revert is needed
            self._on_change()  # marker: netem_installed, before the change
            await self._tc.replace(iface, self.netem)
            line = _netem_line(await self._tc.show(iface))
        except TcError as exc:
            raise ConditionFailedError(self.kind, f"tc failed: {exc}") from None
        after = await self._rtt(host, port) if self.netem.delay_ms else None
        self.measured = {
            "interface": iface,
            "qdisc": line,
            "rtt_before_ms": None if before is None else round(before, 3),
            "rtt_after_ms": None if after is None else round(after, 3),
        }
        if line is None:
            raise ConditionFailedError(
                self.kind, "tc shows no netem qdisc after replace"
            )
        if self.netem.delay_ms:
            if before is None or after is None:
                raise ConditionFailedError(
                    self.kind, "the controller is not reachable to measure RTT"
                )
            added = after - before
            self.measured["rtt_added_ms"] = round(added, 3)
            if added < DELAY_VERIFY_FRACTION * self.netem.delay_ms:
                raise ConditionFailedError(
                    self.kind,
                    f"RTT grew {added:.1f} ms, expected >= "
                    f"{DELAY_VERIFY_FRACTION:g} x {self.netem.delay_ms:g} ms",
                )

    async def revert(self) -> bool:
        if not self._applied:
            return True
        cause = await delete_netem(
            self._tc, self.interface, expect=expected_params(self.netem)
        )
        if cause is not None:
            log.error(
                "agent.conditions.netem_revert_failed",
                interface=self.interface,
                cause=cause,
            )
            return False
        self._applied = False
        return True


async def delete_netem(
    tc: TcRunner, iface: str, *, expect: NetemParams | None
) -> str | None:
    """Delete the root qdisc of ``iface`` **only if it is a netem** with the
    parameters ``expect`` (G5 item 0.3; ``None`` = a G4 marker entry without
    them: any root netem); None = no netem of ours is left there (also when
    there was nothing to delete, or the netem there is someone else's), else
    why it could not. ``tc qdisc show`` runs first: a non-netem root qdisc
    (the system's default, someone's fq / htb) is never deleted, nor a netem
    with other parameters.

    Callers delete only a netem the agent itself installed (the controller's
    ``_applied`` / the marker's ``netem_installed``); a foreign netem is
    refused by :meth:`NetemController.apply`'s pre-check before anything is
    recorded as installed, so it is never reached here."""
    try:
        before = _netem_line(await tc.show(iface))
    except TcError as exc:
        return f"tc qdisc show failed: {str(exc)[:200]}"
    if before is None:
        return None  # nothing of ours to delete
    if expect is not None and not netem_matches(before, expect):
        log.warning(
            "agent.conditions.netem_not_ours", interface=iface, qdisc=before[:200]
        )
        return None  # a foreign netem (other parameters): left alone
    try:
        await tc.delete(iface)
    except TcError as exc:
        cause = str(exc)[:200]
    else:
        return None
    try:
        still = _netem_line(await tc.show(iface))
    except TcError as exc:
        return f"{cause}; and {str(exc)[:100]}"
    return None if still is None else cause
