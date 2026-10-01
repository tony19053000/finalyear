"""Tree manifests: a project folder as a deterministic list of blobs (D81).

A **tree manifest** is canonical JSON (UTF-8, sorted keys, no spaces)::

    {"chunk_size": 8388608, "entries": [...], "v": 1}

with one entry per path, sorted by the POSIX path's UTF-8 bytes:

* file - ``{"path", "sha256", "size", "exec"}`` plus ``"chunks"`` (the per-chunk
  SHA-256 hex list, D80) when ``size > chunk_size``;
* symlink - ``{"path", "link"}`` (target relative and inside the tree);
* empty folder - ``{"path", "dir": true}`` (other folders are implied by their
  contents).

No timestamps, owners or modes beyond the exec bit, so the same folder gives the
same bytes on Linux and Windows (Windows has no exec bit: an executable script
packed there reads ``exec: false``, as with bundles). ``tree_id`` = SHA-256 of
those bytes; the manifest itself is stored as a blob under that digest, so
"resubmit an unchanged project" sends nothing (criterion 1).

:func:`build_tree` walks the folder with the bundle's walk
(:mod:`meshforge.common.walk`: ignore rules and path refusals shared, not copied)
and hashes each file once into its whole and chunk digests.

:func:`parse_tree` treats a manifest as **hostile** (it arrives over the network):
size cap, exact shape, canonical form (re-encoding must give the same bytes), every
path checked like an archive member name, strictly increasing order (so unique),
nothing placed under a file or symlink, chunk lists consistent with the sizes, and
the :class:`TreeLimits` caps. Any problem is :class:`TreeError` (``invalid_input``).
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from meshforge.common.bundle import check_member_name
from meshforge.common.cas import (
    DEFAULT_CHUNK_SIZE,
    MAX_CHUNK_SIZE,
    MIN_CHUNK_SIZE,
    chunk_count,
)
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.walk import (
    DRIVE_RE,
    MAX_PATH_BYTES,
    PathError,
    open_regular,
    scan_tree,
)

__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_MANIFEST_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "TREE_VERSION",
    "BuiltTree",
    "Tree",
    "TreeEntry",
    "TreeError",
    "TreeLimits",
    "build_tree",
    "canonical_bytes",
    "parse_tree",
]

TREE_VERSION: Final = 1
DEFAULT_MAX_ENTRIES: Final = 100_000
DEFAULT_MAX_TOTAL_BYTES: Final = 64 * 1024**3
DEFAULT_MAX_MANIFEST_BYTES: Final = 64 * 1024 * 1024
_HEX_RE: Final = re.compile(r"[0-9a-f]{64}")
_READ: Final = 1024 * 1024
_FILE_KEYS: Final = frozenset({"path", "sha256", "size", "exec"})
_CHUNKED_KEYS: Final = _FILE_KEYS | {"chunks"}
_LINK_KEYS: Final = frozenset({"path", "link"})
_DIR_KEYS: Final = frozenset({"path", "dir"})


class TreeError(PathError):
    """A folder cannot become a tree, or a manifest must not be accepted."""


@dataclass(frozen=True, slots=True)
class TreeLimits:
    """Caps for :func:`build_tree` and :func:`parse_tree` (max accepted, max+1 not)."""

    max_entries: int = DEFAULT_MAX_ENTRIES
    max_path_bytes: int = MAX_PATH_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES

    def __post_init__(self) -> None:
        if (
            self.max_entries < 1
            or not 1 <= self.max_path_bytes <= MAX_PATH_BYTES
            or self.max_total_bytes < 0
            or self.max_manifest_bytes < 1
        ):
            raise ValueError("tree limits out of range")


@dataclass(frozen=True, slots=True)
class TreeEntry:
    path: str
    kind: Literal["file", "symlink", "dir"]
    sha256: str = ""
    """Hex digest of a file's bytes ("" for symlinks and folders)."""
    size: int = 0
    executable: bool = False
    link: str = ""
    chunks: tuple[str, ...] = ()
    """Per-chunk hex digests; empty when the file is one chunk."""

    def to_json(self) -> dict[str, Any]:
        if self.kind == "dir":
            return {"path": self.path, "dir": True}
        if self.kind == "symlink":
            return {"path": self.path, "link": self.link}
        out: dict[str, Any] = {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "exec": self.executable,
        }
        if self.chunks:
            out["chunks"] = list(self.chunks)
        return out

    @property
    def digest(self) -> Digest:
        return Digest(self.sha256)


@dataclass(frozen=True, slots=True)
class Tree:
    chunk_size: int
    entries: tuple[TreeEntry, ...]
    tree_id: Digest
    manifest_bytes: int

    def files(self) -> Iterator[TreeEntry]:
        return (e for e in self.entries if e.kind == "file")

    def blob_sizes(self) -> dict[str, int]:
        """Unique file blobs: hex digest -> size (a file stored twice counts once)."""
        return {e.sha256: e.size for e in self.files()}

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.files())


