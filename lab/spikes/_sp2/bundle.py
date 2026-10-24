"""Spike SP2: fetch, verify, extract and trim the pinned PostgreSQL build.

Build-time tooling (the controller never downloads). G2/SP3 move it into
``scripts/build_bundle.py``. Cache: ``$MESHFORGE_PG_CACHE`` or
``<repo>/.cache/meshforge-pg`` (``.cache/`` is gitignored)::

    <cache>/downloads/<archive>              verified archive (sha256 pin)
    <cache>/<sha256[:12]>/full/<root>/       extracted, untouched
    <cache>/<sha256[:12]>/trimmed/           what the controller package ships

**Trim rule** (sizes in ``lab/spikes/SP2.md``): keep the server and the programs
the managed mode and a future major upgrade need, the server modules initdb and
our encoding conversions use, ``share/`` minus docs and contrib extensions, and the
licence files; then copy exactly the shared libraries those ELF files load
(``ldd`` closure inside the bundle). Dropped: ``psql`` (links GNU readline,
GPL-3, and is not needed), contrib, PL/Python and its bundled CPython, headers,
static libraries, ECPG.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meshforge.controller.db_managed.binaries import PgBuild

__all__ = [
    "BIN_KEEP",
    "TrimmedBundle",
    "cache_root",
    "dir_size",
    "ensure_trimmed",
    "fetch",
    "sha256_file",
    "trim",
]

REPO_ROOT: Final = Path(__file__).resolve().parents[3]

BIN_KEEP: Final = (
    "postgres",
    "initdb",
    "pg_ctl",  # pg_upgrade runs it
    "pg_dump",
    "pg_restore",
    "pg_controldata",  # pg_upgrade + diagnostics
    "pg_resetwal",  # pg_upgrade
    "pg_upgrade",
)
MODULE_KEEP: Final = (
    "plpgsql.so",  # initdb: CREATE EXTENSION plpgsql
    "dict_snowball.so",  # initdb: snowball_create.sql
    "*_and_*.so",  # encoding conversions
    "euc2004_sjis2004.so",
)
SHARE_DROP: Final = (
    "doc",
    "psqlrc.sample",
    "pg_service.conf.sample",
    "fix-CVE-*.sql",
)
EXTENSION_KEEP: Final = ("plpgsql.control", "plpgsql--*.sql")
TOP_KEEP: Final = (
    "LICENSE",
    "COPYRIGHT",
    "dependency-notices",
    "build-manifest.json",
    "source-input.json",
)
_MARKER: Final = ".meshforge-trimmed.json"
_LDD_LINE: Final = re.compile(r"^\s*(\S+)\s+=>\s+(/.+?)\s+\(0x[0-9a-f]+\)\s*$")


def cache_root() -> Path:
    env = os.environ.get("MESHFORGE_PG_CACHE")
    return Path(env) if env else REPO_ROOT / ".cache" / "meshforge-pg"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BundleError(RuntimeError):
    """Download, checksum or trim failed (never retried silently)."""


def fetch(build: PgBuild, cache: Path, *, timeout_s: float = 300.0) -> Path:
    """The verified archive (downloaded once). A cached file with a wrong hash
    is deleted and fetched again; a fresh download with a wrong hash is an error."""
    downloads = cache / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    target = downloads / build.url.rsplit("/", 1)[-1]
    if target.exists():
        if sha256_file(target) == build.sha256:
            return target
        target.unlink()
    partial = target.with_name(target.name + ".partial")
    with (
        urllib.request.urlopen(build.url, timeout=timeout_s) as resp,  # noqa: S310 - pinned https URL
        partial.open("wb") as out,
    ):
        shutil.copyfileobj(resp, out, 1 << 20)
    actual = sha256_file(partial)
    if actual != build.sha256:
        partial.unlink()
        raise BundleError(
            f"sha256 mismatch for {build.url}: got {actual}, pinned {build.sha256}"
        )
    os.replace(partial, target)
    return target


def _extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tar:
        tar.extractall(dest, filter="data")


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _ldd_closure(files: list[Path], lib_dir: Path) -> dict[str, Path]:
    """soname -> real file inside ``lib_dir`` for every library ``files`` load."""
    wanted: dict[str, Path] = {}
    lib_real = lib_dir.resolve()
    ldd = shutil.which("ldd")
    if ldd is None:
        raise BundleError("ldd not found (glibc's libc-bin); needed to trim the bundle")
    for path in files:
        done = subprocess.run(
            [ldd, str(path)],
            capture_output=True,
            text=True,
            check=False,
            env={"LC_ALL": "C", "PATH": os.defpath},
        )
        if "not found" in done.stdout:
            raise BundleError(f"unresolved library in {path.name}: {done.stdout}")
        for line in done.stdout.splitlines():
            match = _LDD_LINE.match(line)
            if match is None:
                continue
            soname, resolved = match.group(1), Path(match.group(2)).resolve()
            if resolved.parent == lib_real:
                wanted[soname] = resolved
    return wanted


def trim(full_root: Path, out_root: Path) -> None:
    """Copy the kept subset of ``full_root`` into ``out_root`` (must not exist)."""
    if out_root.exists():
        raise BundleError(f"{out_root} already exists")
    (out_root / "bin").mkdir(parents=True)
    (out_root / "lib").mkdir()
    elf: list[Path] = []
    for name in BIN_KEEP:
        src = full_root / "bin" / name
        shutil.copy2(src, out_root / "bin" / name)
        elf.append(src)
    for src in sorted((full_root / "lib").iterdir()):
        if (
            src.suffix == ".so"
            and not src.is_symlink()
            and _matches(src.name, MODULE_KEEP)
        ):
            shutil.copy2(src, out_root / "lib" / src.name)
            elf.append(src)
    for soname, real in _ldd_closure(elf, full_root / "lib").items():
        shutil.copy2(real, out_root / "lib" / soname)
    share_out = out_root / "share"
    shutil.copytree(
        full_root / "share",
        share_out,
        symlinks=True,
        ignore=lambda _dir, names: [n for n in names if _matches(n, SHARE_DROP)],
    )
    ext = share_out / "extension"
    for item in ext.iterdir():
        if not _matches(item.name, EXTENSION_KEEP):
            item.unlink()
    for name in TOP_KEEP:
        src = full_root / name
        if src.is_dir():
            shutil.copytree(src, out_root / name, symlinks=True)
        elif src.exists():
            shutil.copy2(src, out_root / name)


def dir_size(path: Path) -> int:
    """Bytes of regular files under ``path`` (symlinks not followed)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            item = Path(root) / name
            if not item.is_symlink():
                total += item.stat().st_size
    return total


