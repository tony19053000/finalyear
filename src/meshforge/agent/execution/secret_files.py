"""Job secrets on the agent: checked, materialised, wiped (V0.7 G6, D144).

A ``StartAttempt`` carries the values of the job owner's secrets the manifest
names (``StartAttempt.secrets``; sent only over mTLS). :func:`check_secrets`
validates them against the manifest (every named secret exactly once, nothing
extra, value bounds, ``as_env`` values must be text) and refuses them on an
``insecure-dev`` agent unless ``[security] allow_insecure_secrets``.

:class:`SecretVault` writes one attempt's values as files in a private folder:

* the folder lives under the agent's **secrets root** - ``/dev/shm`` (RAM,
  never written to a disk) on Linux when it is a usable tmpfs, else
  ``<data_dir>/secrets`` (documented: then the files sit on disk, ``0600``,
  for the attempt's lifetime) - in
  ``meshforge-secrets-<uid>-<agent tag>/<attempt_id>``;
* the per-agent base is created ``0700`` and is **checked every time it is
  used** - by materialise, wipe and sweep, through one method
  (:meth:`SecretVault._open_base`, "The base check" below);
* files are ``0400`` (Docker: bind-mounted read-only at ``/run/secrets``,
  owned by the container user) or ``0600`` (subprocess/venv runtimes: the
  folder's path is ``MESHFORGE_SECRETS_DIR``); the folder becomes ``0500``.

:meth:`SecretVault.wipe` overwrites every file with zeros, unlinks it and
removes the folder; the executor calls it when the attempt ends on **every**
path (success, failure, cancel, agent shutdown), and :meth:`SecretVault.sweep`
removes what a crashed agent left at the next start.

The base check (fix round 3, T-21)
----------------------------------
``/dev/shm`` is world-writable (sticky) and the base's name is predictable,
so another local user can create that name first - as a symlink to a
directory of ours, and a sweep that followed it would zero and delete what it
points at. Therefore the base is opened with ``O_NOFOLLOW | O_DIRECTORY`` and
the **opened descriptor** is ``fstat``-ed: it must be a directory, owned by
the agent's uid, mode exactly ``0700``. Anything else is refused
(:class:`SecretsBaseRefusedError`, ``config_error``,
``secrets_base_refused:<reason>``) and left exactly as it is: materialise
raises (the attempt fails typed - a job without secrets is not affected),
wipe and sweep do nothing; each logs one warning with the reason and the
base's own name (``agent.execution.secrets_base_refused`` /
``secret_wipe_failed``). The owner removes or repairs the folder; no restart
is needed, the next attempt checks again.

Everything after the check is done **relative to that descriptor**
(``dir_fd=``): the attempt folder is created, opened (``O_NOFOLLOW``),
written, listed, zeroed and removed through descriptors, so replacing the
base's name after the check redirects nothing. What is still done by name,
and why that is safe:

* creating the root and the base (``mkdir``): creating never follows a
  planted name - it fails with "exists", and what exists is then judged by
  the check above;
* the path *to* the base (``/dev/shm`` belongs to root; ``<data_dir>`` is the
  agent's own): only the last component is hostile ground;
* after a refusal, one ``lstat`` to word the reason (symlink or not);
* inside an attempt folder, ``chmod`` by name relative to the folder's
  descriptor (to make a ``0500`` folder / ``0400`` file writable again): it
  would follow a symlink swapped in between the ``lstat`` and the ``chmod``,
  but only a process of the agent's own uid can change anything inside a
  ``0700`` base of ours - and such a process can chmod our files anyway. The
  file is then opened ``O_NOFOLLOW`` and zeroed only if the descriptor is a
  regular file;
* the folder's path handed to the runtime (a Docker bind mount, the
  ``MESHFORGE_SECRETS_DIR`` variable) - a job needs a path. In sticky
  ``/dev/shm`` nobody but its owner (or root) can rename or remove a
  directory, so the checked base keeps its name; the fallback root is inside
  the agent's own data dir;
* Windows has no directory descriptors: everything is by path there, the
  check is an ``lstat`` (symlink / not a directory refused; no uid or mode).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import shutil
import stat
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.common.manifest import SecretSpec
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.secret_names import (
    SecretNameError,
    SecretValueError,
    check_secret_value,
    normalise_secret_name,
)

__all__ = [
    "CONTAINER_SECRETS_DIR",
    "SECRETS_DIR_ENV",
    "SecretFilesError",
    "SecretVault",
    "SecretWrite",
    "SecretsBaseRefusedError",
    "agent_tag",
    "check_secrets",
    "default_root",
    "secret_env",
]

log = get_logger(__name__)

CONTAINER_SECRETS_DIR: Final = "/run/secrets"
SECRETS_DIR_ENV: Final = "MESHFORGE_SECRETS_DIR"
_SHM: Final = Path("/dev/shm")  # noqa: S108 - a private 0700 subfolder, owner-checked
_WIPE_CHUNK: Final = 64 * 1024
_FD_WALK: Final = (
    os.name == "posix"
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.listdir in os.supports_fd
)
"""Directories are opened and walked by descriptor (Linux, macOS)."""
_DIR_FLAGS: Final = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class SecretFilesError(MeshForgeError):
    """Secrets cannot be checked or materialised safely (typed per instance)."""

    default_failure_type = FailureType.CONFIG_ERROR


class SecretsBaseRefusedError(SecretFilesError):
    """The agent's secrets base is not a private directory of this user
    (``reason``: ``symlink`` / ``not_a_directory`` / ``foreign_owner`` /
    ``wrong_mode`` / ``unreadable``); nothing is read, written or removed."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            f"secrets_base_refused:{reason}: the agent's secrets folder must be a "
            "real directory owned by the agent's user with mode 0700; job "
            "secrets are refused on this node until its owner removes or "
            "repairs that folder (see the agent log: "
            "agent.execution.secrets_base_refused)",
        )
        self.reason = reason

    def __reduce__(self) -> tuple[object, ...]:
        return (type(self), (self.reason,))


