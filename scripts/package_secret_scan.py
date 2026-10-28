"""Scan release artefacts for secrets and test material (V1.0 G3, D413; stdlib only).

A MeshForge package must never carry a credential of the build machine or of a
test fabric. This scanner walks a bundle folder, a ``.deb`` (both its
``control`` and ``data`` archives), a ``.tar.*`` / ``.zip`` / ``.whl`` archive,
or (best effort, raw bytes only) any other file such as the Windows installer,
and reports:

* ``pem_private_key`` - a PEM private-key block with a body (``-----BEGIN ...
  PRIVATE KEY-----`` + base64 + ``-----END``); a bare marker in source code
  (a regex, a docstring) has no body and is not a hit;
* ``api_token`` / ``session_token`` - an ``mfat_`` / ``mfbs_`` token
  (prefix + exactly 43 URL-safe characters, ``common/api_token.py``);
* ``join_code`` - an ``mf1-`` join code (base32 groups of five,
  ``common/join_code.py``);
* ``secret_file`` - a file named like a MeshForge or generic secret
  (``*.key``, ``fabric.token``, ``secrets.key``, ``setup-token``,
  ``app.password``, ``admin.password``, ``.pgpass``, ``.env``, ``id_rsa`` ...);
* ``test_dir`` - MeshForge's own tests (a ``tests``/``test``/``conftest.py``
  under the ``meshforge`` package, at the top of ``site-packages`` or at the
  artefact root). Third-party packages' own test suites are not ours and are
  only content-scanned;
* ``dev_password`` - the published dev database password ``meshforge-dev`` as a
  value in a configuration file (``.toml``, ``.ini``, ``.env``, ``.yaml`` ...;
  the controller's Python source names it on purpose, to refuse it), or a
  password the test suite uses.

A hit prints the artefact, the member path, the rule and the byte offset -
**never the matched text** (the output goes to CI logs). ``ALLOW`` lists
(rule, path glob) pairs that are known false positives, each with a reason.

Usage::

    uv run python scripts/package_secret_scan.py ARTEFACT [ARTEFACT ...] [--json]

Exit codes: 0 clean · 1 at least one hit · 2 an artefact is missing or cannot
be read. ``build_bundle.py``, ``build_deb.py`` and ``build_windows_installer.py``
call :func:`scan` and fail the build on a hit; CI runs this CLI on the built
artefacts as well.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import re
import sys
import tarfile
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Final

MAX_MEMBER_BYTES: Final = 512 * 1024 * 1024
"""Members larger than this are scanned by name only (none exist today)."""

# ----------------------------------------------------------------- content rules
_PEM: Final = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ ){0,4}PRIVATE KEY-----\r?\n"
    rb"(?:[A-Za-z][A-Za-z-]*: [^\r\n]{0,200}\r?\n)*\r?\n?"  # Proc-Type / DEK-Info
    rb"[A-Za-z0-9+/=]{16,}\r?\n"
    rb"(?:[A-Za-z0-9+/=]{1,128}\r?\n){0,400}"
    rb"-----END (?:[A-Z0-9]+ ){0,4}PRIVATE KEY-----"
)
_API_TOKEN: Final = re.compile(
    rb"(?<![A-Za-z0-9_])mfat_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"
)
_SESSION_TOKEN: Final = re.compile(
    rb"(?<![A-Za-z0-9_])mfbs_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"
)
_JOIN_CODE: Final = re.compile(
    rb"(?<![A-Za-z0-9_])[Mm][Ff]1-[A-Za-z2-7]{5}(?:-[A-Za-z2-7]{1,5}){6,}"
)
_DEV_PASSWORD: Final = re.compile(rb"meshforge-dev(?![A-Za-z0-9_-])")
TEST_PASSWORDS: Final = (b"correct horse battery staple",)
"""Passwords the test suite uses for fabrics it creates; never in a package."""
CONFIG_SUFFIXES: Final = frozenset(
    {".toml", ".ini", ".cfg", ".conf", ".env", ".yaml", ".yml", ".json", ".properties"}
)

# -------------------------------------------------------------------- name rules
SECRET_NAMES: Final = frozenset(
    {
        "fabric.token",
        "secrets.key",
        "setup-token",
        "app.password",
        "admin.password",
        ".pgpass",
        ".env",
        "id_rsa",
        "id_ecdsa",
        "id_ed25519",
        ".netrc",
    }
)
SECRET_SUFFIXES: Final = (".key",)
TEST_DIR_NAMES: Final = frozenset({"tests", "test"})

ALLOW: Final[tuple[tuple[str, str, str], ...]] = ()
"""(rule, member path glob, reason): known false positives. Empty: the release
bundles have none (V1.0 G3); add one only with a reason a reviewer accepts."""


@dataclass(frozen=True, slots=True)
class Hit:
    artefact: str
    member: str
    rule: str
    offset: int | None = None

    def render(self) -> str:
        where = "" if self.offset is None else f" @ byte {self.offset}"
        return f"{self.artefact}: {self.member}: {self.rule}{where}"


class ScanError(Exception):
    """An artefact is missing or cannot be read."""


# ------------------------------------------------------------------- the rules
def _is_test_path(parts: Sequence[str]) -> bool:
    """MeshForge's own tests: a ``tests``/``test`` folder or ``conftest.py`` at the
    artefact root or at the top of a ``site-packages``, or anywhere inside the
    ``meshforge`` package there (a wheel's members start at that level)."""
    roots = [0] + [i + 1 for i, part in enumerate(parts) if part == "site-packages"]

    def is_test(index: int) -> bool:
        part = parts[index]
        if index < len(parts) - 1:
            return part in TEST_DIR_NAMES
        return part == "conftest.py"

    for root in roots:
        if root >= len(parts):
            continue
        if is_test(root):
            return True
        if parts[root] == "meshforge" and any(
            is_test(i) for i in range(root + 1, len(parts))
        ):
            return True
    return False


def name_hits(member: str) -> list[str]:
    """Rules a member's path alone breaks."""
    parts = [
        p
        for p in PurePosixPath(member.replace("\\", "/")).parts
        if p not in ("", ".", "/")
    ]
    if not parts:
        return []
    name = parts[-1]
    rules: list[str] = []
    if name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES):
        rules.append("secret_file")
    if _is_test_path(parts):
        rules.append("test_dir")
    return rules


