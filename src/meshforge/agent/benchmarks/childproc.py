"""The benchmark child, driven over asyncio pipes (brief §5g D32b; split out of
``runner.py`` by roadmap 0.4.17 item 3).

Spawning, tree-kill and parent-death cleanup are :mod:`meshforge.agent.process`
(shared with the subprocess execution runtime); this module adds only what is
specific to the score child: its thread/device environment, the lowered priority it
reports itself (``--nice``), and spike S7's one-JSON-object-per-line protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Callable, Mapping
from typing import Any, Final

from meshforge.agent.benchmarks.model import MetricReason
from meshforge.agent.benchmarks.plan import PCI_BUS_ID
from meshforge.agent.hardware.probe import sanitize_reason
from meshforge.agent.process import NICE, ProcessHandle, SpawnError, spawn

__all__ = [
    "CHILD_MODULE",
    "THREAD_VARS",
    "ChildError",
    "ChildProcess",
    "thread_env",
]

CHILD_MODULE: Final = "meshforge.agent.benchmarks.child"
THREAD_VARS: Final = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)

_LINE_LIMIT: Final = 1 << 20
_STDERR_TAIL: Final = 2000
_EXIT_GRACE_S: Final = 2.0
_REAP_S: Final = 2.0
_MIN_TIMEOUT_S: Final = 0.05


def thread_env(threads: int) -> dict[str, str]:
    """Environment variables that pin a child's BLAS/OpenMP thread count."""
    if threads < 1:
        raise ValueError("threads must be >= 1")
    return dict.fromkeys(THREAD_VARS, str(threads))


class ChildError(Exception):
    """A request to the child did not produce an answer.

    ``fatal`` means the child is gone (killed or crashed) and must be replaced;
    otherwise the child answered ``ok: false`` and ``code`` is its failure type.
    """

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        fatal: bool,
        reason: MetricReason | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.fatal = fatal
        self.reason = reason


