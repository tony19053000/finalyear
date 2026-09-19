"""What we measure besides wall time: CPU seconds, peak RSS, server-side stats.

* **CPU** - ``time.process_time()`` covers every thread of the process,
  including gRPC's C-core threads, which a per-thread clock would miss. The
  client runs one transfer at a time, so a delta over the transfer window is
  that transfer's CPU cost. Parallel HTTP range parts are bracketed by the
  *transfer* (first part starts, last part ends), not per part, so overlapping
  threads are counted once.
* **Peak RSS** - ``resource.getrusage`` on Linux/macOS, ``GetProcessMemoryInfo``
  on native Windows, ``None`` where neither works. It is a process-wide *peak*,
  so read the ``max`` column of the report: it proves nothing buffered the whole
  payload in RAM.
* :class:`TransferLog` - the server's view of each transfer, fetched afterwards
  by the client over the control connection (``stats``).
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Final

MAX_TRANSFERS: Final = 256
_WAIT_SLICE_S: Final = 0.05


def cpu_seconds() -> float:
    """Process-wide CPU time (user + system), all threads."""
    return time.process_time()


def _peak_rss_windows() -> int | None:  # pragma: no cover - Windows only
    class _Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_uint32),
            ("PageFaultCount", ctypes.c_uint32),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    # getattr: ctypes.WinDLL only exists on Windows, and mypy checks this file
    # with the Linux stubs.
    windll = getattr(ctypes, "WinDLL", None)
    if windll is None:
        return None
    try:
        kernel32 = windll("kernel32", use_last_error=True)
        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        handle = kernel32.GetCurrentProcess()
        ok = kernel32.K32GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        )
    except (OSError, AttributeError, ValueError):
        return None
    return int(counters.PeakWorkingSetSize) if ok else None


def peak_rss_bytes() -> int | None:
    """Peak resident set size of this process, or ``None`` if unavailable."""
    if sys.platform == "win32":
        return _peak_rss_windows()
    try:
        import resource
    except ImportError:  # pragma: no cover - POSIX always has it
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS/BSD bytes.
    return int(peak) * (1024 if sys.platform.startswith("linux") else 1)


def peak_rss_mib() -> float | None:
    peak = peak_rss_bytes()
    return None if peak is None else peak / (1024 * 1024)


@dataclass(slots=True)
class Transfer:
    """One transfer as the server saw it (one or more parts)."""

    transfer_id: str
    transport: str
    t_first: float | None = None
    t_last: float | None = None
    cpu_start: float | None = None
    cpu_end: float | None = None
    bytes: int = 0
    parts: int = 0
    open_parts: int = 0
    crc_ok: bool | None = None
    error: str | None = None

    @property
    def seconds(self) -> float:
        if self.t_first is None or self.t_last is None:
            return 0.0
        return max(0.0, self.t_last - self.t_first)

    @property
    def cpu_s(self) -> float:
        if self.cpu_start is None or self.cpu_end is None:
            return 0.0
        return max(0.0, self.cpu_end - self.cpu_start)

    def to_json(self) -> dict[str, Any]:
        return {
            "transfer_id": self.transfer_id,
            "transport": self.transport,
            "bytes": self.bytes,
            "seconds": self.seconds,
            "cpu_s": self.cpu_s,
            "parts": self.parts,
            "crc_ok": self.crc_ok,
            "error": self.error,
            "rss_peak_mib": peak_rss_mib(),
        }


class TransferLog:
    """Thread-safe, bounded record of recent transfers (all transports).

    A handler calls :meth:`begin` when it starts a part and :meth:`end` when that
    part finishes; :meth:`wait_for` lets the client's ``stats`` request block
    (bounded) until every expected part has been recorded.
    """

    def __init__(self, max_transfers: int = MAX_TRANSFERS) -> None:
        self._max = max_transfers
        self._cond = threading.Condition()
        self._items: OrderedDict[str, Transfer] = OrderedDict()
        self.total_bytes = 0
        self.total_transfers = 0

    def begin(self, transfer_id: str, transport: str) -> Transfer:
        now, cpu = time.perf_counter(), cpu_seconds()
        with self._cond:
            item = self._items.get(transfer_id)
            if item is None:
                item = Transfer(transfer_id, transport)
                self._items[transfer_id] = item
                self.total_transfers += 1
            self._items.move_to_end(transfer_id)
            if item.t_first is None:
                item.t_first, item.cpu_start = now, cpu
            item.open_parts += 1
            while len(self._items) > self._max:
                self._items.popitem(last=False)
            self._cond.notify_all()
        return item

    def end(
        self,
        transfer_id: str,
        *,
        num_bytes: int,
        crc_ok: bool | None = None,
        error: str | None = None,
    ) -> None:
        now, cpu = time.perf_counter(), cpu_seconds()
        with self._cond:
            item = self._items.get(transfer_id)
            if item is None:  # evicted while running; nothing to report
                return
            item.t_last = now if item.t_last is None else max(item.t_last, now)
            item.cpu_end = cpu if item.cpu_end is None else max(item.cpu_end, cpu)
            item.bytes += num_bytes
            item.parts += 1
            item.open_parts = max(0, item.open_parts - 1)
            if crc_ok is not None:
                item.crc_ok = (
                    crc_ok if item.crc_ok is None else (item.crc_ok and crc_ok)
                )
            if error is not None:
                item.error = error if item.error is None else f"{item.error}; {error}"
            self.total_bytes += num_bytes
            self._cond.notify_all()

    def get(self, transfer_id: str) -> Transfer | None:
        with self._cond:
            return self._items.get(transfer_id)

    def wait_for(
        self, transfer_id: str, parts: int, timeout_s: float
    ) -> Transfer | None:
        """The transfer once ``parts`` parts are finished, else what exists (or None)."""
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while True:
                item = self._items.get(transfer_id)
                if item is not None and item.parts >= parts and item.open_parts == 0:
                    return item
                left = deadline - time.monotonic()
                if left <= 0:
                    return item
                self._cond.wait(timeout=min(left, _WAIT_SLICE_S))


@dataclass(slots=True)
class ClientMeter:
    """CPU + wall bracket around one client-side transfer."""

    t_start: float = field(default_factory=time.perf_counter)
    cpu_start: float = field(default_factory=cpu_seconds)
    t_first_byte: float | None = None
    t_end: float | None = None
    cpu_end: float | None = None

    def first_byte(self) -> None:
        if self.t_first_byte is None:
            self.t_first_byte = time.perf_counter()

    def stop(self) -> None:
        self.t_end = time.perf_counter()
        self.cpu_end = cpu_seconds()

    @property
    def wall_s(self) -> float:
        end = self.t_end if self.t_end is not None else time.perf_counter()
        return max(end - self.t_start, 1e-9)

    @property
    def transfer_s(self) -> float:
        """First byte to last byte (excludes connect/request latency)."""
        end = self.t_end if self.t_end is not None else time.perf_counter()
        start = self.t_first_byte if self.t_first_byte is not None else self.t_start
        return max(end - start, 1e-9)

    @property
    def ttfb_s(self) -> float | None:
        """Request sent -> first byte seen (``None`` if nothing arrived)."""
        if self.t_first_byte is None:
            return None
        return max(0.0, self.t_first_byte - self.t_start)

    @property
    def cpu_s(self) -> float:
        end = self.cpu_end if self.cpu_end is not None else cpu_seconds()
        return max(0.0, end - self.cpu_start)
