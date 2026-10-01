"""Build each environment once per env hash per node; reuse it forever (V0.5 D86).

Layout under the envs root (``[agent.envs] dir``, default ``<data_dir>/envs``)::

    venv/<hash>/                 a uv venv (venv runtime), read-only
    subprocess/<hash>/           a ``--target`` dir for the agent's interpreter
    <kind>/<hash>.building/      a build in progress (never used by a job)
    locks/<hash>.lock            one build per hash at a time (never deleted)
    staging/<hash>/              the requirements file a build reads
    uv-cache/                    uv's wheel cache, shared by every build

**Build once.** :meth:`EnvBuilder.ensure_dir_env` returns at once when
``<kind>/<hash>`` holds a valid marker (``.meshforge-env.json`` naming the hash,
written last). Otherwise it takes the build lock, checks again (another process
may have finished meanwhile - the waiter then reuses it: ``cache = "waited"``),
removes any leftover ``.building`` (a crashed build), checks the free-disk floor,
builds into ``<hash>.building``, checks the size cap, writes the marker, makes the
tree read-only and **renames** it into place. A crash at any point leaves either
no ``<hash>`` or a complete one; a failure or a cancellation removes the
``.building`` dir before the error propagates.

**Inputs.** :meth:`read_requirements` reads the job's requirements file from its
workdir - bounded, and refused if it resolves outside the workdir (a symlink in the
project). The *normalised* bytes are both hashed and installed, so the hash names
exactly what was built.

Docker environments (images ``meshforge-env:<hash>``) are built by the Docker
runtime with this builder's lock, floor and size cap (:meth:`lock`,
:meth:`check_free_disk`, :attr:`EnvsConfig.max_env_bytes`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from meshforge.agent.envs._tool import (
    OutputSink,
    capture_tool,
    run_tool,
    tool_env,
    venv_python,
)
from meshforge.agent.envs.errors import EnvError
from meshforge.agent.envs.fsutil import env_lock, make_read_only, remove_tree, tree_size
from meshforge.agent.envs.hash import EnvInputs, env_hash, normalize_requirements
from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger

if TYPE_CHECKING:
    from meshforge.agent.settings import AgentSettings

__all__ = [
    "MARKER_NAME",
    "BuiltEnv",
    "EnvBuilder",
    "EnvsConfig",
    "Interpreter",
    "default_uv_argv",
]

log = get_logger(__name__)

MARKER_NAME: Final = ".meshforge-env.json"
_MIB: Final = 1024 * 1024
_QUERY_TIMEOUT_S: Final = 30.0
_PROBE_SCRIPT: Final = (
    "import json, sys, sysconfig; print(json.dumps([sys.implementation.name, "
    "'%d.%d.%d' % sys.version_info[:3], sysconfig.get_platform()]))"
)
CacheState = Literal["hit", "waited", "built"]


def default_uv_argv() -> tuple[str, ...]:
    """The pinned ``uv`` binary from the ``uv`` wheel (agent extra); ``()`` when
    the wheel is not installed (base install) - builds then fail ``uv_missing``."""
    try:
        import uv
    except ImportError:
        return ()
    try:
        return (uv.find_uv_bin(),)
    except FileNotFoundError:
        return ()


@dataclass(frozen=True, slots=True)
class EnvsConfig:
    """What the builder needs (from ``[agent.envs]`` + ``data_dir``)."""

    root: Path
    max_env_bytes: int = 16 * 1024**3
    free_disk_floor_bytes: int = 2048 * _MIB
    max_requirements_bytes: int = 65_536
    max_requirements_lines: int = 1000
    find_links: Path | None = None
    no_index: bool = False
    index_url: str | None = None
    allow_python_downloads: bool = False

    @classmethod
    def from_settings(cls, settings: AgentSettings) -> EnvsConfig:
        cfg = settings.agent.envs
        return cls(
            root=settings.envs_dir,
            max_env_bytes=cfg.max_env_bytes,
            free_disk_floor_bytes=cfg.free_disk_floor_mb * _MIB,
            max_requirements_bytes=cfg.max_requirements_bytes,
            max_requirements_lines=cfg.max_requirements_lines,
            find_links=cfg.find_links,
            no_index=cfg.no_index,
            index_url=cfg.index_url,
            allow_python_downloads=cfg.allow_python_downloads,
        )

    def index_options(self) -> list[str]:
        """Installer options for the configured index / wheelhouse."""
        options: list[str] = []
        if self.index_url:
            options += ["--index-url", self.index_url]
        if self.find_links is not None:
            options += ["--find-links", str(self.find_links)]
        if self.no_index:
            options.append("--no-index")
        return options


@dataclass(frozen=True, slots=True)
class Interpreter:
    """A Python interpreter the venv runtime can build on."""

    path: Path
    implementation: str
    version: str
    platform: str

    @property
    def base(self) -> str:
        """The env hash ``base`` (e.g. ``cpython-3.12.3``)."""
        return f"{self.implementation}-{self.version}"


@dataclass(frozen=True, slots=True)
class BuiltEnv:
    """A ready environment. ``cache``: ``hit`` (already there), ``waited`` (another
    build finished while we waited for the lock) or ``built`` (by this call)."""

    env_hash: str
    kind: str
    path: Path
    cache: CacheState
    build_s: float = 0.0
    """Lock wait + build, monotonic (0 for a plain hit)."""
    size_bytes: int = 0

    @property
    def python(self) -> Path:
        return venv_python(self.path)


BuildStep = Callable[[Path], Awaitable[None]]


class EnvBuilder:
    """See module docstring. One per agent process; safe for concurrent calls."""

    def __init__(
        self,
        config: EnvsConfig,
        *,
        uv_argv: Sequence[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        disk_free: Callable[[Path], int] | None = None,
    ) -> None:
        self.config = config
        self.uv_argv = tuple(uv_argv) if uv_argv is not None else default_uv_argv()
        self._clock = clock
        self._disk_free = disk_free or _disk_free
        self._interpreters: dict[str, Interpreter] = {}

    # ---------------------------------------------------------------- paths
    @property
    def root(self) -> Path:
        return self.config.root

    def env_dir(self, kind: str, digest: str) -> Path:
        return self.root / kind / digest

    def lock_path(self, digest: str) -> Path:
        return self.root / "locks" / f"{digest}.lock"

    def staging_dir(self, digest: str) -> Path:
        return self.root / "staging" / digest

    def _prepare_root(self) -> None:
        for sub in ("", "locks", "staging", "uv-cache"):
            path = self.root / sub if sub else self.root
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                path.chmod(0o700)

    # --------------------------------------------------------------- inputs
    def read_requirements(self, workdir: Path, relative: str) -> bytes:
        """The job's requirements file, normalised (module docstring)."""
        limit = self.config.max_requirements_bytes
        try:
            base = workdir.resolve(strict=True)
            target = (workdir / relative).resolve(strict=True)
        except (OSError, RuntimeError):
            raise EnvError(
                f"the requirements file {relative!r} is not in the project",
                reason="requirements_missing",
            ) from None
        if not target.is_relative_to(base) or not target.is_file():
            raise EnvError(
                f"the requirements file {relative!r} is not a file inside the project",
                reason="requirements_missing",
            )
        try:
            with target.open("rb") as fh:
                data = fh.read(limit + 1)
        except OSError as exc:
            raise EnvError(
                f"cannot read the requirements file: {exc.strerror or type(exc).__name__}",
                reason="requirements_missing",
            ) from None
        return normalize_requirements(
            data,
            max_bytes=limit,
            max_lines=self.config.max_requirements_lines,
        )

    # ------------------------------------------------------ shared machinery
    def check_free_disk(self) -> None:
        """``EnvError(disk_full)`` when the envs disk has less than the floor free."""
        self._prepare_root()
        free = self._disk_free(self.root)
        floor = self.config.free_disk_floor_bytes
        if free < floor:
            raise EnvError(
                f"only {free // _MIB} MiB free on the environments disk; at least "
                f"{floor // _MIB} MiB is required ([agent.envs] free_disk_floor_mb)",
                reason="disk_full",
                failure_type=FailureType.DISK_FULL,
            )

    def check_size(self, size: int) -> None:
        """``EnvError(env_too_large)`` when a built env is over the cap."""
        if size > self.config.max_env_bytes:
            raise EnvError(
                f"the environment is {size} bytes; the maximum is "
                f"{self.config.max_env_bytes} ([agent.envs] max_env_bytes)",
                reason="env_too_large",
            )

    @contextlib.asynccontextmanager
    async def lock(self, digest: str, *, deadline: float) -> AsyncIterator[None]:
        """The build lock of one env hash (also used by the Docker runtime)."""
        await _to_thread(self._prepare_root)
        async with env_lock(
            self.lock_path(digest), deadline=deadline, clock=self._clock
        ):
            yield

    def is_ready(self, kind: str, digest: str) -> bool:
        """``<kind>/<hash>`` exists and its marker names this hash and kind."""
        marker = self.env_dir(kind, digest) / MARKER_NAME
        try:
            data = json.loads(marker.read_bytes())
        except (OSError, ValueError):
            return False
        return (
            isinstance(data, dict)
            and data.get("env_hash") == digest
            and data.get("kind") == kind
        )

    async def ensure_dir_env(
        self,
        kind: str,
        digest: str,
        build: BuildStep,
        *,
        deadline: float,
        log_fields: dict[str, str] | None = None,
    ) -> BuiltEnv:
        """The directory environment ``<kind>/<digest>``, built by ``build`` if
        missing (module docstring)."""
        fields = {"env_hash": digest, "kind": kind, **(log_fields or {})}
        final = self.env_dir(kind, digest)
        if await _to_thread(self.is_ready, kind, digest):
            log.info("agent.envs.cache_hit", **fields)
            return BuiltEnv(digest, kind, final, "hit")
        started = self._clock()
        async with self.lock(digest, deadline=deadline):
            if await _to_thread(self.is_ready, kind, digest):
                waited = self._clock() - started
                log.info("agent.envs.cache_waited", **fields, wait_s=round(waited, 3))
                return BuiltEnv(digest, kind, final, "waited", build_s=waited)
            building = final.with_name(f"{digest}.building")
            await _to_thread(self._clear, final, building)
            await _to_thread(self.check_free_disk)
            log.info("agent.envs.build_started", **fields)
            try:
                await build(building)
                size = await _to_thread(tree_size, building)
                self.check_size(size)
                await _to_thread(self._seal, building, final, kind, digest, size)
            except BaseException as exc:
                await _shielded(remove_tree, building)
                log.warning(
                    "agent.envs.build_failed",
                    **fields,
                    reason=getattr(exc, "reason", type(exc).__name__),
                )
                raise
        build_s = self._clock() - started
        log.info(
            "agent.envs.build_finished",
            **fields,
            build_s=round(build_s, 3),
            size_bytes=size,
        )
        return BuiltEnv(digest, kind, final, "built", build_s=build_s, size_bytes=size)

    def _clear(self, final: Path, building: Path) -> None:
        """Under the lock: a leftover ``.building`` is a crashed build; a ``<hash>``
        without a valid marker is damaged - both go."""
        final.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            final.parent.chmod(0o700)
        remove_tree(building)
        remove_tree(final)

    def _seal(
        self, building: Path, final: Path, kind: str, digest: str, size: int
    ) -> None:
        marker = {
            "v": 1,
            "env_hash": digest,
            "kind": kind,
            "size_bytes": size,
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        atomic_write_bytes(
            building / MARKER_NAME,
            json.dumps(marker, sort_keys=True).encode("ascii"),
            mode=0o444,
        )
        make_read_only(building)
        os.replace(building, final)

    # ------------------------------------------------------------- the tools
    def _uv(self) -> tuple[str, ...]:
        if not self.uv_argv:
            raise EnvError(
                "the uv binary is not installed (agent extra: `uv sync --extra agent`)",
                reason="uv_missing",
            )
        return self.uv_argv

    def _uv_env(self) -> dict[str, str]:
        extra = {
            "UV_CACHE_DIR": str(self.root / "uv-cache"),
            "UV_PYTHON_DOWNLOADS": "automatic"
            if self.config.allow_python_downloads
            else "never",
            "UV_NO_PROGRESS": "1",
        }
        return tool_env(extra)

    def _write_requirements(self, digest: str, requirements: bytes) -> Path:
        staging = self.staging_dir(digest)
        remove_tree(staging)
        staging.mkdir(parents=True)
        if os.name == "posix":
            staging.chmod(0o700)
        path = staging / "requirements.txt"
        atomic_write_bytes(path, requirements, mode=0o600)
        return path

    async def find_interpreter(self, request: str | None) -> Interpreter:
        """The interpreter for ``env.python`` (default: this agent's major.minor)."""
        wanted = request or f"{sys.version_info[0]}.{sys.version_info[1]}"
        found = self._interpreters.get(wanted)
        if found is not None:
            return found
        uv = self._uv()
        await _to_thread(self._prepare_root)
        env = self._uv_env()
        find = [*uv, "python", "find", "--no-config", "--system", wanted]
        code, out = await self._query(find, env)
        if code != 0 and self.config.allow_python_downloads:
            await self._query([*uv, "python", "install", "--no-config", wanted], env)
            code, out = await self._query(find, env)
        if code != 0 or not out.strip():
            raise EnvError(
                f"no Python {wanted} on this node"
                + (
                    ""
                    if self.config.allow_python_downloads
                    else " ([agent.envs] allow_python_downloads is off)"
                ),
                reason="python_not_found",
            )
        path = Path(out.decode("utf-8", "replace").strip().splitlines()[-1])
        code, probe = await self._query([str(path), "-I", "-c", _PROBE_SCRIPT], env)
        try:
            impl, version, platform = json.loads(probe)
            interpreter = Interpreter(path, str(impl), str(version), str(platform))
        except (ValueError, TypeError):
            raise EnvError(
                f"the Python {wanted} interpreter did not describe itself",
                reason="python_not_found",
            ) from None
        if code != 0:
            raise EnvError(
                f"Python {wanted} failed to start", reason="python_not_found"
            )
        self._interpreters[wanted] = interpreter
        return interpreter

    async def _query(self, argv: list[str], env: dict[str, str]) -> tuple[int, bytes]:
        # cwd = the envs root: uv must not discover a project's .venv from the
        # agent's working directory
        return await capture_tool(
            argv,
            env=env,
            timeout_s=_QUERY_TIMEOUT_S,
            cwd=self.root,
            what="a Python lookup",
        )

    async def _install(
        self,
        argv: list[str],
        *,
        deadline: float,
        on_output: OutputSink | None,
    ) -> None:
        code = await run_tool(
            argv,
            env=self._uv_env(),
            cwd=self.root,
            deadline=deadline,
            on_output=on_output,
            clock=self._clock,
            what="the requirements install",
        )
        if code != 0:
            raise EnvError(
                f"requirements install failed (exit {code})", reason="setup_failed"
            )

    # ---------------------------------------------------------------- kinds
    async def ensure_venv(
        self,
        *,
        python: str | None,
        requirements: bytes,
        deadline: float,
        on_output: OutputSink | None = None,
        log_fields: dict[str, str] | None = None,
    ) -> BuiltEnv:
        """A uv venv with ``requirements`` (normalised; ``b""`` = none) for the
        interpreter matching ``python`` (venv runtime)."""
        uv = self._uv()
        interpreter = await self.find_interpreter(python)
        digest = env_hash(
            EnvInputs(
                runtime_kind="venv",
                base=interpreter.base,
                python=python or "",
                requirements=requirements,
                platform=interpreter.platform,
            )
        )

        async def build(target: Path) -> None:
            venv = [
                *uv,
                "venv",
                "--no-config",
                "--relocatable",
                "--python",
                str(interpreter.path),
                str(target),
            ]
            await self._install(venv, deadline=deadline, on_output=on_output)
            if requirements:
                req = await _to_thread(self._write_requirements, digest, requirements)
                try:
                    await self._install(
                        [
                            *uv,
                            "pip",
                            "install",
                            "--no-config",
                            "--python",
                            str(venv_python(target)),
                            "--link-mode",
                            "copy",
                            *self.config.index_options(),
                            "-r",
                            str(req),
                        ],
                        deadline=deadline,
                        on_output=on_output,
                    )
                finally:
                    await _to_thread(remove_tree, req.parent)

        return await self.ensure_dir_env(
            "venv", digest, build, deadline=deadline, log_fields=log_fields
        )

    async def ensure_target(
        self,
        *,
        requirements: bytes,
        deadline: float,
        on_output: OutputSink | None = None,
        log_fields: dict[str, str] | None = None,
    ) -> BuiltEnv:
        """A ``--target`` dir with ``requirements`` for this agent's own interpreter
        (``sys.executable``; subprocess runtime)."""
        uv = self._uv()
        interpreter_path = Path(sys.executable)
        version = ".".join(str(part) for part in sys.version_info[:3])
        digest = env_hash(
            EnvInputs(
                runtime_kind="subprocess",
                base=f"{sys.implementation.name}-{version}",
                python="",
                requirements=requirements,
                platform=_platform(),
            )
        )

        async def build(target: Path) -> None:
            req = await _to_thread(self._write_requirements, digest, requirements)
            try:
                await self._install(
                    [
                        *uv,
                        "pip",
                        "install",
                        "--no-config",
                        "--python",
                        str(interpreter_path),
                        "--target",
                        str(target),
                        "--link-mode",
                        "copy",
                        *self.config.index_options(),
                        "-r",
                        str(req),
                    ],
                    deadline=deadline,
                    on_output=on_output,
                )
            finally:
                await _to_thread(remove_tree, req.parent)

        return await self.ensure_dir_env(
            "subprocess", digest, build, deadline=deadline, log_fields=log_fields
        )


def _platform() -> str:
    import sysconfig

    return sysconfig.get_platform()


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


async def _to_thread[T](func: Callable[..., T], *args: object) -> T:
    return await asyncio.to_thread(func, *args)


async def _shielded(func: Callable[..., object], *args: object) -> None:
    """Cleanup that a second cancellation must not cut short."""
    await asyncio.shield(asyncio.to_thread(func, *args))
