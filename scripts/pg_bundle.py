"""The controller bundle's PostgreSQL: fetch, verify, extract, trim (V0.17 G2, D390/D398).

Moved here from the SP2 spike (``lab/spikes/_sp2/bundle.py`` now re-exports it).
Build-time tooling: the controller itself never downloads anything.

**Pin** (``packaging/pins.toml [postgresql.<target>]``): the sha256 of the
archive is its identity - the upstream release assets have been rebuilt in
place before (same tag, new upload). Q19: the archive is mirrored as a release
asset of our own repository (``mirror_url``, tried first) so a build never
depends on the upstream project staying up; the upstream ``url`` is the
fallback. Both must yield the pinned bytes; anything else is refused.

**Cache** (``--cache``, ``$MESHFORGE_PG_CACHE`` or ``<repo>/.cache/meshforge-pg``)::

    <cache>/downloads/<archive>              verified archive
    <cache>/<sha256[:12]>/full/<root>/       extracted, untouched
    <cache>/<sha256[:12]>/trimmed/           what the controller bundle ships

**Trim rule** (sizes in ``lab/spikes/SP2.md``): keep the server and the programs
the managed mode and a future major upgrade need, the server modules initdb and
our encoding conversions use, ``share/`` minus docs and contrib extensions, and
the licence files (``LICENSE``, ``COPYRIGHT``, ``dependency-notices/``); then
copy exactly the shared libraries those ELF files load (``ldd`` closure inside
the bundle). Dropped: ``psql`` (links GNU readline, GPL-3, and is not needed),
contrib, PL/Python and its bundled CPython, headers, static libraries, ECPG.

Usage (a developer checkout's managed mode, ``[controller.db] bin_dir``)::

    uv run python scripts/pg_bundle.py --out .cache/pg     # prints <out>/pgsql/bin
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_SCRIPTS: Final = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:  # loaded by path (tests) rather than run
    sys.path.insert(0, str(_SCRIPTS))

from bundle_lib import BundleError, load_pins  # noqa: E402

__all__ = [
    "BIN_KEEP",
    "PGSQL_DIR_NAME",
    "BundleError",
    "PgPin",
    "TrimmedBundle",
    "cache_root",
    "dir_size",
    "drop_library_exec_bits",
    "ensure_trimmed",
    "fetch",
    "install_pgsql",
    "pg_pin",
    "sha256_file",
    "trim",
]

REPO_ROOT: Final = _SCRIPTS.parent
PINS: Final = REPO_ROOT / "packaging" / "pins.toml"
PGSQL_DIR_NAME: Final = "pgsql"
"""``<bundle>/pgsql``; equal to ``meshforge.controller.db_managed.runtime.PGSQL_DIR_NAME``."""

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
MARKER: Final = ".meshforge-trimmed.json"
_LDD_LINE: Final = re.compile(r"^\s*(\S+)\s+=>\s+(/.+?)\s+\(0x[0-9a-f]+\)\s*$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


# ----------------------------------------------------------------- the pin
@dataclass(frozen=True, slots=True)
class PgPin:
    """One pinned PostgreSQL archive (``[postgresql.<target>]``)."""

    version: str
    urls: tuple[str, ...]
    """Tried in order: our mirror (Q19), then upstream."""
    sha256: str
    archive_root: str

    @property
    def file_name(self) -> str:
        return self.urls[-1].rsplit("/", 1)[-1]


def pg_pin(pins: Mapping[str, object], target: str = "linux-x86_64") -> PgPin:
    """``[postgresql.<target>]`` as a :class:`PgPin`; a missing or malformed
    pin is refused (``bad_pins``), never guessed."""
    table = pins.get("postgresql")
    entry = table.get(target) if isinstance(table, dict) else None
    version = table.get("version") if isinstance(table, dict) else None
    if not isinstance(entry, dict):
        raise BundleError(
            f"packaging/pins.toml has no [postgresql.{target}]", reason="bad_pins"
        )
    url, mirror = entry.get("url"), entry.get("mirror_url")
    digest, root = entry.get("sha256"), entry.get("archive_root")
    urls = [u for u in (mirror, url) if u is not None]
    if not (
        isinstance(version, str)
        and isinstance(url, str)
        and all(isinstance(u, str) and u.startswith("https://") for u in urls)
        and not any("latest" in str(u) for u in urls)
        and isinstance(digest, str)
        and _SHA256.match(digest)
        and isinstance(root, str)
        and root
        and "/" not in root
    ):
        raise BundleError(
            f"[postgresql.{target}] needs version, https url (+ optional "
            "mirror_url), a 64-hex sha256 and archive_root",
            reason="bad_pins",
        )
    return PgPin(version, tuple(str(u) for u in urls), digest, root)


def cache_root() -> Path:
    env = os.environ.get("MESHFORGE_PG_CACHE")
    return Path(env) if env else REPO_ROOT / ".cache" / "meshforge-pg"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, partial: Path, timeout_s: float) -> None:
    with (
        urllib.request.urlopen(url, timeout=timeout_s) as resp,  # noqa: S310 - pinned https URL
        partial.open("wb") as out,
    ):
        shutil.copyfileobj(resp, out, 1 << 20)


def fetch(pin: PgPin, cache: Path, *, timeout_s: float = 300.0) -> Path:
    """The verified archive (downloaded once).

    A cached file with a wrong hash is deleted and fetched again. Each URL is
    tried in order; a URL that fails *or* yields other bytes is skipped (the
    mismatch is reported); if none yields the pinned bytes the build fails.
    Network errors of the last URL propagate (``URLError``) so a caller (the
    tests) can tell "offline" from "wrong bytes" (``BundleError``).
    """
    downloads = cache / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    target = downloads / pin.file_name
    if target.exists():
        if sha256_file(target) == pin.sha256:
            return target
        target.unlink()
    partial = target.with_name(target.name + ".partial")
    mismatches: list[str] = []
    last_error: OSError | None = None
    for url in pin.urls:
        try:
            _download(url, partial, timeout_s)
        except OSError as exc:  # URLError/HTTPError/timeouts are OSErrors
            partial.unlink(missing_ok=True)
            last_error = exc
            print(f"pg_bundle: {url}: {exc} (trying the next source)", file=sys.stderr)
            continue
        actual = sha256_file(partial)
        if actual == pin.sha256:
            os.replace(partial, target)
            return target
        partial.unlink()
        mismatches.append(f"{url}: got {actual}")
    if mismatches:
        raise BundleError(
            f"sha256 mismatch (pinned {pin.sha256}): " + "; ".join(mismatches),
            reason="sha256_mismatch",
        )
    assert last_error is not None  # noqa: S101 - at least one URL was tried
    raise last_error


# ----------------------------------------------------------------- trimming
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
        raise BundleError(
            "ldd not found (glibc's libc-bin); needed to trim the bundle",
            reason="no_ldd",
        )
    for path in files:
        done = subprocess.run(
            [ldd, str(path)],
            capture_output=True,
            text=True,
            check=False,
            env={"LC_ALL": "C", "PATH": os.defpath},
        )
        if "not found" in done.stdout:
            raise BundleError(
                f"unresolved library in {path.name}: {done.stdout}",
                reason="unresolved_library",
            )
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
        raise BundleError(f"{out_root} already exists", reason="exists")
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
    for soname, real in sorted(_ldd_closure(elf, full_root / "lib").items()):
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
    if not any((out_root / name).exists() for name in ("LICENSE", "COPYRIGHT")):
        # The PostgreSQL licence must travel with the binaries (licence review).
        raise BundleError(
            f"{full_root} has neither LICENSE nor COPYRIGHT", reason="licence"
        )


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
    version: str = ""
    sha256: str = ""

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"


def ensure_trimmed(pin: PgPin, cache: Path | None = None) -> TrimmedBundle:
    """Fetch + extract + trim once; later calls reuse the cache."""
    cache = cache or cache_root()
    archive = fetch(pin, cache)
    base = cache / pin.sha256[:12]
    full_root = base / "full" / pin.archive_root
    trimmed = base / "trimmed"
    marker = trimmed / MARKER
    if not full_root.exists():
        tmp = base / "full.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        _extract(archive, tmp)
        shutil.rmtree(base / "full", ignore_errors=True)
        os.replace(tmp, base / "full")
    if not marker.exists():
        tmp_trim = base / "trimmed.tmp"
        shutil.rmtree(tmp_trim, ignore_errors=True)
        shutil.rmtree(trimmed, ignore_errors=True)
        trim(full_root, tmp_trim)
        (tmp_trim / MARKER).write_text(
            json.dumps({"sha256": pin.sha256, "version": pin.version}) + "\n"
        )
        os.replace(tmp_trim, trimmed)
    return TrimmedBundle(
        root=trimmed,
        full_root=full_root,
        archive=archive,
        archive_bytes=archive.stat().st_size,
        full_bytes=dir_size(full_root),
        trimmed_bytes=dir_size(trimmed),
        version=pin.version,
        sha256=pin.sha256,
    )


def install_pgsql(trimmed: TrimmedBundle, bundle_root: Path) -> Path:
    """Copy the trimmed tree to ``<bundle_root>/pgsql`` (without the cache
    marker); returns that folder. Modes and mtimes are normalised later by the
    bundle builder, like every other file."""
    dest = bundle_root / PGSQL_DIR_NAME
    if dest.exists():
        raise BundleError(f"{dest} already exists", reason="exists")
    shutil.copytree(
        trimmed.root,
        dest,
        symlinks=True,
        ignore=lambda _dir, names: [n for n in names if n == MARKER],
    )
    if os.name != "nt":
        drop_library_exec_bits(dest / "lib")
    return dest


def drop_library_exec_bits(lib_dir: Path) -> int:
    """Shared objects in ``lib_dir`` lose their execute bits (0755 -> 0644);
    returns how many changed.

    The upstream archive ships some of them executable (``libpq.so.5``, the
    conversion modules); ``normalise_tree`` keeps an existing execute bit, and
    lintian refuses an executable shared library (``shared-library-is-executable``,
    an error). ``dlopen`` and the dynamic loader never need the bit, so this is
    a pure packaging fix. Symlinks and subfolders are left alone."""
    changed = 0
    if not lib_dir.is_dir():
        return changed
    for item in sorted(lib_dir.iterdir()):
        if item.is_symlink() or not item.is_file():
            continue
        mode = item.stat().st_mode & 0o7777
        if mode & 0o111:
            item.chmod(mode & ~0o111)
            changed += 1
    return changed


# ----------------------------------------------------------------------- CLI
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="folder to put pgsql/ in (a developer's [controller.db] bin_dir)",
    )
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--target", default="linux-x86_64")
    parser.add_argument("--pins", type=Path, default=PINS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        pin = pg_pin(load_pins(args.pins), args.target)
        trimmed = ensure_trimmed(pin, args.cache)
        out = args.out.resolve()
        out.mkdir(parents=True, exist_ok=True)
        dest = out / PGSQL_DIR_NAME
        if dest.exists():
            shutil.rmtree(dest)
        install_pgsql(trimmed, out)
    except BundleError as exc:
        print(f"pg_bundle: error ({exc.reason}): {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"pg_bundle: error (download_failed): {exc}", file=sys.stderr)
        return 1
    print(dest / "bin")
    return 0


if __name__ == "__main__":
    sys.exit(main())
