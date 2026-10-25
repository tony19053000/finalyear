"""Pure parts of the D384 bundle builder (``scripts/build_bundle.py``).

No subprocesses and no network: OS layouts, launcher text, the relocation
rewrites (``#!`` lines, wheel ``RECORD`` rows, ``/install`` placeholders), the
build-path leak scan, the manifest (``MANIFEST.sha256`` + ``BUILDINFO.json``),
the pins file and the reproducible ``.tar.xz``. Unit-tested on their own
(``tests/unit/test_v017_sp3_bundle_lib.py``, ``tests/unit/test_v017_g1_bundle.py``).
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import lzma
import os
import re
import stat
import tarfile
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

MANIFEST_NAME: Final = "MANIFEST.sha256"
BUILDINFO_NAME: Final = "BUILDINFO.json"
"""Read at run time by ``meshforge.common.buildinfo`` (same name there)."""
MANIFEST_FORMAT: Final = 1
PLACEHOLDER: Final = "/install"
"""python-build-standalone's build prefix, which uv replaces on install."""


class BundleError(Exception):
    """A build or verify step failed; ``reason`` is a stable short code."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# --------------------------------------------------------------------- layout
@dataclass(frozen=True, slots=True)
class Layout:
    """Everything that differs between operating systems (paths relative to the
    bundle root, always ``/``-separated)."""

    os_name: str
    """``linux`` / ``windows`` (the bundle name part)."""
    python_exe: str
    scripts_dir: str
    stdlib_dir: str
    site_packages: str
    uv_name: str
    launcher_suffix: str
    python_platform: str
    """``uv pip install --python-platform``: fixes the wheel tags to the target OS."""
    posix: bool

    def site_packages_path(self, root: Path) -> Path:
        return root / self.site_packages


LINUX_X86_64: Final = Layout(
    os_name="linux",
    python_exe="python/bin/python3.12",
    scripts_dir="python/bin",
    stdlib_dir="python/lib/python3.12",
    site_packages="python/lib/python3.12/site-packages",
    uv_name="uv",
    launcher_suffix="",
    # Ubuntu 22.04 has glibc 2.35, 24.04 2.39 (D394); 2_28 also covers RHEL 8.
    python_platform="x86_64-manylinux_2_28",
    posix=True,
)


def layout_for(system: str, machine: str, *, experimental_windows: bool) -> Layout:
    """The layout for this build machine (the bundle is built on its target OS)."""
    arch = normalise_arch(machine)
    if system == "Linux":
        if arch != "x86_64":
            # aarch64 would only need its python_platform; no team machine has one.
            raise BundleError(f"no Linux layout for {arch} yet", reason="unsupported")
        return LINUX_X86_64
    if system == "Windows" and experimental_windows:
        return Layout(
            os_name="windows",
            python_exe="python/python.exe",
            scripts_dir="python/Scripts",
            stdlib_dir="python/Lib",
            site_packages="python/Lib/site-packages",
            uv_name="uv.exe",
            launcher_suffix=".cmd",
            python_platform="x86_64-pc-windows-msvc",
            posix=False,
        )
    raise BundleError(
        f"no bundle layout for {system} (Windows: build with --os windows there)",
        reason="unsupported",
    )


def normalise_arch(machine: str) -> str:
    lowered = machine.lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(
        lowered, lowered
    )


def bundle_name(version: str, layout: Layout, arch: str) -> str:
    return f"meshforge-{version}-{layout.os_name}-{arch}"


# ------------------------------------------------------------------ launchers
@dataclass(frozen=True, slots=True)
class EntryPoint:
    name: str
    module: str
    function: str


def script_names(text: str) -> set[str]:
    """Every ``console_scripts`` and ``gui_scripts`` name of an
    ``entry_points.txt`` (V0.17 G4: the Windows ``Scripts\\<name>.exe``
    trampolines an installer wrote for them name the build interpreter)."""
    names: set[str] = set()
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line.strip("[]").strip()
            continue
        if section in ("console_scripts", "gui_scripts") and "=" in line:
            names.add(line.partition("=")[0].strip())
    return names


def parse_entry_points(text: str) -> dict[str, EntryPoint]:
    """``[console_scripts]`` of a wheel's ``entry_points.txt``."""
    found: dict[str, EntryPoint] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            section = line.strip("[]").strip()
            continue
        if section != "console_scripts" or "=" not in line:
            continue
        name, _, target = (part.strip() for part in line.partition("="))
        module, _, function = target.partition(":")
        if not module or not function or "[" in function:
            raise BundleError(
                f"unsupported entry point {line!r}", reason="bad_entry_point"
            )
        found[name] = EntryPoint(name, module.strip(), function.strip())
    return found