@dataclass(frozen=True, slots=True)
class TrimmedBundle:
    root: Path
    """Trimmed install root (``bin/``, ``lib/``, ``share/``)."""
    full_root: Path
    archive: Path
    archive_bytes: int
    full_bytes: int
    trimmed_bytes: int

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"


def ensure_trimmed(build: PgBuild, cache: Path | None = None) -> TrimmedBundle:
    """Fetch + extract + trim once; later calls reuse the cache."""
    cache = cache or cache_root()
    archive = fetch(build, cache)
    base = cache / build.sha256[:12]
    full_root = base / "full" / build.archive_root
    trimmed = base / "trimmed"
    marker = trimmed / _MARKER
    if not full_root.exists():
        tmp = base / "full.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        _extract(archive, tmp)
        os.replace(tmp, base / "full")
    if not marker.exists():
        tmp_trim = base / "trimmed.tmp"
        shutil.rmtree(tmp_trim, ignore_errors=True)
        shutil.rmtree(trimmed, ignore_errors=True)
        trim(full_root, tmp_trim)
        (tmp_trim / _MARKER).write_text(
            json.dumps({"sha256": build.sha256, "version": build.version}) + "\n"
        )
        os.replace(tmp_trim, trimmed)
    return TrimmedBundle(
        root=trimmed,
        full_root=full_root,
        archive=archive,
        archive_bytes=archive.stat().st_size,
        full_bytes=dir_size(full_root),
        trimmed_bytes=dir_size(trimmed),
    )
