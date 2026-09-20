"""``uv`` virtual environments with a private cache: the venv-fallback runtime
of ADR-0004 and the "warm env cache" of V0.5 criterion 5.

The pinned set is fixed in code so every laptop installs the same wheels
(the grpcio/pydantic pins are the ones ``uv.lock`` resolves for the project).
``UV_CACHE_DIR`` always points into the run's own temp directory: a *cold*
repetition gets a fresh empty cache (real download + unpack, needs network); a
*warm* repetition reuses one cache populated once in setup and installs
``--offline``, so the number means "warm cache", not "this laptop's history".
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from spikes._s6 import proc
from spikes._s6.proc import Outcome

type JSON = Any

IS_WINDOWS: Final = sys.platform == "win32"
PYTHON_REQUEST: Final = "3.12"
#: Fixed small set: a BLAS-backed array library, our RPC stack, our schemas.
PINNED_REQUIREMENTS: Final[tuple[str, ...]] = (
    "numpy==2.2.6",
    "grpcio==1.84.0",
    "pydantic==2.13.5",
)
REQUIREMENTS_FILE: Final = "requirements-s6.txt"
#: What ``venv-start`` runs with the venv interpreter.
START_SCRIPT: Final = "import numpy; import grpc; print('ok')"
_UV_ENV: Final = {
    "UV_NO_PROGRESS": "1",
    "UV_PYTHON_DOWNLOADS": "never",
    # never let the developer's project/config leak into the measurement
    "UV_NO_CONFIG": "1",
}


class VenvError(Exception):
    """A typed ``uv`` failure."""

    def __init__(self, failure_type: str, message: str, *, fix: str = "") -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.fix = fix


def find_uv() -> str | None:
    return shutil.which("uv")


def uv_version(uv: str | None, *, timeout: float = 30.0) -> str | None:
    if uv is None:
        return None
    out = proc.run([uv, "--version"], timeout=timeout)
    return out.stdout.strip() or None if out.ok else None


def uv_env(cache_dir: Path) -> dict[str, str]:
    return {**_UV_ENV, "UV_CACHE_DIR": str(cache_dir)}


def venv_python(venv_dir: Path) -> Path:
    if IS_WINDOWS:
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def site_packages(venv_dir: Path) -> Path | None:
    if IS_WINDOWS:
        candidate = venv_dir / "Lib" / "site-packages"
        return candidate if candidate.is_dir() else None
    lib = venv_dir / "lib"
    if not lib.is_dir():
        return None
    for entry in sorted(lib.iterdir()):
        sp = entry / "site-packages"
        if entry.name.startswith("python") and sp.is_dir():
            return sp
    return None


def dir_bytes(path: Path | None) -> int | None:
    """Total size of the regular files under ``path`` (no symlink following)."""
    if path is None or not path.is_dir():
        return None
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                info = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _classify(outcome: Outcome, *, what: str) -> VenvError:
    if outcome.error:
        return VenvError(
            "uv_cli_missing", outcome.error, fix="install uv (astral.sh/uv)"
        )
    if outcome.timed_out:
        return VenvError(
            "timeout", f"{what}: {outcome.describe()}", fix="rerun with --timeout"
        )
    low = f"{outcome.stderr}\n{outcome.stdout}".lower()
    detail = outcome.describe()[-500:]
    if "no interpreter found" in low or "no such python" in low:
        return VenvError(
            "python_not_found",
            f"{what}: {detail}",
            fix=f"uv python install {PYTHON_REQUEST}",
        )
    if (
        "network" in low
        or "failed to fetch" in low
        or "error sending request" in low
        or "dns error" in low
        or "connection" in low
        or "offline" in low
    ):
        return VenvError(
            "network_error", f"{what}: {detail}", fix="check the network / proxy"
        )
    return VenvError("uv_failed", f"{what}: {detail}", fix="see the message")


def python_available(uv: str, *, cwd: Path, timeout: float = 60.0) -> str | None:
    """The 3.12 interpreter ``uv`` would use (no download), or ``None``."""
    out = proc.run(
        [uv, "python", "find", PYTHON_REQUEST], timeout=timeout, env=_UV_ENV, cwd=cwd
    )
    return out.stdout.strip() or None if out.ok else None


def write_requirements(directory: Path) -> Path:
    path = directory / REQUIREMENTS_FILE
    path.write_text("\n".join(PINNED_REQUIREMENTS) + "\n", encoding="utf-8")
    return path


def create_venv(uv: str, venv_dir: Path, *, cache_dir: Path, timeout: float) -> float:
    """``uv venv --python 3.12 DIR``; returns the elapsed seconds."""
    out = proc.run(
        [uv, "venv", "--python", PYTHON_REQUEST, str(venv_dir)],
        timeout=timeout,
        env=uv_env(cache_dir),
        cwd=venv_dir.parent,
    )
    if not out.ok:
        raise _classify(out, what="uv venv")
    return out.elapsed_s


def install(
    uv: str,
    venv_dir: Path,
    requirements: Path,
    *,
    cache_dir: Path,
    offline: bool,
    timeout: float,
) -> float:
    """``uv pip install --python <venv python> -r requirements``; seconds."""
    argv = [uv, "pip", "install", "--python", str(venv_python(venv_dir))]
    if offline:
        argv.append("--offline")
    argv += ["-r", str(requirements)]
    out = proc.run(argv, timeout=timeout, env=uv_env(cache_dir), cwd=venv_dir.parent)
    if not out.ok:
        raise _classify(out, what="uv pip install")
    return out.elapsed_s


def start_process(venv_dir: Path, *, timeout: float) -> Outcome:
    """The venv interpreter importing the installed packages (``venv-start``)."""
    return proc.run([str(venv_python(venv_dir)), "-c", START_SCRIPT], timeout=timeout)


def _force_writable(
    func: Callable[[str], object], path: str, _exc: BaseException
) -> None:
    """``shutil.rmtree`` ``onexc``: make the entry and its directory writable
    (Windows marks cached files read-only; a read-only directory blocks unlink
    on POSIX) and retry once."""
    target = Path(path)
    for entry in (target, target.parent):
        with contextlib.suppress(OSError):
            os.chmod(entry, stat.S_IRWXU)
    with contextlib.suppress(OSError):
        func(path)


def _make_writable(path: Path) -> None:
    for root, dirs, files in os.walk(path):
        for name in (*dirs, *files):
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IRWXU)


def rmtree(path: Path) -> None:
    """Remove ``path`` even with read-only files (Windows); never raises."""
    if not path.exists():
        return
    with contextlib.suppress(OSError):
        shutil.rmtree(path, onexc=_force_writable)
    if path.exists():  # a read-only directory tree: fix permissions, retry once
        _make_writable(path)
        with contextlib.suppress(OSError):
            shutil.rmtree(path, onexc=_force_writable)