_IDENT: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_COMMAND: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _launch_code(entry: EntryPoint, quote: str) -> str:
    """The ``-c`` program. ``argv[0]`` is set so usage lines name the command.
    ``quote`` is the Python string quote that cannot clash with the shell's own
    quoting of the whole program (sh: ``'...'`` around it, so ``"``; cmd: ``"``
    around it, so ``'``). Names are validated, so no escaping is needed."""
    if not (
        _IDENT.match(entry.module)
        and _IDENT.match(entry.function)
        and _COMMAND.match(entry.name)
    ):
        raise BundleError(f"unsafe entry point {entry!r}", reason="bad_entry_point")
    return (
        f"import sys; sys.argv[0] = {quote}{entry.name}{quote}; "
        f"from {entry.module} import {entry.function}; "
        f"sys.exit({entry.function}())"
    )


def launcher_posix(entry: EntryPoint, layout: Layout) -> str:
    """A ``/bin/sh`` launcher in ``bin/``: resolves its own real path (so a symlink
    from ``/usr/bin`` works), then runs the bundled interpreter isolated (``-I``:
    no ``PYTHON*`` variables, no user site, no cwd on ``sys.path``)."""
    python = "../" + layout.python_exe
    code = _launch_code(entry, quote='"')
    return (
        "#!/bin/sh\n"
        f"# {entry.name} - MeshForge bundle launcher, generated by "
        "scripts/build_bundle.py (do not edit).\n"
        "# Paths are relative to this file's real location: the bundle can move.\n"
        'here=$(dirname -- "$(realpath -- "$0")") || exit 127\n'
        f'exec "$here/{python}" -I -c \'{code}\' "$@"\n'
    )


def launcher_windows(entry: EntryPoint, layout: Layout) -> str:
    """A ``.cmd`` launcher (V0.17 G4; run on the ``package-windows`` CI job).
    ``%~dp0`` is the launcher's own folder; the code uses only ``'`` quotes so
    cmd's ``"`` quoting stays simple. ``.cmd``, not ``.exe``: an ``.exe`` would
    have to be a trampoline (uv's or distlib's), which names its interpreter in
    a binary trailer and cannot pass ``-I`` (G4 progress note, decision 2); the
    Start-menu entries run ``pythonw.exe`` directly."""
    python = "..\\" + layout.python_exe.replace("/", "\\")
    code = _launch_code(entry, quote="'")
    return (
        "@echo off\r\n"
        f"rem {entry.name} - MeshForge bundle launcher, generated by "
        "scripts/build_bundle.py (do not edit).\r\n"
        f'"%~dp0{python}" -I -c "{code}" %*\r\n'
        "exit /b %ERRORLEVEL%\r\n"
    )


def launcher_text(entry: EntryPoint, layout: Layout) -> str:
    return (
        launcher_posix(entry, layout)
        if layout.posix
        else launcher_windows(entry, layout)
    )


# ----------------------------------------------------------- shebang rewrite
def trampoline(python_name: str) -> bytes:
    """The relocatable ``#!`` header python-build-standalone gives ``pip``: sh runs
    the interpreter next to the script's real path; Python sees a string literal."""
    return (
        "#!/bin/sh\n"
        f'\'\'\'exec\' "$(dirname -- "$(realpath -- "$0")")/{python_name}" '
        '"$0" "$@"\n'
        "' '''\n"
    ).encode()


def rewrite_shebang(data: bytes, python_abs: bytes, python_name: str) -> bytes | None:
    """``data`` with an installer-written header that names ``python_abs``
    replaced by :func:`trampoline`; ``None`` if it has no such header. Handles the
    plain ``#!/abs/python`` form and the sh form installers use for paths with
    spaces."""
    if not data.startswith(b"#!"):
        return None
    lines = data.split(b"\n", 3)
    first = lines[0]
    if python_abs in first and first[2:].strip().startswith(python_abs):
        return trampoline(python_name) + data[len(first) + 1 :]
    if (
        first.strip() == b"#!/bin/sh"
        and len(lines) >= 3
        and lines[1].startswith(b"'''exec'")
        and python_abs in lines[1]
        and lines[2].strip() == b"' '''"
    ):
        header = len(lines[0]) + len(lines[1]) + len(lines[2]) + 3
        return trampoline(python_name) + data[header:]
    return None


def record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + digest.decode("ascii")