@dataclass(frozen=True, slots=True)
class BuiltTree:
    tree: Tree
    manifest: bytes
    """The canonical manifest bytes (store them as the blob named ``tree_id``)."""
    sources: dict[str, Path]
    """Hex digest -> one local file with those bytes (for uploading)."""
    ignored: int


def canonical_bytes(chunk_size: int, entries: tuple[TreeEntry, ...]) -> bytes:
    body = {
        "v": TREE_VERSION,
        "chunk_size": chunk_size,
        "entries": [e.to_json() for e in entries],
    }
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


# ----------------------------------------------------------------------- build


def _hash_file(
    entry_path: Path, rel: str, ident: tuple[int, int], chunk_size: int, size: int
) -> tuple[str, tuple[str, ...]]:
    whole = hashlib.sha256()
    chunks: list[str] = []
    got_total = 0
    with open_regular(entry_path, rel, TreeError, ident) as fh:
        while True:
            piece = hashlib.sha256()
            got = 0
            while got < chunk_size:
                data = fh.read(min(_READ, chunk_size - got))
                if not data:
                    break
                piece.update(data)
                whole.update(data)
                got += len(data)
            if got == 0 and chunks:
                break
            chunks.append(piece.hexdigest())
            got_total += got
            if got < chunk_size:
                break
    if got_total != size:
        raise TreeError("file changed while it was hashed; try again", path=rel)
    return whole.hexdigest(), tuple(chunks) if size > chunk_size else ()


def build_tree(
    root: Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    limits: TreeLimits | None = None,
    exclude: tuple[str, ...] = (),
) -> BuiltTree:
    """The tree of ``root`` (see module docstring). Raises :class:`TreeError`."""
    limits = limits or TreeLimits()
    if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        raise ValueError("chunk_size out of range")
    root = Path(root).resolve()
    if not root.is_dir():
        raise TreeError("the project folder does not exist or is not a folder")
    state = scan_tree(
        root,
        # Non-empty folders are walked but not listed; this only bounds the walk's
        # memory - the entry cap below is the real limit.
        max_files=limits.max_entries * 8,
        max_total_bytes=limits.max_total_bytes,
        error=TreeError,
        exclude=exclude,
    )
    has_children = {
        posixpath.dirname(e.rel) for e in state.entries if posixpath.dirname(e.rel)
    }
    entries: list[TreeEntry] = []
    sources: dict[str, Path] = {}
    for item in state.entries:
        if len(item.rel.encode("utf-8")) > limits.max_path_bytes:
            raise TreeError(
                f"path longer than {limits.max_path_bytes} bytes", path=item.rel
            )
        if item.kind == "dir":
            if item.rel in has_children:
                continue
            entries.append(TreeEntry(item.rel, "dir"))
        elif item.kind == "symlink":
            entries.append(TreeEntry(item.rel, "symlink", link=item.link))
        else:
            sha, chunks = _hash_file(
                item.path, item.rel, item.ident, chunk_size, item.size
            )
            entries.append(
                TreeEntry(
                    item.rel,
                    "file",
                    sha256=sha,
                    size=item.size,
                    executable=item.executable,
                    chunks=chunks,
                )
            )
            sources.setdefault(sha, item.path)
        if len(entries) > limits.max_entries:
            raise TreeError(
                f"more than {limits.max_entries} entries "
                "(add rules to .computeignore, or raise the limit)"
            )
    ordered = tuple(sorted(entries, key=lambda e: e.path.encode("utf-8")))
    manifest = canonical_bytes(chunk_size, ordered)
    if len(manifest) > limits.max_manifest_bytes:
        raise TreeError(f"tree manifest larger than {limits.max_manifest_bytes} bytes")
    tree = Tree(
        chunk_size=chunk_size,
        entries=ordered,
        tree_id=Digest(hashlib.sha256(manifest).hexdigest()),
        manifest_bytes=len(manifest),
    )
    return BuiltTree(
        tree=tree, manifest=manifest, sources=sources, ignored=state.ignored
    )


# ----------------------------------------------------------------------- parse


