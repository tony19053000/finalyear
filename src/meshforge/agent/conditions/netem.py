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
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar, Final

import psutil  # type: ignore[import-untyped]

from meshforge.agent.conditions.base import ConditionController, ConditionFailedError
from meshforge.common.bench_conditions import Netem
from meshforge.common.logging import get_logger

__all__ = [
    "DELAY_VERIFY_FRACTION",
    "RTT_SAMPLES",
    "NetemController",
    "TcRunner",
    "delete_netem",
    "egress_interface",
    "is_loopback_interface",
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
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise TcError(f"tc timed out after {self._timeout_s:g} s") from None
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
    ) -> None:
        super().__init__()
        self.netem = netem
        self._tc = tc
        self.interface = interface
        self._controller = controller
        self._egress = egress
        self._rtt = rtt
        self._applied = False

    def marker_entry(self) -> Mapping[str, Any]:
        return {"netem": self.interface}

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
        cause = await delete_netem(self._tc, self.interface)
        if cause is not None:
            log.error(
                "agent.conditions.netem_revert_failed",
                interface=self.interface,
                cause=cause,
            )
            return False
        self._applied = False
        return True


async def delete_netem(tc: TcRunner, iface: str) -> str | None:
    """Delete the root qdisc of ``iface``; None = no netem is left there
    (also when there was nothing to delete), else why it could not."""
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