class ChildProcess:
    """One benchmark child, driven over asyncio pipes. Owned by one task."""

    def __init__(
        self,
        *,
        threads: int,
        lower_priority: bool,
        python: str,
        extra_env: Mapping[str, str],
        on_spawn: Callable[[int], None],
    ) -> None:
        self.threads = threads
        self._lower_priority = lower_priority
        self._python = python
        self._extra_env = dict(extra_env)
        self._on_spawn = on_spawn
        self._handle: ProcessHandle | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr: list[str] = []
        self.dead = False

    @property
    def pid(self) -> int | None:
        return self._handle.pid if self._handle is not None else None

    @property
    def _proc(self) -> asyncio.subprocess.Process | None:
        return self._handle.proc if self._handle is not None else None

    async def start(self) -> None:
        env = dict(os.environ)
        env.update(thread_env(self.threads))
        env["CUDA_DEVICE_ORDER"] = PCI_BUS_ID
        env.update(self._extra_env)
        argv = [self._python, "-I", "-u", "-m", CHILD_MODULE]
        if self._lower_priority:
            # The child lowers (and reports) its own niceness on POSIX; Windows
            # gets BELOW_NORMAL_PRIORITY_CLASS from spawn() and records the flag.
            argv += ["--nice", str(NICE)]
        try:
            self._handle = await spawn(
                argv,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                limit=_LINE_LIMIT,
                lower_priority=self._lower_priority and sys.platform == "win32",
                on_spawn=self._on_spawn,
            )
        except SpawnError as exc:
            self.dead = True
            raise ChildError(
                "child_not_started",
                f"cannot start the benchmark child: {exc.reason}",
                fatal=True,
                reason=MetricReason.CHILD_NOT_STARTED,
            ) from exc
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"bench-child-stderr:{self._handle.pid}"
        )

    async def _drain_stderr(self) -> None:
        """Keep the stderr pipe empty (a chatty library must not block the child)."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        with contextlib.suppress(OSError, ValueError, asyncio.LimitOverrunError):
            while chunk := await proc.stderr.read(4096):
                self._stderr.append(chunk.decode("utf-8", "replace"))
                text = "".join(self._stderr)
                self._stderr = [text[-_STDERR_TAIL:]]

    @property
    def stderr_tail(self) -> str:
        return sanitize_reason("".join(self._stderr)[-400:])

    async def request(
        self, payload: Mapping[str, Any], timeout_s: float
    ) -> dict[str, Any]:
        """One request/answer. Raises :class:`ChildError`; kills the child if fatal."""
        proc = self._proc
        if proc is None or self.dead or proc.stdin is None or proc.stdout is None:
            raise ChildError(
                "child_gone",
                "the child is not running",
                fatal=True,
                reason=MetricReason.CHILD_CRASHED,
            )
        op = str(payload.get("op"))
        try:
            proc.stdin.write((json.dumps(payload, allow_nan=False) + "\n").encode())
            await asyncio.wait_for(proc.stdin.drain(), max(timeout_s, _MIN_TIMEOUT_S))
            line = await asyncio.wait_for(
                proc.stdout.readline(), max(timeout_s, _MIN_TIMEOUT_S)
            )
        except TimeoutError:
            await self.kill()
            raise ChildError(
                "timeout",
                f"no answer to {op!r} within {timeout_s:.3g} s; the child was killed",
                fatal=True,
                reason=MetricReason.TIMEOUT,
            ) from None
        except (OSError, ValueError, asyncio.LimitOverrunError) as exc:
            await self.kill()
            raise ChildError(
                "child_crashed",
                f"lost the child while sending {op!r}: {type(exc).__name__}",
                fatal=True,
                reason=MetricReason.CHILD_CRASHED,
            ) from exc
        if not line:
            code = await self._exit_code()
            await self.kill()
            raise ChildError(
                "child_crashed",
                f"the child exited (code {code}) during {op!r}: "
                f"{self.stderr_tail or 'no stderr output'}",
                fatal=True,
                reason=MetricReason.CHILD_CRASHED,
            )
        try:
            answer = json.loads(line)
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            await self.kill()
            raise ChildError(
                "bad_answer",
                f"unreadable answer to {op!r}: {sanitize_reason(repr(line[:120]))}",
                fatal=True,
                reason=MetricReason.BAD_ANSWER,
            )
        if not answer.get("ok"):
            raise ChildError(
                str(answer.get("failure_type", "benchmark_error")),
                sanitize_reason(str(answer.get("message", ""))),
                fatal=False,
            )
        return answer

    async def call(
        self, payload: Mapping[str, Any], key: str, timeout_s: float
    ) -> dict[str, Any]:
        """:meth:`request`, then the answer's ``key`` object (``info`` / ``sample``);
        an answer without it is protocol garbage: the child is killed."""
        answer = await self.request(payload, timeout_s)
        value = answer.get(key)
        if not isinstance(value, dict):
            await self.kill()
            raise ChildError(
                "bad_answer",
                f"answer to {payload.get('op')!r} has no {key!r} object",
                fatal=True,
                reason=MetricReason.BAD_ANSWER,
            )
        return value

    async def _exit_code(self) -> int | None:
        if self._handle is None:
            return None
        return await self._handle.wait(_REAP_S)

    def kill_now(self) -> None:
        """Synchronous tree kill; safe to call from a cancellation path."""
        if self._handle is not None:
            self.dead = True
            self._handle.kill_now()

    async def kill(self) -> None:
        self.kill_now()
        await self.reap()

    async def close(self) -> None:
        """Ask the child to exit (``bye``), kill it if it does not, reap it."""
        proc = self._proc
        if proc is None:
            return
        if not self.dead and proc.returncode is None and proc.stdin is not None:
            with contextlib.suppress(OSError, ValueError, TimeoutError):
                proc.stdin.write(b'{"op": "bye"}\n')
                await asyncio.wait_for(proc.stdin.drain(), _EXIT_GRACE_S)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), _EXIT_GRACE_S)
        self.kill_now()
        await self.reap()

    async def reap(self) -> None:
        handle = self._handle
        if handle is None:
            return
        await handle.reap(_REAP_S)
        task = self._stderr_task
        if task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), _REAP_S)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