def _utf8(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _check_link(link: object, rel: str) -> str:
    if (
        not isinstance(link, str)
        or not link
        or "\x00" in link
        or "\\" in link
        or not _utf8(link)
    ):
        raise TreeError("symlink target is empty or malformed", path=rel)
    if len(link.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES:
        raise TreeError("symlink target too long", path=rel)
    if link.startswith("/") or DRIVE_RE.match(link):
        raise TreeError("symlink target is absolute", path=rel)
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(rel), link))
    if joined == ".." or joined.startswith("../"):
        raise TreeError("symlink escapes the tree", path=rel)
    return link


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _entry(raw: object, chunk_size: int, limits: TreeLimits) -> TreeEntry:
    if not isinstance(raw, dict):
        raise TreeError("a tree entry is not an object")
    path = raw.get("path")
    if not isinstance(path, str):
        raise TreeError("a tree entry has no path")
    if not _utf8(path):
        # JSON may carry a lone surrogate ("\ud800"): not a real file name.
        raise TreeError("a tree path is not valid UTF-8")
    try:
        check_member_name(path)
    except PathError as exc:
        raise TreeError(exc.message) from None
    if path.endswith("/"):
        raise TreeError("a tree path ends with '/'", path=path)
    if len(path.encode("utf-8", "surrogatepass")) > limits.max_path_bytes:
        raise TreeError(f"path longer than {limits.max_path_bytes} bytes", path=path)
    keys = frozenset(raw)
    if keys == _DIR_KEYS:
        if raw["dir"] is not True:
            raise TreeError("'dir' must be true", path=path)
        return TreeEntry(path, "dir")
    if keys == _LINK_KEYS:
        return TreeEntry(path, "symlink", link=_check_link(raw["link"], path))
    if keys not in (_FILE_KEYS, _CHUNKED_KEYS):
        raise TreeError("a tree entry has unexpected or missing fields", path=path)
    sha, size, executable = raw["sha256"], raw["size"], raw["exec"]
    if not isinstance(sha, str) or not _HEX_RE.fullmatch(sha):
        raise TreeError("sha256 is not 64 lower-case hex", path=path)
    if not _is_int(size) or not 0 <= size <= limits.max_total_bytes:
        raise TreeError("size is not a valid byte count", path=path)
    if not isinstance(executable, bool):
        raise TreeError("exec must be true or false", path=path)
    chunks: tuple[str, ...] = ()
    if size > chunk_size:
        listed = raw.get("chunks")
        if not isinstance(listed, list) or len(listed) != chunk_count(size, chunk_size):
            raise TreeError("chunk list does not match the file size", path=path)
        if not all(isinstance(c, str) and _HEX_RE.fullmatch(c) for c in listed):
            raise TreeError("a chunk digest is not 64 lower-case hex", path=path)
        chunks = tuple(listed)
    elif "chunks" in raw:
        raise TreeError("a one-chunk file must not list chunks", path=path)
    return TreeEntry(
        path, "file", sha256=sha, size=size, executable=executable, chunks=chunks
    )


def parse_tree(
    data: bytes, *, expected: Digest | None = None, limits: TreeLimits | None = None
) -> Tree:
    """Validate manifest bytes (hostile input; see module docstring).

    ``expected`` = the tree_id the bytes must hash to (``integrity_error`` if not).
    """
    limits = limits or TreeLimits()
    if len(data) > limits.max_manifest_bytes:
        raise TreeError(f"tree manifest larger than {limits.max_manifest_bytes} bytes")
    tree_id = Digest(hashlib.sha256(data).hexdigest())
    if expected is not None and tree_id != expected:
        raise TreeError(
            "the tree manifest does not match its id",
            failure_type=FailureType.INTEGRITY_ERROR,
        )
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise TreeError("the tree manifest is not UTF-8 JSON") from None
    if not isinstance(body, dict) or frozenset(body) != {"v", "chunk_size", "entries"}:
        raise TreeError(
            "the tree manifest does not have exactly v, chunk_size, entries"
        )
    if body["v"] != TREE_VERSION or not _is_int(body["v"]):
        raise TreeError(
            f"unsupported tree manifest version (this build reads {TREE_VERSION})"
        )
    chunk_size = body["chunk_size"]
    if not _is_int(chunk_size) or not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        raise TreeError("chunk_size out of range")
    raw_entries = body["entries"]
    if not isinstance(raw_entries, list):
        raise TreeError("entries is not a list")
    if len(raw_entries) > limits.max_entries:
        raise TreeError(f"more than {limits.max_entries} entries")
    entries: list[TreeEntry] = []
    leaves: set[str] = set()
    total = 0
    previous: bytes | None = None
    for raw in raw_entries:
        entry = _entry(raw, chunk_size, limits)
        key = entry.path.encode("utf-8")
        if previous is not None and key <= previous:
            raise TreeError(
                "entries are not strictly sorted (or repeat)", path=entry.path
            )
        previous = key
        parts = entry.path.split("/")
        for depth in range(1, len(parts)):
            if "/".join(parts[:depth]) in leaves:
                raise TreeError(
                    "an entry is placed under a file, link or empty folder",
                    path=entry.path,
                )
        leaves.add(entry.path)
        total += entry.size
        if total > limits.max_total_bytes:
            raise TreeError(f"tree content larger than {limits.max_total_bytes} bytes")
        entries.append(entry)
    ordered = tuple(entries)
    if canonical_bytes(chunk_size, ordered) != data:
        raise TreeError("the tree manifest is not in canonical form")
    return Tree(
        chunk_size=chunk_size,
        entries=ordered,
        tree_id=tree_id,
        manifest_bytes=len(data),
    )