def check_secrets(
    received: Sequence[agent_pb2.JobSecret],
    wanted: Sequence[SecretSpec],
    *,
    allowed: bool,
) -> dict[str, bytes]:
    """name -> value, exactly the manifest's names (module docstring)."""
    if not received and not wanted:
        return {}
    if not allowed:
        raise SecretFilesError(
            "secrets_need_mtls: this agent runs insecure-dev; job secrets are "
            "refused without mTLS ([security] allow_insecure_secrets)",
        )
    malformed = SecretFilesError(
        "secrets_mismatch: the controller sent a malformed secret",
        failure_type=FailureType.INVALID_INPUT,
    )
    values: dict[str, bytes] = {}
    for item in received:
        try:
            name = normalise_secret_name(item.name)
            value = check_secret_value(bytes(item.value))
        except (SecretNameError, SecretValueError):
            raise malformed from None
        if name in values or name != item.name:
            raise malformed
        values[name] = value
    if set(values) != {spec.name for spec in wanted}:
        raise SecretFilesError(
            "secrets_mismatch: the secrets sent are not the ones the manifest names",
            failure_type=FailureType.INVALID_INPUT,
        )
    for spec in wanted:
        if spec.as_env:
            try:
                text = values[spec.name].decode("utf-8")
            except UnicodeDecodeError:
                text = "\0"
            if "\0" in text:
                raise SecretFilesError(
                    f"secret_not_text:{spec.name}: a secret used as an environment "
                    "variable must be UTF-8 text without NUL; read it from its file",
                    failure_type=FailureType.INVALID_INPUT,
                )
    return values


def secret_env(
    values: Mapping[str, bytes], wanted: Sequence[SecretSpec]
) -> dict[str, str]:
    """The ``as_env`` variables (only those the manifest asks for)."""
    return {
        spec.name: values[spec.name].decode("utf-8") for spec in wanted if spec.as_env
    }


def default_root(data_dir: Path) -> Path:
    """``/dev/shm`` on Linux when it is a writable directory, else the data dir."""
    if sys.platform.startswith("linux"):
        with contextlib.suppress(OSError):
            info = os.stat(_SHM)
            if stat.S_ISDIR(info.st_mode) and os.access(_SHM, os.W_OK | os.X_OK):
                return _SHM
    return data_dir / "secrets"