def update_record(
    record: str, site_packages: Path, changed: Mapping[Path, bytes | None]
) -> str:
    """A wheel ``RECORD`` with the rows of ``changed`` files rehashed (``bytes``) or
    dropped (``None``). Rows are matched by resolved path (RECORD paths are relative
    to site-packages, e.g. ``../../../bin/compute``)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    for row in csv.reader(io.StringIO(record)):
        if not row:
            continue
        target = Path(os.path.normpath(site_packages / row[0]))
        if target in changed:
            data = changed[target]
            if data is None:
                continue
            row = [row[0], record_hash(data), str(len(data))]
        writer.writerow(row)
    return out.getvalue()


# ---------------------------------------------------------------- path leaks
@dataclass(frozen=True, slots=True)
class Leak:
    path: str
    needle: str


_TOO_GENERIC: Final = frozenset({"/home", "/root", "/usr", "/tmp"})  # noqa: S108 - not opened
"""Paths too common to flag (``HOME=/root`` on CI would hit unrelated text)."""


def _windows_forms(path: str) -> set[str]:
    """A Windows path as tools write it: ``\\`` and ``/`` separators, either case
    of the drive letter (``C:\\x`` / ``c:/x``). Nothing for a POSIX path."""
    if len(path) < 3 or path[1] != ":" or not path[0].isalpha():
        return {path}
    forms: set[str] = set()
    for drive in (path[0].upper(), path[0].lower()):
        body = drive + path[1:]
        forms |= {body, body.replace("\\", "/"), body.replace("/", "\\")}
    return forms


def leak_needles(paths: Iterable[Path | str]) -> list[bytes]:
    """Byte strings that must not appear in a bundle: each path as given and
    resolved (Windows: in every separator/drive-case form), longest first,
    ignoring ones too short to be meaningful."""
    needles: set[str] = set()
    for item in paths:
        text = str(item).rstrip("/\\")
        for form in (text, os.path.realpath(text)):
            for variant in _windows_forms(form):
                if len(variant) >= 8 and variant not in _TOO_GENERIC:
                    needles.add(variant)
    return sorted((n.encode("utf-8", "surrogateescape") for n in needles), key=len)[
        ::-1
    ]


def find_leaks(root: Path, needles: Sequence[bytes]) -> list[Leak]:
    """Every regular file under ``root`` containing one of ``needles`` (first hit
    per file). Symlink targets are checked too."""
    leaks: list[Leak] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = os.readlink(path).encode("utf-8", "surrogateescape")
            hit = next((n for n in needles if n in target), None)
        elif path.is_file():
            data = path.read_bytes()
            hit = next((n for n in needles if n in data), None)
        else:
            continue
        if hit is not None:
            leaks.append(Leak(rel, hit.decode("utf-8", "surrogateescape")))
    return leaks


def replace_in_text_files(root: Path, old: bytes, new: bytes) -> list[str]:
    """Replace ``old`` with ``new`` in every non-binary file (no NUL byte) under
    ``root``; returns the changed files. Binaries are left for :func:`find_leaks`
    (a same-length patch of an ELF is not something to do silently)."""
    changed: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if old in data and b"\x00" not in data:
            path.write_bytes(data.replace(old, new))
            changed.append(path.relative_to(root).as_posix())
    return changed


# ------------------------------------------------------------------ manifest
# Two files at the bundle root (V0.17 G1, brief §5a item 1):
#
# * ``BUILDINFO.json`` - what was built from what (version, git sha, lock digest,
#   Python, ...); read at run time by ``meshforge.common.buildinfo``.
# * ``MANIFEST.sha256`` - every regular file (``BUILDINFO.json`` included), in
#   ``sha256sum`` format so ``sha256sum -c MANIFEST.sha256`` checks an installed
#   bundle with no MeshForge code at all. What ``sha256sum`` cannot express
#   (symlinks, empty directories, the executable bit) is recorded on ``#`` lines,
#   which ``sha256sum -c`` ignores (even with ``--strict``).
#
# Same commit + same lock -> byte-identical ``MANIFEST.sha256`` (and therefore an
# identical BUILDINFO.json, whose hash it contains).


@dataclass(slots=True)
class Manifest:
    """Build facts (``meta`` -> BUILDINFO.json) + the tree (``files``)."""

    meta: dict[str, object]
    files: list[dict[str, object]] = field(default_factory=list)

    def buildinfo_json(self) -> str:
        doc = {**self.meta, "format": MANIFEST_FORMAT}
        return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=True) + "\n"

    def to_json(self) -> str:
        """Meta + files as one document (``--diff`` reports, tests)."""
        total = sum(int(str(f.get("size", 0))) for f in self.files)
        doc = {
            **self.meta,
            "format": MANIFEST_FORMAT,
            "total_bytes": total,
            "files": self.files,
        }
        return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


_MANIFEST_HEADER: Final = (
    f"# MeshForge bundle manifest, format {MANIFEST_FORMAT}. Check an installed copy "
    f"with: sha256sum -c {MANIFEST_NAME}\n"
    "# '#' lines are ignored by sha256sum; they record symlinks, empty directories "
    "and executable files.\n"
)


def _manifest_path(path: str) -> str:
    """Refuse names ``sha256sum`` would escape (backslash, newline)."""
    if "\n" in path or "\\" in path or "\r" in path:
        raise BundleError(
            f"unsupported file name in the bundle: {path!r}", reason="bad_file"
        )
    return path


def manifest_text(entries: Sequence[Mapping[str, object]]) -> str:
    """``MANIFEST.sha256`` for ``entries`` (from :func:`scan_tree`). Canonical:
    one order, one spelling, ``\\n`` line ends."""
    files: list[str] = []
    notes: list[str] = []
    for entry in sorted(entries, key=lambda e: str(e["path"])):
        path = _manifest_path(str(entry["path"]))
        kind = entry["type"]
        if kind == "file":
            files.append(f"{entry['sha256']}  {path}\n")
            if entry.get("mode") == "0755":
                notes.append(f"#exec {path}\n")
        elif kind == "symlink":
            notes.append(f"#symlink {path} -> {_manifest_path(str(entry['target']))}\n")
        elif kind == "dir":
            notes.append(f"#dir {path}\n")
    return _MANIFEST_HEADER + "".join(files) + "".join(notes)


def parse_manifest_text(text: str) -> list[dict[str, object]]:
    """The entries of a ``MANIFEST.sha256`` (sizes are not recorded there)."""
    files: dict[str, dict[str, object]] = {}
    others: list[dict[str, object]] = []
    executable: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line or line.startswith("# "):
            continue
        if line.startswith("#exec "):
            executable.add(line[len("#exec ") :])
        elif line.startswith("#symlink "):
            path, sep, target = line[len("#symlink ") :].partition(" -> ")
            if not sep:
                raise BundleError(
                    f"manifest line {number}: bad symlink", reason="bad_manifest"
                )
            others.append({"path": path, "type": "symlink", "target": target})
        elif line.startswith("#dir "):
            others.append({"path": line[len("#dir ") :], "type": "dir"})
        elif len(line) > 66 and line[64:66] == "  ":
            path = line[66:]
            files[path] = {
                "path": path,
                "type": "file",
                "sha256": line[:64],
                "mode": "0644",
            }
        else:
            raise BundleError(
                f"manifest line {number}: not understood", reason="bad_manifest"
            )
    for path in executable:
        if path in files:
            files[path]["mode"] = "0755"
    return sorted([*files.values(), *others], key=lambda e: str(e["path"]))


def file_mode(st: os.stat_result) -> str:
    """Normalised mode: ``0755`` for anything executable, else ``0644``."""
    return "0755" if st.st_mode & 0o111 else "0644"


def scan_tree(
    root: Path, *, skip: Iterable[str] = (MANIFEST_NAME,)
) -> list[dict[str, object]]:
    """Sorted manifest entries for ``root``: files (size, sha256, mode), symlinks
    (target), empty directories."""
    skipped = set(skip)
    entries: list[dict[str, object]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        here = Path(dirpath)
        names = sorted([*filenames, *(d for d in dirnames if (here / d).is_symlink())])
        if not names and not dirnames and here != root:
            entries.append({"path": here.relative_to(root).as_posix(), "type": "dir"})
        for name in names:
            path = here / name
            rel = path.relative_to(root).as_posix()
            if rel in skipped:
                continue
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                entries.append(
                    {"path": rel, "type": "symlink", "target": os.readlink(path)}
                )
            elif stat.S_ISREG(st.st_mode):
                entries.append(
                    {
                        "path": rel,
                        "type": "file",
                        "size": st.st_size,
                        "sha256": sha256_file(path),
                        "mode": file_mode(st),
                    }
                )
            else:
                raise BundleError(f"{rel}: not a file or symlink", reason="bad_file")
    entries.sort(key=lambda e: str(e["path"]))
    return entries


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


_COMPARED: Final = ("type", "sha256", "mode", "target")
"""Entry fields compared by diff/verify (``size`` is implied by ``sha256`` and is
not in ``MANIFEST.sha256``)."""


def diff_entries(
    a: Sequence[Mapping[str, object]], b: Sequence[Mapping[str, object]]
) -> list[str]:
    """Human-readable differences between two entry lists (empty = same)."""
    out: list[str] = []
    files_a = {str(e["path"]): e for e in a}
    files_b = {str(e["path"]): e for e in b}
    for path in sorted(set(files_a) | set(files_b)):
        left, right = files_a.get(path), files_b.get(path)
        if left is None:
            out.append(f"only in second: {path}")
        elif right is None:
            out.append(f"only in first: {path}")
        else:
            fields = [k for k in _COMPARED if left.get(k) != right.get(k)]
            if fields:
                out.append(f"differs: {path} ({', '.join(fields)})")
    return out


def diff_meta(a: Mapping[str, object], b: Mapping[str, object]) -> list[str]:
    return [
        f"meta {key}: {a.get(key)!r} != {b.get(key)!r}"
        for key in sorted(set(a) | set(b))
        if a.get(key) != b.get(key)
    ]


def read_manifest(root: Path) -> list[dict[str, object]]:
    try:
        text = (root / MANIFEST_NAME).read_text(encoding="utf-8")
    except OSError as exc:
        raise BundleError(
            f"cannot read {MANIFEST_NAME}: {exc}", reason="no_manifest"
        ) from None
    return parse_manifest_text(text)


def verify_bundle(root: Path) -> list[str]:
    """Differences between ``root`` and its ``MANIFEST.sha256`` (empty = intact)."""
    return diff_entries(read_manifest(root), scan_tree(root))


# --------------------------------------------------------------- pins
@dataclass(frozen=True, slots=True)
class Download:
    """One pinned download: never "latest" (``packaging/pins.toml``)."""

    name: str
    version: str
    url: str
    sha256: str

    @property
    def file_name(self) -> str:
        return self.url.rsplit("/", 1)[-1].replace("%2B", "+")


def load_pins(path: Path) -> dict[str, object]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BundleError(
            f"cannot read pins {path}: {exc}", reason="bad_pins"
        ) from None


_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


def pinned(pins: Mapping[str, object], section: str, target: str) -> Download:
    """``[<section>.<target>]`` of the pins file as a :class:`Download`; refuses
    a missing / malformed pin rather than guessing a URL."""
    table = pins.get(section)
    entry = table.get(target) if isinstance(table, dict) else None
    version = table.get("version") if isinstance(table, dict) else None
    if not isinstance(entry, dict):
        raise BundleError(
            f"packaging/pins.toml has no [{section}.{target}] (url + sha256)",
            reason="bad_pins",
        )
    url, digest = entry.get("url"), entry.get("sha256")
    version = entry.get("version", version)
    if not (
        isinstance(url, str)
        and url.startswith("https://")
        and isinstance(digest, str)
        and _SHA256.match(digest)
        and isinstance(version, str)
        and "latest" not in url
    ):
        raise BundleError(
            f"[{section}.{target}] needs version, an https url (not 'latest') and a "
            "64-hex sha256",
            reason="bad_pins",
        )
    return Download(section, version, url, digest)


# ----------------------------------------------------------- the archive
def deterministic_tar_xz(root: Path, out: Path, epoch: int, *, preset: int = 6) -> int:
    """``root`` as ``<root.name>/...`` in a reproducible ``.tar.xz`` (sorted
    entries, owner 0/0 with no names, every mtime ``epoch``, normalised modes);
    returns its size. The size budget (Q17) is checked on this file."""

    def normalise(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = epoch
        if info.isdir():
            info.mode = 0o755
        elif info.isfile():
            info.mode = 0o755 if info.mode & 0o111 else 0o644
        return info

    paths = [
        root,
        *sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()),
    ]
    with (
        lzma.open(out, "wb", preset=preset, format=lzma.FORMAT_XZ) as xz,
        tarfile.open(fileobj=xz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for path in paths:
            arcname = (Path(root.name) / path.relative_to(root)).as_posix()
            tar.add(path, arcname=arcname, recursive=False, filter=normalise)
    return out.stat().st_size


def check_size_budget(size: int, budget_mb: float, what: str) -> None:
    limit = int(budget_mb * 1_000_000)
    if size > limit:
        raise BundleError(
            f"{what} is {size / 1e6:.1f} MB, over the {budget_mb:g} MB "
            "download budget (Q17); see the size table in lab/spikes/SP3.md",
            reason="size_budget",
        )
