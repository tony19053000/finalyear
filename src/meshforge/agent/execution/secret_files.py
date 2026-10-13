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
* the per-agent root is created ``0700`` and must be a real directory owned
  by us (a pre-planted symlink or another user's folder in ``/dev/shm`` is
  refused - :class:`SecretFilesError`, ``config_error``);
* files are ``0400`` (Docker: bind-mounted read-only at ``/run/secrets``,
  owned by the container user) or ``0600`` (subprocess/venv runtimes: the
  folder's path is ``MESHFORGE_SECRETS_DIR``); the folder becomes ``0500``.

:meth:`SecretVault.wipe` overwrites every file with zeros, unlinks it and
removes the folder; the executor calls it when the attempt ends on **every**
path (success, failure, cancel, agent shutdown), and :meth:`SecretVault.sweep`
removes what a crashed agent left at the next start.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
import sys
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


class SecretFilesError(MeshForgeError):
    """Secrets cannot be checked or materialised safely (typed per instance)."""

    default_failure_type = FailureType.CONFIG_ERROR


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
        base = self.base
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            base.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise SecretFilesError(
                f"cannot create the secrets folder: {exc.strerror or exc}"
            ) from None
        info = os.lstat(base)
        if not stat.S_ISDIR(info.st_mode) or (
            hasattr(os, "getuid") and info.st_uid != os.getuid()
        ):
            raise SecretFilesError(
                "the secrets folder is not a directory owned by this user "
                "(a symlink or another user's folder); refusing to write secrets"
            )
        if os.name == "posix":
            os.chmod(base, 0o700)
        return base

    def materialise(
        self,
        attempt_id: str,
        values: Mapping[str, bytes],
        *,
        read_only: bool,
        owner: tuple[int, int] | None = None,
    ) -> Path:
        """Write ``values`` to ``<base>/<attempt_id>/NAME``; returns the folder."""
        self._ensure_base()
        folder = self.folder(attempt_id)
        if folder.exists() or folder.is_symlink():
            self._wipe_folder(folder)
        folder.mkdir(mode=0o700)
        mode = 0o400 if read_only else 0o600
        try:
            for name, value in values.items():
                path = folder / name
                fd = os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                with os.fdopen(fd, "wb") as fh:
                    fh.write(value)
                if owner is not None:
                    os.chown(path, *owner)
                os.chmod(path, mode)
            if owner is not None:
                os.chown(folder, *owner)
            if os.name == "posix":
                os.chmod(folder, 0o500)
        except BaseException:
            self._wipe_folder(folder)
            raise
        return folder

    def wipe(self, folder: Path | None) -> None:
        """Zero, unlink and remove one attempt's folder (never raises)."""
        if folder is None:
            return
        try:
            self._wipe_folder(folder)
        except OSError:
            log.warning("agent.execution.secret_wipe_failed", attempt_id=folder.name)

    def sweep(self) -> int:
        """Remove every attempt folder (a crashed agent's); returns how many."""
        base = self.base
        try:
            entries = list(base.iterdir())
        except OSError:
            return 0
        for entry in entries:
            self.wipe(entry)
        if entries:
            log.info("agent.execution.secrets_swept", count=len(entries))
        return len(entries)

    def _wipe_folder(self, folder: Path) -> None:
        if folder.is_symlink():
            folder.unlink()
            return
        if not folder.exists():
            return
        with contextlib.suppress(OSError):
            os.chmod(folder, 0o700)
        for path in list(folder.iterdir()):
            with contextlib.suppress(OSError):
                if path.is_file() and not path.is_symlink():
                    os.chmod(path, 0o600)
                    size = path.stat().st_size
                    with open(path, "r+b", buffering=0) as fh:
                        remaining = size
                        while remaining > 0:
                            n = min(remaining, _WIPE_CHUNK)
                            fh.write(b"\0" * n)
                            remaining -= n
                        os.fsync(fh.fileno())
            with contextlib.suppress(FileNotFoundError):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        folder.rmdir()


def agent_tag(data_dir: Path) -> str:
    """12 hex chars naming one agent's data dir (never the path itself)."""
    return hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:12]