@dataclass(frozen=True, slots=True)
class _Dir:
    """A directory to work in: by descriptor where the platform can
    (``fd``; names then resolve relative to it, whatever its path points at
    by now), else by path (``fd`` is None - Windows)."""

    fd: int | None
    path: Path

    def at(self, name: str) -> str:
        """What to pass as the path argument next to ``dir_fd=self.fd``."""
        return name if self.fd is not None else str(self.path / name)

    def names(self) -> list[str]:
        return sorted(os.listdir(self.path if self.fd is None else self.fd))

    def lstat(self, name: str) -> os.stat_result:
        return os.stat(self.at(name), dir_fd=self.fd, follow_symlinks=False)

    def open_dir(self, name: str) -> _Dir:
        """The sub-directory ``name`` itself - never what a symlink names."""
        if self.fd is None:
            if not stat.S_ISDIR(self.lstat(name).st_mode):
                raise NotADirectoryError(name)
            return _Dir(None, self.path / name)
        return _Dir(os.open(name, _DIR_FLAGS, dir_fd=self.fd), self.path / name)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)


@dataclass(slots=True)
class SecretVault:
    """One agent's secret folders (module docstring)."""

    root: Path
    tag: str = ""
    """Names this agent's base apart from another agent of the same user on
    the same host (a hash of its data dir): a sweep never touches theirs."""

    @property
    def base(self) -> Path:
        uid = os.getuid() if hasattr(os, "getuid") else 0
        suffix = f"-{self.tag}" if self.tag else ""
        return self.root / f"meshforge-secrets-{uid}{suffix}"

    def folder(self, attempt_id: str) -> Path:
        """Where one attempt's files are (or would be); creates nothing."""
        return self.base / attempt_id

    def _ensure_base(self) -> Path:
        """Create the base if it is missing (never trusted: :meth:`_open_base`)."""
        base = self.base
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            base.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise SecretFilesError(
                f"cannot create the secrets folder: {exc.strerror or exc}"
            ) from None
        else:
            self._own_new_base(base)
        return base

    @staticmethod
    def _own_new_base(base: Path) -> None:
        """We just created it: an unusual umask must not leave it refused.
        By descriptor and only if it is ours - a name swapped in meanwhile is
        not touched here and is refused by :meth:`_open_base`."""
        if not _FD_WALK:
            return
        with contextlib.suppress(OSError):
            fd = os.open(base, _DIR_FLAGS)
            try:
                if os.fstat(fd).st_uid == os.getuid():
                    os.fchmod(fd, 0o700)
            finally:
                os.close(fd)

    def _open_base(self) -> _Dir | None:
        """THE check of the base, shared by materialise, wipe and sweep (T-21).

        Returns the base opened as a directory, or None when it does not exist.
        Refused (:class:`SecretsBaseRefusedError`): a symlink, anything but a
        directory, an owner other than this user, a mode other than ``0700``.
        The checks are made on the **opened descriptor** (``O_NOFOLLOW``, then
        ``fstat``), and the caller works relative to it, so replacing the name
        after the check redirects nothing.
        """
        base = self.base
        if not _FD_WALK:  # Windows: no descriptors for directories, no modes
            try:
                info = os.lstat(base)
            except FileNotFoundError:
                return None
            if stat.S_ISLNK(info.st_mode):
                raise SecretsBaseRefusedError("symlink")
            if not stat.S_ISDIR(info.st_mode):
                raise SecretsBaseRefusedError("not_a_directory")
            return _Dir(None, base)
        try:
            fd = os.open(base, _DIR_FLAGS)
        except FileNotFoundError:
            return None
        except OSError as exc:
            # Refused already (ELOOP / ENOTDIR: O_NOFOLLOW | O_DIRECTORY on a
            # symlink; EACCES: a mode without read). The lstat only words it.
            reason = "unreadable"
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                reason = "symlink" if os.path.islink(base) else "not_a_directory"
            raise SecretsBaseRefusedError(reason) from None
        info = os.fstat(fd)
        reason = ""
        if not stat.S_ISDIR(info.st_mode):
            reason = "not_a_directory"
        elif info.st_uid != os.getuid():
            reason = "foreign_owner"
        elif stat.S_IMODE(info.st_mode) != 0o700:
            reason = "wrong_mode"
        if reason:
            os.close(fd)
            raise SecretsBaseRefusedError(reason)
        return _Dir(fd, base)

    def _refused(self, event: str, exc: SecretsBaseRefusedError, **fields: str) -> None:
        # The base's own name only - never what a symlink points at.
        log.warning(event, reason=exc.reason, base=self.base.name, **fields)

    def materialise(
        self,
        attempt_id: str,
        values: Mapping[str, bytes],
        *,
        read_only: bool,
        owner: tuple[int, int] | None = None,
    ) -> Path:
        """Write ``values`` to ``<base>/<attempt_id>/NAME``; returns the folder."""
        _check_component(attempt_id)
        self._ensure_base()
        try:
            base = self._open_base()
        except SecretsBaseRefusedError as exc:
            self._refused(
                "agent.execution.secrets_base_refused", exc, attempt_id=attempt_id
            )
            raise
        if base is None:
            raise SecretFilesError("the secrets folder vanished while it was created")
        mode = 0o400 if read_only else 0o600
        try:
            self._wipe_entry(base, attempt_id)
            os.mkdir(base.at(attempt_id), 0o700, dir_fd=base.fd)
            try:
                folder = base.open_dir(attempt_id)
                try:
                    if folder.fd is not None:
                        os.fchmod(folder.fd, 0o700)  # whatever the umask is
                    for name, value in values.items():
                        _check_component(name)
                        self._write(folder, name, value, mode, owner)
                    if folder.fd is not None:
                        if owner is not None:
                            os.fchown(folder.fd, *owner)
                        os.fchmod(folder.fd, 0o500)
                finally:
                    folder.close()
            except BaseException:
                self._wipe_entry(base, attempt_id)
                raise
        finally:
            base.close()
        return self.folder(attempt_id)

    @staticmethod
    def _write(
        folder: _Dir,
        name: str,
        value: bytes,
        mode: int,
        owner: tuple[int, int] | None,
    ) -> None:
        fd = os.open(
            folder.at(name),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=folder.fd,
        )
        with os.fdopen(fd, "wb") as fh:
            fh.write(value)
            fh.flush()
            if _FD_WALK:
                if owner is not None:
                    os.fchown(fd, *owner)
                os.fchmod(fd, mode)
        if not _FD_WALK:
            os.chmod(folder.path / name, mode)

    def wipe(self, folder: Path | None) -> None:
        """Zero, unlink and remove one attempt's folder (never raises).

        Only ever ``<base>/<name>``: a path elsewhere is refused, and so is a
        base that fails :meth:`_open_base` (nothing is touched, a warning says
        why).
        """
        if folder is None:
            return
        attempt_id = folder.name
        if folder.parent != self.base:
            log.warning(
                "agent.execution.secret_wipe_failed",
                attempt_id=attempt_id,
                reason="outside_the_base",
            )
            return
        try:
            base = self._open_base()
        except SecretsBaseRefusedError as exc:
            self._refused(
                "agent.execution.secret_wipe_failed", exc, attempt_id=attempt_id
            )
            return
        if base is None:
            return
        try:
            self._wipe_logged(base, attempt_id)
        finally:
            base.close()

    def sweep(self) -> int:
        """Remove every entry of the base (a crashed agent's); returns how many.

        A refused base (:meth:`_open_base`) is left exactly as it is: 0, with
        the warning ``agent.execution.secrets_base_refused``.
        """
        try:
            base = self._open_base()
        except SecretsBaseRefusedError as exc:
            self._refused("agent.execution.secrets_base_refused", exc)
            return 0
        if base is None:
            return 0
        try:
            try:
                names = base.names()
            except OSError:
                return 0
            for name in names:
                self._wipe_logged(base, name)
        finally:
            base.close()
        if names:
            log.info("agent.execution.secrets_swept", count=len(names))
        return len(names)

    def _wipe_logged(self, base: _Dir, name: str) -> None:
        try:
            self._wipe_entry(base, name)
        except OSError:
            log.warning("agent.execution.secret_wipe_failed", attempt_id=name)

    def _wipe_entry(self, base: _Dir, name: str) -> None:
        """Remove ``name`` inside the opened base; nothing outside it.

        An attempt folder: every regular file zeroed, everything unlinked, the
        folder removed. Anything else (a symlink, a FIFO, a plain file - the
        agent creates none) is unlinked, never opened.
        """
        try:
            info = base.lstat(name)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(base.at(name), dir_fd=base.fd)
            return
        # By name, relative to the verified base (module docstring: what stays
        # name-based and why): the folder is 0500, it must be listable.
        with contextlib.suppress(OSError):
            os.chmod(base.at(name), 0o700, dir_fd=base.fd)
        folder = base.open_dir(name)
        try:
            for entry in folder.names():
                with contextlib.suppress(OSError):
                    self._zero(folder, entry)
                with contextlib.suppress(FileNotFoundError):
                    if stat.S_ISDIR(folder.lstat(entry).st_mode):
                        shutil.rmtree(folder.at(entry), dir_fd=folder.fd)
                    else:
                        os.unlink(folder.at(entry), dir_fd=folder.fd)
        finally:
            folder.close()
        os.rmdir(base.at(name), dir_fd=base.fd)

    @staticmethod
    def _zero(folder: _Dir, name: str) -> None:
        """Overwrite one regular file with zeros (a hard link made by the job
        shares the inode and is zeroed with it)."""
        if not stat.S_ISREG(folder.lstat(name).st_mode):
            return
        os.chmod(folder.at(name), 0o600, dir_fd=folder.fd)
        fd = os.open(
            folder.at(name),
            os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=folder.fd,
        )
        with os.fdopen(fd, "r+b", buffering=0) as fh:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return
            remaining = info.st_size
            while remaining > 0:
                n = min(remaining, _WIPE_CHUNK)
                fh.write(b"\0" * n)
                remaining -= n
            os.fsync(fd)


