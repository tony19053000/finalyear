"""The load generator child process (V0.9 G4, D217): ``python -m meshforge.agent.conditions.burn``.

``burn cpu --deadline-s S --parent PID`` spins one core; ``burn gpu --percent P
--deadline-s S --parent PID`` keeps the CUDA device it sees busy P % of every
100 ms period with a matmul loop (torch, imported here only). Both run at the
**lowest OS priority** (``nice 19`` / ``IDLE_PRIORITY_CLASS``): they compete
with the benchmark job like a background program would, never ahead of it.

**Bounded, always.** A burner exits by itself at its deadline (the attempt's
time limit + slack, :mod:`.load`) and as soon as its parent agent is gone
(polled every :data:`PARENT_POLL_S`), so a crashed or killed agent never
leaves a laptop spinning; the agent's start-up sweep kills any survivor it
recorded (:mod:`.marker`). Exit codes: 0 deadline / parent gone, 2 bad
arguments, 3 the GPU cannot be used (no torch / no CUDA).

Imports only the standard library (and torch for ``gpu``) - nothing from the
agent - so it starts fast and cannot drag the agent's state along.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Final

PARENT_POLL_S: Final = 0.5
PERIOD_S: Final = 0.1
EXIT_BAD_ARGS: Final = 2
EXIT_NO_GPU: Final = 3


def lower_priority() -> None:
    """Lowest scheduling priority this OS offers a normal user."""
    try:
        if os.name == "nt":  # pragma: no cover - Windows
            import ctypes

            idle_priority_class = 0x00000040
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), idle_priority_class)
        else:
            os.nice(19)
    except OSError:  # pragma: no cover - already as low as allowed
        pass


def parent_alive(pid: int) -> bool:
    if os.name == "nt":  # pragma: no cover - Windows
        import ctypes

        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)  # type: ignore[attr-defined]
        if not handle:
            return False
        try:
            wait_timeout = 0x102
            return bool(
                ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == wait_timeout  # type: ignore[attr-defined]
            )
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    return os.getppid() == pid


def burn_cpu(deadline: float, parent: int) -> int:
    next_check = 0.0
    value = 0
    while True:
        now = time.monotonic()
        if now >= deadline:
            return 0
        if now >= next_check:
            if not parent_alive(parent):
                return 0
            next_check = now + PARENT_POLL_S
        for _ in range(20_000):  # ~1 ms of pure-Python work between clock reads
            value = (value * 1103515245 + 12345) & 0xFFFFFFFF


def burn_gpu(
    percent: int, deadline: float, parent: int
) -> int:  # pragma: no cover - CUDA
    try:
        import torch
    except ImportError:
        return EXIT_NO_GPU
    if not torch.cuda.is_available():
        return EXIT_NO_GPU
    device = torch.device("cuda", 0)
    a = torch.randn((2048, 2048), device=device)
    b = torch.randn((2048, 2048), device=device)
    busy = PERIOD_S * percent / 100.0
    next_check = 0.0
    while True:
        start = time.monotonic()
        if start >= deadline:
            return 0
        if start >= next_check:
            if not parent_alive(parent):
                return 0
            next_check = start + PARENT_POLL_S
        while time.monotonic() - start < busy:
            torch.matmul(a, b)
            torch.cuda.synchronize(device)
        rest = PERIOD_S - (time.monotonic() - start)
        if rest > 0:
            time.sleep(rest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="meshforge-burn", add_help=False)
    parser.add_argument("mode", choices=("cpu", "gpu"))
    parser.add_argument("--deadline-s", type=float, required=True)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--percent", type=int, default=100)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return EXIT_BAD_ARGS
    if not 0 < args.deadline_s <= 86_400 or not 1 <= args.percent <= 100:
        return EXIT_BAD_ARGS
    lower_priority()
    deadline = time.monotonic() + args.deadline_s
    if args.mode == "cpu":
        return burn_cpu(deadline, args.parent)
    return burn_gpu(args.percent, deadline, args.parent)


if __name__ == "__main__":
    sys.exit(main())
