"""A local wheelhouse with the benchmark jobs' pinned numpy (V0.10 TD1, D288).

The Mode 3 cluster tests run the benchmark library's ``split.image_classify``
job, whose ``requirements.txt`` pins ``numpy`` (``NUMPY_REQUIREMENT``). Each
test agent builds that env with ``uv``; pointed at the package index, every
run needs pypi.org. Instead the tests give the agents ``[agent.envs]
find_links = <wheelhouse>`` + ``no_index = true`` - the product's own offline-lab
setting - and this module provides the wheelhouse:

* the wheel is the one **``uv.lock``** names for that numpy version and this
  interpreter's best-matching platform tag (``packaging.tags.sys_tags``), so
  its URL and sha256 come from the lock, not from a search;
* it is kept in a **cache** - ``$MESHFORGE_TEST_WHEELHOUSE`` if set, else
  ``$XDG_CACHE_HOME/meshforge-tests/wheelhouse`` (``~/.cache/...``) - one
  folder per wheel (``find_links`` must list exactly that wheel: the agent's
  env hash covers the folder's file names and sizes); a cached wheel is used
  only when its sha256 matches the lock, so the cache never needs a network;
* on a miss it is **downloaded once** (public CA roots, hash and size checked,
  written to a uniquely named ``.part`` beside the folder - ``mkstemp``, so
  two callers in one process never share it - and moved in atomically); with
  neither a cached wheel nor the network the caller gets a reason to skip,
  never a half-written folder;
* a fresh download that does **not** match the lock is a **hard failure**
  (:class:`WheelDownloadMismatchError`, never a skip): the index served other
  bytes than ``uv.lock`` pins, or the lock is wrong - either way someone must
  look. A *cached* wheel that does not match is only cache corruption and is
  re-downloaded;
* before the folder is returned it is checked to hold **exactly** the wheel
  (V0.10 TD2, D-2). Stray files are **removed**: the folder is this module's
  own (named after the wheel's sha256, nothing else writes there), and a
  stray file would be served to the agents' ``uv`` as another candidate (a
  different wheel could be installed in place of the locked one) and would
  change their env hash. A stray **directory** is not deleted - the caller
  gets a skip reason naming it.

To prepare an offline machine: run the tests once with network, or copy the
cache folder from a machine that did (same OS / CPU / Python).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "CACHE_ENV",
    "LockedWheel",
    "WheelDownloadMismatchError",
    "WheelhouseUnavailableError",
    "cache_root",
    "ensure_wheel",
    "locked_wheels",
    "pick_wheel",
]

CACHE_ENV: Final = "MESHFORGE_TEST_WHEELHOUSE"
"""Overrides the cache root (e.g. a shared, pre-filled folder in a lab)."""
REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DOWNLOAD_TIMEOUT_S: Final = 120.0


class WheelhouseUnavailableError(Exception):
    """No verified wheel in the cache and it could not be downloaded; the
    message is the skip reason."""


class WheelDownloadMismatchError(Exception):
    """A freshly downloaded wheel does not match ``uv.lock`` (sha256 or size):
    a hard failure, never a skip (module docstring)."""


@dataclass(frozen=True, slots=True)
class LockedWheel:
    """One ``wheels`` entry of a ``uv.lock`` package."""

    url: str
    sha256: str
    size: int | None

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]


def locked_wheels(
    name: str, version: str, lock: Path = REPO_ROOT / "uv.lock"
) -> list[LockedWheel]:
    """The wheels ``uv.lock`` records for ``name==version`` ([] if none)."""
    document = tomllib.loads(lock.read_text(encoding="utf-8"))
    for package in document.get("package", []):
        if package.get("name") == name and package.get("version") == version:
            out: list[LockedWheel] = []
            for wheel in package.get("wheels", []):
                algorithm, _, digest = str(wheel["hash"]).partition(":")
                if algorithm != "sha256" or not digest:
                    continue
                size = wheel.get("size")
                out.append(
                    LockedWheel(
                        url=str(wheel["url"]),
                        sha256=digest,
                        size=int(size) if size is not None else None,
                    )
                )
            return out
    return []


def pick_wheel(
    wheels: Iterable[LockedWheel], tags: Iterable[object] | None = None
) -> LockedWheel | None:
    """The wheel an installer would choose for this interpreter: the first of
    ``tags`` (most specific first; default :func:`packaging.tags.sys_tags`)
    that some wheel supports."""
    from packaging.tags import sys_tags
    from packaging.utils import parse_wheel_filename

    candidates = list(wheels)
    supported = {
        wheel.filename: parse_wheel_filename(wheel.filename)[3] for wheel in candidates
    }
    for tag in sys_tags() if tags is None else tags:
        for wheel in candidates:
            if tag in supported[wheel.filename]:
                return wheel
    return None


def cache_root() -> Path:
    """``$MESHFORGE_TEST_WHEELHOUSE``, else ``<XDG cache>/meshforge-tests/wheelhouse``."""
    configured = os.environ.get(CACHE_ENV)
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(xdg) / "meshforge-tests" / "wheelhouse"


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _verified(path: Path, wheel: LockedWheel) -> bool:
    try:
        if wheel.size is not None and path.stat().st_size != wheel.size:
            return False
        return _sha256(path) == wheel.sha256
    except OSError:
        return False


def _download(url: str, dest: Path) -> None:
    """``url`` -> ``dest`` over HTTPS with the **public** CA roots (the test
    process's ``SSL_CERT_FILE`` names the test fabric's CA)."""
    import ssl

    import certifi
    import httpx

    context = ssl.create_default_context(cafile=certifi.where())
    with (
        httpx.stream(
            "GET",
            url,
            verify=context,
            timeout=DOWNLOAD_TIMEOUT_S,
            follow_redirects=True,
        ) as resp,
        dest.open("wb") as fh,
    ):
        resp.raise_for_status()
        for block in resp.iter_bytes():
            fh.write(block)


def _only_the_wheel(folder: Path, wheel: LockedWheel) -> None:
    """Make ``folder`` hold exactly ``wheel.filename`` (module docstring):
    stray files and links are removed; a stray directory is a skip reason.
    A ``folder`` that is itself a symlink is refused (iterating it would delete
    files outside the cache - TD2 review N2)."""
    if folder.is_symlink():
        raise WheelhouseUnavailableError(
            f"the wheelhouse folder {folder} is a symlink; remove it"
        )
    for entry in folder.iterdir():
        if entry.name == wheel.filename:
            continue
        try:
            mode = entry.lstat().st_mode
        except FileNotFoundError:
            continue  # removed by a concurrent caller
        if stat.S_ISDIR(mode):
            raise WheelhouseUnavailableError(
                f"the wheelhouse folder {folder} must hold only {wheel.filename} "
                f"but also holds the directory {entry.name!r}; remove it"
            )
        _unlink_shared(entry)


def _unlink_shared(entry: Path) -> None:
    """``unlink`` that tolerates a concurrent caller deleting the same entry.

    Windows refuses a delete (``PermissionError``) while another caller's
    delete of that file is still pending; the file is gone a moment later.
    """
    try:
        entry.unlink(missing_ok=True)
    except PermissionError:
        if sys.platform != "win32":
            raise
        deadline = time.monotonic() + 5.0
        while os.path.lexists(entry):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.01)


def ensure_wheel(
    wheel: LockedWheel,
    root: Path | None = None,
    *,
    download: Callable[[str, Path], None] = _download,
) -> Path:
    """The folder holding exactly ``wheel`` (verified), filling the cache on a
    miss; raises :class:`WheelhouseUnavailableError` with the skip reason, or
    :class:`WheelDownloadMismatchError` when a fresh download is not the
    locked wheel."""
    base = cache_root() if root is None else root
    folder = base / wheel.sha256[:16]
    target = folder / wheel.filename
    if _verified(target, wheel):
        _only_the_wheel(folder, wheel)
        return folder
    folder.mkdir(parents=True, exist_ok=True)
    # beside the folder, not in it (the folder only ever holds the wheel), and
    # unique per call: same-process callers never write one file (TD2, D-3)
    handle, name = tempfile.mkstemp(
        prefix=f".{wheel.filename}.", suffix=".part", dir=base
    )
    os.close(handle)
    partial = Path(name)
    moved = False
    try:
        try:
            download(wheel.url, partial)
        except Exception as exc:  # any failure here means "no network"
            raise WheelhouseUnavailableError(
                f"{wheel.filename} is not in the wheelhouse cache ({folder}) and "
                f"could not be downloaded ({type(exc).__name__}: {exc}); run once "
                f"with network or set {CACHE_ENV} to a folder that holds it"
            ) from exc
        if not _verified(partial, wheel):
            got = partial.stat().st_size if partial.exists() else None
            raise WheelDownloadMismatchError(
                f"the downloaded {wheel.filename} does not match uv.lock "
                f"(sha256 {wheel.sha256[:16]}..., size {wheel.size}; got "
                f"{got} bytes) from {wheel.url} - the index served other bytes "
                "than the lock pins, or uv.lock is wrong"
            )
        os.chmod(partial, 0o644)  # mkstemp makes it 0600; a lab cache is shared
        try:
            os.replace(partial, target)
        except PermissionError:
            # Windows: a concurrent caller holds the target open (hashing it), so
            # it cannot be replaced. That caller placed the good wheel already,
            # so this call has nothing to add; anything else is a real error.
            if not _verified(target, wheel):
                raise
        else:
            moved = True
    finally:
        if not moved:  # once moved the name is free and may be another call's
            with contextlib.suppress(OSError):
                partial.unlink(missing_ok=True)
    _only_the_wheel(folder, wheel)
    return folder
