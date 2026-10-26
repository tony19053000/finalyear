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
written last) **and** the files still match the summary the marker recorded at
build time (entry count, bytes, sha256 of the sorted path/type/size listing: one
``lstat`` per file, ~10-50 ms for a venv of a few thousand files; no file is
read, so a same-size in-place edit is not caught - the tree is read-only). A
damaged env is never used: it is logged (``agent.envs.cache_damaged``), evicted
(renamed ``<hash>.evicted-<time>-<id>``, deleted by a later build after
``evicted_keep_s``: a job still running in it is not pulled from under) and rebuilt
under the lock. The check time is logged as ``check_s`` with every cache result.
Otherwise it takes the build lock, checks again (another process
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

**Installed agents (V0.17 G1, SP3 F1/F2).** An agent running from a D384 bundle
builds venvs for its own Python version on **the bundled interpreter**, never on
whatever ``uv python find`` meets first (a system Python, or nothing on a fresh
Windows laptop). The path given to ``uv venv`` is the version-independent one
(``/opt/meshforge/current/python/bin/python3.12``, :func:`bundled_interpreter`),
so a venv's ``pyvenv.cfg`` survives an upgrade that deletes the old version's
folder; and a venv whose interpreter link dangles anyway is ``damaged`` (rebuilt),
never ``ready``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
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
from meshforge.agent.envs.fsutil import (
    env_lock,
    make_read_only,
    remove_tree,
    tree_size,
    tree_summary,
)
from meshforge.agent.envs.hash import EnvInputs, env_hash, normalize_requirements
from meshforge.agent.execution.failure_evidence import (
    DISK_FULL_ERRNOS,
    DISK_FULL_MARKER,
    OutputTail,
)
from meshforge.agent.gc.leases import lease_env
from meshforge.agent.threads import uninterrupted
from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.buildinfo import bundle_root, stable_root
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
    "bundled_interpreter",
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
EnvState = Literal["ready", "damaged", "missing"]
_DISK_FULL_ERRNOS: Final = DISK_FULL_ERRNOS
_INSTALL_TAIL_BYTES: Final = 8192
"""The end of the installer's output searched for its out-of-disk message."""


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


def bundled_interpreter(executable: str | None = None) -> Path | None:
    """This agent's interpreter by its upgrade-stable path when it runs from a
    D384 bundle (``<bundle>/python/...`` with ``<bundle>`` replaced by the
    ``current`` pointer when there is one), else ``None`` (a dev venv)."""
    exe = Path(executable if executable is not None else sys.executable)
    root = bundle_root(exe)
    if root is None:
        return None
    return stable_root(root) / exe.relative_to(root)


def venv_home_missing(venv: Path) -> bool:
    """``pyvenv.cfg`` names a ``home`` (the base interpreter's folder) that no
    longer exists. Unreadable or no ``home`` key: ``False`` (the summary check
    already covers a damaged tree)."""
    try:
        text = (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() == "home":
            home = value.strip()
            return bool(home) and not Path(home).is_dir()
    return False


ONLY_BINARY_OPTIONS: Final = ("--only-binary", ":all:")
"""Installer options for a wheels-only build (pip and uv spell it the same)."""


def only_binary_hint(only_binary: bool) -> str:
    """The escape hatch, appended to an install failure in binary-only mode."""
    if not only_binary:
        return ""
    return (
        "; installs are wheels-only ([agent.envs] only_binary = true): a package "
        "with no wheel for this platform needs only_binary = false on this node"
    )


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
    evicted_keep_s: float = 86_400.0
    only_binary: bool = True
    """V0.7 G6 (D161, WS-I4): install wheels only (``--only-binary :all:``),
    so no package's own build code (an sdist's ``setup.py``) runs during a
    build. ``[agent.envs] only_binary = false`` is the owner's escape hatch."""

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
            evicted_keep_s=cfg.evicted_keep_s,
            only_binary=cfg.only_binary,
        )

    def source_digest(self) -> str:
        """The env hash ``source`` input: sha256 of the canonical package-source
        settings, ``""`` for the default index. A ``find_links`` wheelhouse is
        represented by the names and sizes of its files (not its path: two nodes
        with the same wheels share envs; a changed wheel gives a new env). Blocking
        (one ``scandir``)."""
        if self.index_url is None and self.find_links is None and not self.no_index:
            return ""
        wheels: list[list[object]] | str | None = None
        if self.find_links is not None:
            try:
                with os.scandir(self.find_links) as found:
                    wheels = sorted(
                        [entry.name, entry.stat(follow_symlinks=False).st_size]
                        for entry in found
                        if entry.is_file(follow_symlinks=False)
                    )
            except OSError:
                wheels = "unreadable"
        document = {
            "index_url": self.index_url,
            "no_index": self.no_index,
            "find_links": wheels,
        }
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()

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

    def binary_options(self) -> list[str]:
        """``--only-binary :all:`` unless the owner allowed source builds."""
        return list(ONLY_BINARY_OPTIONS) if self.only_binary else []


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
        wall: Callable[[], float] = time.time,
        bundled_python: Path | Literal["auto"] | None = "auto",
    ) -> None:
        self.config = config
        self.uv_argv = tuple(uv_argv) if uv_argv is not None else default_uv_argv()
        self.bundled_python = (
            bundled_interpreter() if bundled_python == "auto" else bundled_python
        )
        """The bundle's interpreter (SP3 F1), used for venvs of this agent's
        own Python version instead of ``uv python find``; ``None`` in a dev venv."""
        self._clock = clock
        self._wall = wall
        self._disk_free = disk_free or _disk_free
        self._interpreters: dict[str, Interpreter] = {}

    # ---------------------------------------------------------------- paths
    @property
    def clock(self) -> Callable[[], float]:
        """The monotonic clock deadlines given to this builder are measured on."""
        return self._clock

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
            wheels_only=self.config.only_binary,
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

    def env_state(self, kind: str, digest: str) -> EnvState:
        """``ready``: ``<kind>/<hash>`` has a marker naming this hash and kind *and*
        its files still match the summary recorded at build time (one ``lstat`` per
        file, no reads - see :class:`~.fsutil.TreeSummary`); ``damaged``: a marker
        but the files differ (or an older marker without a summary); ``missing``:
        no usable marker."""
        final = self.env_dir(kind, digest)
        try:
            data = json.loads((final / MARKER_NAME).read_bytes())
        except (OSError, ValueError):
            return "missing"
        if not (
            isinstance(data, dict)
            and data.get("env_hash") == digest
            and data.get("kind") == kind
        ):
            return "missing"
        try:
            summary = tree_summary(final, skip_root_names=(MARKER_NAME,))
        except OSError:
            return "damaged"
        recorded = data.get("summary")
        if recorded != summary.as_dict():
            return "damaged"
        python = venv_python(final)
        if kind == "venv" and python.is_symlink() and not python.exists():
            # SP3 F2: the venv links an interpreter that is gone (an upgrade
            # removed the bundle folder it was built from). Same files, unusable.
            return "damaged"
        if kind == "venv" and venv_home_missing(final):
            # V0.17 G4, F2 on Windows: no symlink there - the venv's python.exe
            # is a launcher that reads `home` from pyvenv.cfg, and the versioned
            # bundle folder it names is gone after an upgrade removed it.
            return "damaged"
        return "ready"

    def is_ready(self, kind: str, digest: str) -> bool:
        """:meth:`env_state` is ``ready``."""
        return self.env_state(kind, digest) == "ready"

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
        missing (module docstring). An :class:`EnvError` carries ``digest`` as
        its ``env_hash`` (V0.6 D116: the controller remembers a known-bad env)."""
        try:
            return await self._ensure_dir_env(
                kind, digest, build, deadline=deadline, log_fields=log_fields
            )
        except EnvError as exc:
            exc.details.setdefault("env_hash", digest)
            raise

    async def _ensure_dir_env(
        self,
        kind: str,
        digest: str,
        build: BuildStep,
        *,
        deadline: float,
        log_fields: dict[str, str] | None = None,
    ) -> BuiltEnv:
        fields = {"env_hash": digest, "kind": kind, **(log_fields or {})}
        final = self.env_dir(kind, digest)
        # V0.5 G5 (D91): held by the current attempt BEFORE the readiness check,
        # so GC can never remove it between "ready" and the job using it.
        await lease_env(kind, digest)
        checked = self._clock()
        state = await _to_thread(self.env_state, kind, digest)
        check_s = round(self._clock() - checked, 4)
        if state == "ready":
            log.info("agent.envs.cache_hit", **fields, check_s=check_s)
            return BuiltEnv(digest, kind, final, "hit")
        if state == "damaged":
            # never used: evicted and rebuilt under the lock below
            log.warning("agent.envs.cache_damaged", **fields, check_s=check_s)
        started = self._clock()
        async with self.lock(digest, deadline=deadline):
            if await _to_thread(self.is_ready, kind, digest):
                waited = self._clock() - started
                log.info("agent.envs.cache_waited", **fields, wait_s=round(waited, 3))
                return BuiltEnv(digest, kind, final, "waited", build_s=waited)
            building = final.with_name(f"{digest}.building")
            try:
                await _to_thread(self._clear, final, building)
            except OSError as exc:
                # never build over a tree we could not remove; no retry loop
                log.warning(
                    "agent.envs.evict_failed", **fields, cause=type(exc).__name__
                )
                raise EnvError(
                    "a damaged or partial environment could not be removed: "
                    f"{exc.strerror or type(exc).__name__}",
                    reason="evict_failed",
                ) from exc
            await _to_thread(self.check_free_disk)
            log.info("agent.envs.build_started", **fields)
            try:
                try:
                    await build(building)
                    size = await _to_thread(tree_size, building)
                    self.check_size(size)
                    await _to_thread(self._seal, building, final, kind, digest, size)
                except OSError as exc:
                    raise _io_error(exc) from exc
            except BaseException as exc:
                await _discard(building)
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
        """Under the lock: a leftover ``.building`` (a crashed build) is removed; a
        ``<hash>`` that is not ready (damaged, or no marker) is **renamed** to
        ``<hash>.evicted-<unix time>-<id>`` - never deleted here, because a running
        job may still be using it (Windows cannot delete files in use, and a POSIX
        job would lose files under it). Evicted dirs older than
        ``evicted_keep_s`` are deleted (best effort) by :meth:`_sweep_evicted`."""
        final.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            final.parent.chmod(0o700)
        remove_tree(building)
        if os.path.lexists(final):
            evicted = final.with_name(
                f"{final.name}.evicted-{int(self._wall())}-{secrets.token_hex(3)}"
            )
            os.replace(final, evicted)
            log.warning(
                "agent.envs.evicted", env_hash=final.name, kind=final.parent.name
            )
        self._sweep_evicted(final.parent)

    def _sweep_evicted(self, folder: Path) -> None:
        """Delete ``*.evicted-<time>-*`` dirs in ``folder`` older than
        ``evicted_keep_s``; one that cannot be removed now stays for the next sweep."""
        now = self._wall()
        for entry in folder.glob("*.evicted-*"):
            try:
                stamp = int(entry.name.rsplit(".evicted-", 1)[1].split("-", 1)[0])
            except (IndexError, ValueError):
                stamp = 0
            if now - stamp < self.config.evicted_keep_s:
                continue
            try:
                remove_tree(entry)
            except OSError as exc:
                log.info(
                    "agent.envs.evicted_kept", path=entry.name, cause=type(exc).__name__
                )

    def _seal(
        self, building: Path, final: Path, kind: str, digest: str, size: int
    ) -> None:
        marker = {
            "v": 2,
            "env_hash": digest,
            "kind": kind,
            "size_bytes": size,
            "summary": tree_summary(building, skip_root_names=(MARKER_NAME,)).as_dict(),
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        atomic_write_bytes(
            building / MARKER_NAME,
            json.dumps(marker, sort_keys=True).encode("ascii"),
            mode=0o444,
        )
        make_read_only(building)
        os.replace(building, final)

    def _normalised(self, requirements: bytes) -> bytes:
        return normalize_requirements(
            requirements,
            max_bytes=self.config.max_requirements_bytes,
            max_lines=self.config.max_requirements_lines,
            wheels_only=self.config.only_binary,
        )

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

    async def _staged_requirements(self, digest: str, requirements: bytes) -> Path:
        """:meth:`_write_requirements` in a thread that has ended when this
        returns or raises: a cancel during the write removes the staging
        folder by path instead of leaving it (V0.7 G6 T-14 class)."""
        try:
            return await uninterrupted(
                asyncio.to_thread(self._write_requirements, digest, requirements)
            )
        except BaseException:
            await _discard(self.staging_dir(digest))
            raise

    async def find_interpreter(self, request: str | None) -> Interpreter:
        """The interpreter for ``env.python`` (default: this agent's major.minor).

        From a bundle, this agent's own version (``3.12`` or ``3.12.14``) is the
        bundled interpreter (module docstring, SP3 F1); anything else, and every
        request in a dev venv, is looked up with ``uv python find``."""
        own_minor = f"{sys.version_info[0]}.{sys.version_info[1]}"
        own_full = ".".join(str(part) for part in sys.version_info[:3])
        wanted = request or own_minor
        found = self._interpreters.get(wanted)
        if found is not None:
            return found
        uv = self._uv()
        await _to_thread(self._prepare_root)
        env = self._uv_env()
        if self.bundled_python is not None and wanted in (own_minor, own_full):
            path = self.bundled_python
            source = "bundle"
        else:
            path = await self._find_with_uv(uv, wanted, env)
            source = "uv_find"
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
        log.info(
            "agent.envs.interpreter",
            requested=wanted,
            source=source,
            python=interpreter.base,
            interpreter=str(interpreter.path),
        )
        self._interpreters[wanted] = interpreter
        return interpreter

    async def _find_with_uv(
        self, uv: tuple[str, ...], wanted: str, env: dict[str, str]
    ) -> Path:
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
        return Path(out.decode("utf-8", "replace").strip().splitlines()[-1])

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
        tail = OutputTail(_INSTALL_TAIL_BYTES)

        async def watched(stream: str, data: bytes) -> None:
            tail.add(data)
            if on_output is not None:
                await on_output(stream, data)

        code = await run_tool(
            argv,
            env=self._uv_env(),
            cwd=self.root,
            deadline=deadline,
            on_output=watched,
            clock=self._clock,
            what="the requirements install",
        )
        if code != 0 and DISK_FULL_MARKER.search(tail.data):
            # D116: the installer ran out of disk (its own ENOSPC message).
            raise EnvError(
                f"the disk filled up during the requirements install (exit {code})",
                reason="disk_full",
                failure_type=FailureType.DISK_FULL,
            )
        if code != 0:
            raise EnvError(
                f"requirements install failed (exit {code})"
                + only_binary_hint(self.config.only_binary),
                reason="setup_failed",
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
        """A uv venv with ``requirements`` (``b""`` = none) for the interpreter
        matching ``python`` (venv runtime). The bytes are normalised (again - it is
        idempotent) so a caller can never install a line the hash refuses."""
        requirements = self._normalised(requirements)
        uv = self._uv()
        interpreter = await self.find_interpreter(python)
        digest = env_hash(
            EnvInputs(
                runtime_kind="venv",
                base=interpreter.base,
                python=python or "",
                requirements=requirements,
                platform=interpreter.platform,
                source=await _to_thread(self.config.source_digest),
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
                req = await self._staged_requirements(digest, requirements)
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
                            *self.config.binary_options(),
                            "-r",
                            str(req),
                        ],
                        deadline=deadline,
                        on_output=on_output,
                    )
                finally:
                    await _discard(req.parent)

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
        (``sys.executable``; subprocess runtime). ``requirements`` is normalised
        as in :meth:`ensure_venv`."""
        requirements = self._normalised(requirements)
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
                source=await _to_thread(self.config.source_digest),
            )
        )

        async def build(target: Path) -> None:
            req = await self._staged_requirements(digest, requirements)
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
                        *self.config.binary_options(),
                        "-r",
                        str(req),
                    ],
                    deadline=deadline,
                    on_output=on_output,
                )
            finally:
                await _discard(req.parent)

        return await self.ensure_dir_env(
            "subprocess", digest, build, deadline=deadline, log_fields=log_fields
        )


