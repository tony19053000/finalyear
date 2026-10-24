"""Pure parts of the D384 bundle builder (``scripts/build_bundle.py``).

No subprocesses and no network: OS layouts, launcher text, the relocation
rewrites (``#!`` lines, wheel ``RECORD`` rows, ``/install`` placeholders), the
build-path leak scan and the manifest. Unit-tested on their own
(``tests/unit/test_v017_sp3_bundle_lib.py``).
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

MANIFEST_NAME: Final = "MANIFEST.json"
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
        f"no bundle layout for {system} (Windows: --experimental-windows, untested)",
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
    """A ``.cmd`` launcher (UNTESTED until the Windows runner: SP1). ``%~dp0`` is
    the launcher's own folder; the code uses only ``'`` quotes so cmd's ``"``
    quoting stays simple."""
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


def leak_needles(paths: Iterable[Path | str]) -> list[bytes]:
    """Byte strings that must not appear in a bundle: each path as given and
    resolved, longest first, ignoring ones too short to be meaningful."""
    needles: set[str] = set()
    for item in paths:
        text = str(item).rstrip("/\\")
        for form in (text, os.path.realpath(text)):
            if len(form) >= 8 and form not in _TOO_GENERIC:
                needles.add(form)
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
@dataclass(slots=True)
class Manifest:
    meta: dict[str, object]
    files: list[dict[str, object]] = field(default_factory=list)

    def to_json(self) -> str:
        total = sum(int(str(f.get("size", 0))) for f in self.files)
        doc = {
            **self.meta,
            "format": MANIFEST_FORMAT,
            "total_bytes": total,
            "files": self.files,
        }
        return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


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


def diff_manifests(a: Mapping[str, object], b: Mapping[str, object]) -> list[str]:
    """Human-readable differences between two manifest documents (empty = same)."""
    out: list[str] = []
    for key in sorted(set(a) | set(b)):
        if key in {"files", "total_bytes"}:
            continue
        if a.get(key) != b.get(key):
            out.append(f"meta {key}: {a.get(key)!r} != {b.get(key)!r}")
    files_a = _by_path(a.get("files"))
    files_b = _by_path(b.get("files"))
    for path in sorted(set(files_a) | set(files_b)):
        left, right = files_a.get(path), files_b.get(path)
        if left is None:
            out.append(f"only in second: {path}")
        elif right is None:
            out.append(f"only in first: {path}")
        elif left != right:
            fields = sorted(
                k for k in set(left) | set(right) if left.get(k) != right.get(k)
            )
            out.append(f"differs: {path} ({', '.join(fields)})")
    return out


def _by_path(files: object) -> dict[str, dict[str, object]]:
    if not isinstance(files, list):
        return {}
    return {str(f["path"]): f for f in files if isinstance(f, dict) and "path" in f}


def verify_bundle(root: Path) -> list[str]:
    """Differences between ``root`` and its ``MANIFEST.json`` (empty = intact)."""
    try:
        recorded = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(
            f"cannot read {MANIFEST_NAME}: {exc}", reason="no_manifest"
        ) from None
    current = {**recorded, "files": scan_tree(root)}
    return diff_manifests(recorded, current)
