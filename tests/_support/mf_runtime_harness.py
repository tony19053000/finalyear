"""Shared helpers for the runtime contract / sandbox tests (not a test module).

Each test gets its own agent label (``g3t-<random>``) so parallel runs of these
tests on one Docker daemon never touch each other's containers, and every
fixture checks at teardown that nothing with its label is left.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from meshforge.agent.execution.runtime import (
    RunLimits,
    RunObserver,
    RunResult,
    RunSpec,
    Stream,
)
from meshforge.common.ids import new_attempt_id

DOCKER_PATH = "/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"


class RuntimeLike(Protocol):
    @property
    def name(self) -> str: ...

    async def run(
        self,
        spec: RunSpec,
        on_output: Callable[[Stream, bytes], Awaitable[None]],
        *,
        observer: RunObserver | None = None,
    ) -> RunResult: ...


@dataclass
class Capture:
    """Collects on_output calls with their arrival time (wall clock, test only)."""

    chunks: list[tuple[Stream, bytes, float]] = field(default_factory=list)
    concurrent: int = 0
    max_concurrent: int = 0
    seen: asyncio.Event = field(default_factory=asyncio.Event)
    marker: bytes = b"READY"

    async def __call__(self, stream: Stream, data: bytes) -> None:
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            self.chunks.append((stream, data, time.time()))
            if self.marker in self.data(Stream.STDOUT):
                self.seen.set()
            await asyncio.sleep(0)
        finally:
            self.concurrent -= 1

    def data(self, stream: Stream) -> bytes:
        return b"".join(d for s, d, _ in self.chunks if s is stream)


@dataclass
class Target:
    """One runtime under test plus how to address a Python inside it."""

    runtime: RuntimeLike
    kind: str  # "docker" | "subprocess"
    image: str | None
    label: str
    python: str

    @property
    def is_docker(self) -> bool:
        return self.kind == "docker"

    def env(self, **extra: str) -> dict[str, str]:
        if self.is_docker:
            base = {"PATH": DOCKER_PATH, "LANG": "C.UTF-8"}
        else:
            base = {"PATH": os.environ.get("PATH", "")}
            if sys.platform == "win32":  # pragma: no cover
                base["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        base.update(extra)
        return base

    def spec(
        self,
        workdir: Path,
        code: str,
        *,
        timeout_s: float = 60.0,
        memory_mb: int = 256,
        pids: int = 128,
        grace_s: float = 3.0,
        env: dict[str, str] | None = None,
        setup_code: str | None = None,
        gpu_indices: tuple[int, ...] = (),
        network: bool = False,
        argv: tuple[str, ...] | None = None,
        image: str | None = None,
    ) -> RunSpec:
        return RunSpec(
            attempt_id=new_attempt_id(),
            argv=argv or (self.python, "-u", "-c", code),
            workdir=workdir,
            env=env if env is not None else self.env(),
            limits=RunLimits(
                memory_bytes=memory_mb << 20, pids=pids, timeout_s=timeout_s
            ),
            image=image or self.image,
            setup_argv=None if setup_code is None else (self.python, "-c", setup_code),
            gpu_indices=gpu_indices,
            network=network,
            grace_s=grace_s,
        )


def new_label() -> str:
    return f"g3t-{secrets.token_hex(6)}"


def alive(pid: int) -> bool:
    import psutil  # type: ignore[import-untyped]

    try:
        return bool(psutil.Process(pid).status() != psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return False


def containers_with_label(label: str) -> list[Any]:
    from meshforge.agent.execution.docker_support import LABEL_AGENT, open_client

    client = open_client(10)
    try:
        return list(
            client.containers.list(
                all=True, filters={"label": f"{LABEL_AGENT}={label}"}
            )
        )
    finally:
        client.close()