def _io_error(exc: OSError) -> EnvError:
    """An OSError during a build, typed: a full disk / quota is ``disk_full``,
    anything else ``env_error`` (``build_io_failed``)."""
    why = exc.strerror or type(exc).__name__
    if exc.errno in _DISK_FULL_ERRNOS:
        return EnvError(
            f"the disk filled up while building the environment: {why}",
            reason="disk_full",
            failure_type=FailureType.DISK_FULL,
        )
    return EnvError(
        f"the environment build failed on the filesystem: {why}",
        reason="build_io_failed",
    )


def _platform() -> str:
    import sysconfig

    return sysconfig.get_platform()


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


async def _to_thread[T](func: Callable[..., T], *args: object) -> T:
    return await asyncio.to_thread(func, *args)


async def _discard(path: Path) -> None:
    """Remove a build leftover from a cleanup (``except`` / ``finally``).

    Shielded: a second cancellation cannot cut the removal short. And it never
    raises an error of its own: it runs while another exception - often a
    ``CancelledError`` - is on its way, and must not replace it (T-20). What
    cannot be removed is logged and removed by path before the next build
    (:meth:`EnvBuilder._clear`, :meth:`EnvBuilder._write_requirements`).
    """
    try:
        await asyncio.shield(asyncio.to_thread(remove_tree, path))
    except Exception as exc:  # noqa: BLE001 - never replace the exception in flight
        log.warning(
            "agent.envs.cleanup_failed", path=path.name, cause=type(exc).__name__
        )