def content_hits(member: str, data: bytes) -> list[tuple[str, int]]:
    """(rule, offset) for every content rule ``data`` breaks (first match each)."""
    found: list[tuple[str, int]] = []
    if b"PRIVATE KEY-----" in data:
        match = _PEM.search(data)
        if match:
            found.append(("pem_private_key", match.start()))
    if b"mfat_" in data and (match := _API_TOKEN.search(data)):
        found.append(("api_token", match.start()))
    if b"mfbs_" in data and (match := _SESSION_TOKEN.search(data)):
        found.append(("session_token", match.start()))
    if (b"mf1-" in data or b"MF1-" in data) and (match := _JOIN_CODE.search(data)):
        found.append(("join_code", match.start()))
    suffix = PurePosixPath(member).suffix.lower()
    name = PurePosixPath(member).name
    if (suffix in CONFIG_SUFFIXES or name.startswith(".env")) and (
        match := _DEV_PASSWORD.search(data)
    ):
        found.append(("dev_password", match.start()))
    for password in TEST_PASSWORDS:
        offset = data.find(password)
        if offset >= 0:
            found.append(("dev_password", offset))
            break
    return found


def _allowed(rule: str, member: str) -> bool:
    return any(rule == r and fnmatch.fnmatch(member, glob) for r, glob, _ in ALLOW)


def check_member(artefact: str, member: str, data: bytes | None) -> list[Hit]:
    hits = [Hit(artefact, member, rule) for rule in name_hits(member)]
    if data is not None:
        hits += [
            Hit(artefact, member, rule, offset)
            for rule, offset in content_hits(member, data)
        ]
    return [h for h in hits if not _allowed(h.rule, h.member)]


