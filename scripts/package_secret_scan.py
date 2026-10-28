"""Scan release artefacts for secrets and test material (V1.0 G3, D413; stdlib only).

A MeshForge package must never carry a credential of the build machine or of a
test fabric. This scanner walks a bundle folder, a ``.deb`` (both its
``control`` and ``data`` archives), a ``.tar.*`` / ``.zip`` / ``.whl`` archive,
or (best effort, raw bytes only) any other file such as the Windows installer.
An archive found inside (a wheel in a bundle, a tar.gz in a zip) is opened too,
up to ``MAX_NESTING`` levels and ``MAX_NESTED_BYTES`` expanded; a deeper,
larger or unreadable nested archive fails the scan (exit 2) rather than hide.
Its members are reported as ``outer!/inner``. It reports:

* ``pem_private_key`` - a PEM private-key block with a body (``-----BEGIN ...
  PRIVATE KEY-----`` + base64 + ``-----END``), with real line breaks or breaks
  escaped into a string (``\\n`` in JSON / Python / YAML); a bare marker in
  source code (a regex, a docstring) has no body and is not a hit;
* ``api_token`` / ``session_token`` - an ``mfat_`` / ``mfbs_`` token
  (prefix + exactly 43 URL-safe characters, ``common/api_token.py``);
* ``join_code`` - an ``mf1-`` join code (base32 groups of five,
  ``common/join_code.py``);
* ``secret_file`` - a file named like a MeshForge or generic secret
  (``*.key``, ``*.p12``, ``*.pfx``, ``*.jks``, ``fabric.token``,
  ``secrets.key``, ``setup-token``, ``app.password``, ``admin.password``,
  ``.pgpass``, ``.env``, ``.env.*`` but not ``.env.example``, ``id_rsa`` ...);
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
_NL: Final = rb"(?:\r?\n|(?:\\+r)?\\+n)"
"""A line break: a real one, or one escaped into a string (``\\n``, ``\\r\\n``,
``\\\\n`` ...) as a key pasted into JSON / Python / YAML source is (TG3-1)."""
MAX_PEM_BODY: Final = 256 * 1024
_PEM: Final = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ ){0,4}PRIVATE KEY-----"
    rb"(?:" + _NL + rb"[A-Za-z][A-Za-z-]*: [^\r\n\\]{0,200})*"  # Proc-Type / DEK-Info
    # the body: base64 in lines of any length, real or escaped breaks, and no
    # break required before END (TG3-2); checked for real content in _pem_hit
    rb"(?P<body>[A-Za-z0-9+/=\s\\]{16," + str(MAX_PEM_BODY).encode() + rb"})"
    rb"-----END (?:[A-Z0-9]+ ){0,4}PRIVATE KEY-----"
)
_ESCAPE: Final = re.compile(rb"\\+[rnt]|\s|\\")
MIN_PEM_BODY_CHARS: Final = 16
# Not preceded by a word character - unless that character ends an escaped
# line break / tab (``...\\nmfat_...`` in a JSON log line, TG3-5).
_START: Final = rb"(?:(?<![A-Za-z0-9_])|(?<=\\[nrt]))"
# Not followed by a token character - unless another token starts right there
# (tokens concatenated with no separator).
_END: Final = rb"(?:(?![A-Za-z0-9_-])|(?=mf(?:at|bs)_))"
_API_TOKEN: Final = re.compile(_START + rb"mfat_[A-Za-z0-9_-]{43}" + _END)
_SESSION_TOKEN: Final = re.compile(_START + rb"mfbs_[A-Za-z0-9_-]{43}" + _END)
_JOIN_CODE: Final = re.compile(
    _START + rb"[Mm][Ff]1-[A-Za-z2-7]{5}(?:-[A-Za-z2-7]{1,5}){6,}"
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
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        ".netrc",
    }
)
SECRET_SUFFIXES: Final = (".key", ".p12", ".pfx", ".jks", ".keystore")
"""Private-key / key-store containers (TG3-4). Not ``.pem``: CA bundles such as
certifi's ``cacert.pem`` are public certificates and ship on purpose (a PEM
*private key* is found by content instead)."""
ENV_TEMPLATES: Final = frozenset({".env.example", ".env.sample", ".env.template"})
"""``.env.*`` files are secrets (``.env.local``, ``.env.production``) except
these conventional, value-free templates."""
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
    is_env = name.startswith(".env.") and name.lower() not in ENV_TEMPLATES
    if name in SECRET_NAMES or name.lower().endswith(SECRET_SUFFIXES) or is_env:
        rules.append("secret_file")
    if _is_test_path(parts):
        rules.append("test_dir")
    return rules


def _pem_hit(data: bytes) -> int | None:
    """Offset of the first PEM private-key block with a real body (at least
    ``MIN_PEM_BODY_CHARS`` base64 characters once breaks and escapes go)."""
    for match in _PEM.finditer(data):
        if len(_ESCAPE.sub(b"", match["body"])) >= MIN_PEM_BODY_CHARS:
            return match.start()
    return None


def content_hits(member: str, data: bytes) -> list[tuple[str, int]]:
    """(rule, offset) for every content rule ``data`` breaks (first match each)."""
    found: list[tuple[str, int]] = []
    if b"PRIVATE KEY-----" in data and (offset := _pem_hit(data)) is not None:
        found.append(("pem_private_key", offset))
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


def check_member(
    artefact: str, member: str, data: bytes | None, rule_path: str | None = None
) -> list[Hit]:
    """Hits of one member. ``rule_path`` is the path the name rules judge (for
    a member of a nested archive: its path inside that archive); ``member`` is
    the full ``outer!/inner`` path reported and matched against ``ALLOW``."""
    path = member if rule_path is None else rule_path
    hits = [Hit(artefact, member, rule) for rule in name_hits(path)]
    if data is not None:
        hits += [
            Hit(artefact, member, rule, offset)
            for rule, offset in content_hits(path, data)
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


MAX_NESTING: Final = 3
"""Archives inside archives are opened this many levels deep (a wheel inside a
bundle tar is level 1); deeper nesting fails the scan rather than hide."""
MAX_NESTED_BYTES: Final = 2 * 1024 * 1024 * 1024
"""Total bytes all nested archives of one artefact may expand to (a zip bomb
fails the scan instead of exhausting memory or time)."""
NESTED_SEPARATOR: Final = "!/"

Member = tuple[str, str, bytes | None]
"""(reported path, path the name rules judge, bytes or None)."""


_TAR_SUFFIXES: Final = (
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.xz",
    ".txz",
    ".tar.bz2",
    ".tbz2",
)
_ZIP_SUFFIXES: Final = (".zip", ".whl", ".jar", ".egg", ".nupkg")


def _archive_kind(name: str) -> str | None:
    lower = name.lower()
    if lower.endswith(".deb"):
        return "deb"
    if lower.endswith(_TAR_SUFFIXES):
        return "tar"
    if lower.endswith(_ZIP_SUFFIXES):
        return "zip"
    return None


def _zip_members(source: Path | bytes) -> Iterator[tuple[str, bytes | None]]:
    fileobj: Path | io.BytesIO = (
        io.BytesIO(source) if isinstance(source, bytes) else source
    )
    with zipfile.ZipFile(fileobj) as zf:
        for info in zf.infolist():
            if info.is_dir() or info.file_size > MAX_MEMBER_BYTES:
                yield info.filename, None
            else:
                yield info.filename, zf.read(info)


def _deb_members(data: bytes, label: str) -> Iterator[tuple[str, bytes | None]]:
    for member, blob in _read_ar(data, label).items():
        if member.startswith(("control.tar", "data.tar")):
            prefix = "control/" if member.startswith("control") else ""
            for inner, inner_data in _tar_members(blob, f"{label}:{member}"):
                yield prefix + inner, inner_data
        else:
            yield f"{member}", blob


def _archive_members(
    kind: str, source: Path | bytes, label: str
) -> Iterator[tuple[str, bytes | None]]:
    """(member path, bytes or None) of a .deb / tar / zip, from a file or bytes."""
    try:
        if kind == "deb":
            data = source if isinstance(source, bytes) else source.read_bytes()
            yield from _deb_members(data, label)
        elif kind == "tar":
            if isinstance(source, bytes):
                yield from _tar_members(source, label)
            else:
                with source.open("rb") as handle:
                    yield from _tar_members(handle, label)
        else:
            yield from _zip_members(source)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, EOFError) as exc:
        raise ScanError(f"{label}: cannot read ({exc})") from None
    except (ValueError, NotImplementedError, RuntimeError) as exc:
        # e.g. an unsupported compression method or an encrypted zip member:
        # unreadable content must fail the scan, not pass it.
        raise ScanError(f"{label}: cannot read ({exc})") from None


class _Budget:
    """Bytes nested archives of one artefact may still expand to."""

    def __init__(self) -> None:
        self.left = MAX_NESTED_BYTES

    def spend(self, size: int, label: str) -> None:
        self.left -= size
        if self.left < 0:
            raise ScanError(
                f"{label}: nested archives expand past {MAX_NESTED_BYTES} bytes"
            )


def _expand(
    items: Iterator[tuple[str, bytes | None]],
    label: str,
    prefix: str,
    depth: int,
    budget: _Budget,
) -> Iterator[Member]:
    """Yield every member, and open any member that is itself an archive (a
    wheel in a bundle, a tar.gz in a zip ...) up to ``MAX_NESTING`` levels."""
    for name, data in items:
        yield prefix + name, name, data
        if data is None or name.endswith("/"):
            continue
        kind = _archive_kind(name)
        if kind is None:
            continue
        nested_label = f"{label}{NESTED_SEPARATOR}{name}"
        if depth >= MAX_NESTING:
            raise ScanError(
                f"{nested_label}: archives nested deeper than {MAX_NESTING} levels"
            )
        inner_items = _archive_members(kind, data, nested_label)
        yield from _expand(
            _spent(inner_items, nested_label, budget),
            nested_label,
            f"{prefix}{name}{NESTED_SEPARATOR}",
            depth + 1,
            budget,
        )


def _spent(
    items: Iterator[tuple[str, bytes | None]], label: str, budget: _Budget
) -> Iterator[tuple[str, bytes | None]]:
    for name, data in items:
        if data is not None:
            budget.spend(len(data), label)
        yield name, data


def walk(artefact: Path) -> Iterator[Member]:
    """Every member of an artefact, nested archives opened (see the module
    docstring): (reported path, path the name rules judge, bytes or None)."""
    label = str(artefact)
    budget = _Budget()
    if artefact.is_dir():
        yield from _expand(_dir_members(artefact), label, "", 0, budget)
        return
    if not artefact.is_file():
        raise ScanError(f"{label}: no such file or folder")
    kind = _archive_kind(artefact.name)
    if kind is None:
        # e.g. the Inno Setup .exe: compressed, so only raw-byte rules can
        # fire; the builder scans the installer's inputs before compiling.
        try:
            data = artefact.read_bytes()
        except OSError as exc:
            raise ScanError(f"{label}: cannot read ({exc})") from None
        yield artefact.name, artefact.name, data
        return
    yield from _expand(_archive_members(kind, artefact, label), label, "", 0, budget)


def members(artefact: Path) -> Iterator[tuple[str, bytes | None]]:
    """(reported member path, bytes or None) of an artefact, nested included."""
    for member, _rule_path, data in walk(artefact):
        yield member, data


def scan(artefact: Path) -> list[Hit]:
    """Every hit in one artefact; :class:`ScanError` if it cannot be read."""
    hits: list[Hit] = []
    for member, rule_path, data in walk(artefact):
        hits += check_member(str(artefact), member, data, rule_path)
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