class SecretWrite:
    """One attempt's secret files being written in a worker thread - and the
    guarantee that they are wiped, whoever is still there to do it (T-19).

    A cancelled ``await`` cannot stop the thread that writes, and a
    filesystem call can hang (a stale mount). The attempt's cleanup therefore
    waits for the write only for a bound (``[agent.execution]
    secret_write_wait_s``) and then calls :meth:`finish`:

    * the write has returned -> the folder is wiped now (True);
    * it has not -> the wipe is **handed to the write thread itself** (False):
      the moment ``materialise`` returns, that thread wipes the folder - no
      event loop, no attempt, no executor is needed any more. A write that
      had not even started is skipped.

    One lock decides which of the two happens, so no order of "the write
    returns" and "the wait gives up" leaves the folder behind. What remains:
    until a hung write returns, the files it already wrote exist (``0600``,
    in a ``0700`` folder in RAM); if the process is killed first, the next
    start's :meth:`SecretVault.sweep` removes them.
    """

    def __init__(self, vault: SecretVault, attempt_id: str) -> None:
        self._vault = vault
        self._attempt_id = attempt_id
        self._lock = threading.Lock()
        self._returned = False
        self._abandoned = threading.Event()

    def run(
        self,
        values: Mapping[str, bytes],
        *,
        read_only: bool,
        owner: tuple[int, int] | None = None,
    ) -> Path:
        """In the worker thread: :meth:`SecretVault.materialise`."""
        with self._lock:
            if self._abandoned.is_set():  # the attempt ended before we started
                self._returned = True
                raise SecretFilesError(
                    "the attempt ended before its secrets were written"
                )
        try:
            return self._vault.materialise(
                self._attempt_id, values, read_only=read_only, owner=owner
            )
        finally:
            with self._lock:
                self._returned = True
                if self._abandoned.is_set():
                    self._vault.wipe(self._vault.folder(self._attempt_id))

    def finish(self) -> bool:
        """Wipe now if the write has returned (True); else leave the wipe to
        the write thread (False). Blocking (a few small files): off the loop."""
        with self._lock:
            if not self._returned:
                self._abandoned.set()
                return False
            self._vault.wipe(self._vault.folder(self._attempt_id))
            return True


def _check_component(name: str) -> None:
    """A file or folder name used relative to a descriptor is ONE component."""
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise SecretFilesError(
            "secrets_mismatch: a secret folder or file name is not a plain name",
            failure_type=FailureType.INVALID_INPUT,
        )


def agent_tag(data_dir: Path) -> str:
    """12 hex chars naming one agent's data dir (never the path itself)."""
    return hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:12]