# ------------------------------------------------------------------ the walkers
def _tar_members(
    blob: io.IOBase | bytes, label: str
) -> Iterator[tuple[str, bytes | None]]:
    fileobj = io.BytesIO(blob) if isinstance(blob, bytes) else blob
    try:
        with tarfile.open(fileobj=fileobj, mode="r:*") as tar:
            for info in tar:
                name = info.name.removeprefix("./")
                if info.isdir():
                    yield name.rstrip("/") + "/", None
                    continue
                if not info.isfile():
                    yield name, None
                    continue
                if info.size > MAX_MEMBER_BYTES:
                    yield name, None
                    continue
                handle = tar.extractfile(info)
                yield name, handle.read() if handle is not None else None
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ScanError(f"{label}: not a readable tar archive ({exc})") from None


def _read_ar(data: bytes, label: str) -> dict[str, bytes]:
    if not data.startswith(b"!<arch>\n"):
        raise ScanError(f"{label}: not an ar archive (.deb)")
    members: dict[str, bytes] = {}
    offset = 8
    while offset < len(data):
        header = data[offset : offset + 60]
        if len(header) < 60 or header[58:60] != b"`\n":
            raise ScanError(f"{label}: truncated or corrupt .deb")
        name = header[:16].decode("ascii", "replace").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        start = offset + 60
        members[name] = data[start : start + size]
        offset = start + size + (size % 2)
    return members


def _dir_members(root: Path) -> Iterator[tuple[str, bytes | None]]:
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            yield rel, None
        elif path.is_dir():
            yield rel + "/", None
        elif path.is_file():
            size = path.stat().st_size
            yield rel, None if size > MAX_MEMBER_BYTES else path.read_bytes()


def members(artefact: Path) -> Iterator[tuple[str, bytes | None]]:
    """(member path, bytes or None) of an artefact (see the module docstring)."""
    label = str(artefact)
    if artefact.is_dir():
        yield from _dir_members(artefact)
        return
    if not artefact.is_file():
        raise ScanError(f"{label}: no such file or folder")
    name = artefact.name.lower()
    try:
        if name.endswith(".deb"):
            for member, blob in _read_ar(artefact.read_bytes(), label).items():
                if member.startswith(("control.tar", "data.tar")):
                    prefix = "control/" if member.startswith("control") else ""
                    for inner, data in _tar_members(blob, f"{label}:{member}"):
                        yield prefix + inner, data
                else:
                    yield f"{member}", blob
        elif ".tar" in name or name.endswith((".tgz", ".txz")):
            with artefact.open("rb") as handle:
                yield from _tar_members(handle, label)
        elif name.endswith((".zip", ".whl")):
            with zipfile.ZipFile(artefact) as zf:
                for info in zf.infolist():
                    if info.is_dir() or info.file_size > MAX_MEMBER_BYTES:
                        yield info.filename, None
                    else:
                        yield info.filename, zf.read(info)
        else:
            # e.g. the Inno Setup .exe: compressed, so only raw-byte rules can
            # fire; the builder scans the installer's inputs before compiling.
            yield artefact.name, artefact.read_bytes()
    except (OSError, zipfile.BadZipFile) as exc:
        raise ScanError(f"{label}: cannot read ({exc})") from None


def scan(artefact: Path) -> list[Hit]:
    """Every hit in one artefact; :class:`ScanError` if it cannot be read."""
    hits: list[Hit] = []
    for member, data in members(artefact):
        hits += check_member(str(artefact), member, data)
    return hits


def failure_message(hits: Sequence[Hit], limit: int = 30) -> str:
    """One build-failure text for a builder (rules and paths, never values)."""
    shown = "\n".join(f"  {hit.render()}" for hit in hits[:limit])
    more = f"\n  ... and {len(hits) - limit} more" if len(hits) > limit else ""
    return (
        f"the package secret scan found {len(hits)} problem(s) "
        f"(scripts/package_secret_scan.py):\n{shown}{more}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("artefacts", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true", help="print hits as JSON")
    args = parser.parse_args(argv)
    hits: list[Hit] = []
    unreadable = False
    for artefact in args.artefacts:
        try:
            found = scan(artefact)
        except ScanError as exc:
            print(f"package_secret_scan: error: {exc}", file=sys.stderr)
            unreadable = True
            continue
        hits += found
        print(f"package_secret_scan: {artefact}: {len(found)} hit(s)", file=sys.stderr)
    if args.json:
        print(json.dumps([asdict(h) for h in hits], indent=2))
    else:
        for hit in hits:
            print(hit.render())
    if unreadable:
        return 2
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
