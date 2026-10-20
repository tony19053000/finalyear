"""The built-in combines of a split job (V0.10 G5, D268/D283).

Trusted MeshForge code that the agent runs **in its own process** (``python -m
meshforge.agent.execution.combine``, the agent's interpreter: no user code, no
Docker image) over an agent-owned folder:

``parts/NNNNN/...`` - every chunk's committed result, unpacked read-only, the
folder names zero-padded so that name order **is** chunk order - and an empty
``out/`` that becomes the job's output.

**Per relative path** (the union of the files of every part; a file that only
some chunks wrote is fine), in chunk order:

* ``concat`` - the bytes of every part's copy, in chunk order, nothing inserted.
* ``merge-csv`` - ``*.csv`` files (any case of the suffix) are merged; every
  other file is concatenated. The first line of every non-empty part file is its
  header; headers are compared **byte for byte** after one leading UTF-8 BOM and
  the line terminator are stripped - different -> ``header_mismatch``. The
  first part's header line is kept (with its BOM); every later header is
  dropped. A part whose rows do not end with a line terminator gets one, in the
  first header's style (``CRLF`` or ``LF``), so no row is glued to the next
  part's first row. An empty (0-byte) part file has no header and no rows.
* ``merge-json`` - ``*.json`` files are merged, every other file is
  concatenated. Every part file must be one JSON document (UTF-8, an optional
  BOM): all top-level **arrays** -> one array, elements in chunk order; all
  top-level **objects** -> one object, members in chunk order, a key seen twice
  (in two parts, or twice in one document at any depth) -> ``duplicate_key`` -
  never "last wins"; arrays mixed with objects, or a scalar at the top ->
  ``type_mismatch``; not JSON (an empty file too, ``NaN``, a number too large)
  -> ``invalid_json``; a part file over ``json_max_bytes`` -> ``too_large``
  (never parsed partly, never truncated). The merged document is written
  compactly (``,`` / ``:`` separators, UTF-8, a final ``\\n``).

**Refused parts** (``unsafe_part``): a part folder or anything inside it that is
a symlink, or anything that is neither a regular file nor a folder - checked for
every part before a byte is written (``lstat``, never following a link), and
every file is opened ``O_NOFOLLOW``. A path that is a file in one part and a
folder in another is ``type_mismatch``. More than ``max_output_bytes`` written
-> ``too_large``.

**Bounded memory.** Bytes are copied in fixed blocks; a CSV header is read up to
:data:`MAX_HEADER_BYTES`; ``merge-json`` holds one part's document at a time
(plus the set of an object's keys). The result is deterministic: the same parts
give the same bytes on every node (criterion 1).

The process answers with its exit code (0 ok, :data:`EXIT_COMBINE_ERROR` a data
error, anything else a crash) and a small JSON result file the agent reads.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import stat
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Final

__all__ = [
    "BUILTINS",
    "EXIT_COMBINE_ERROR",
    "MAX_HEADER_BYTES",
    "CombineError",
    "CombineStats",
    "combine",
    "main",
]

BUILTINS: Final = ("concat", "merge-csv", "merge-json")
EXIT_COMBINE_ERROR: Final = 2
"""The process's exit code for a refused combine (the result file says why)."""
MAX_HEADER_BYTES: Final = 1024 * 1024
"""Longest CSV header line (a longer one is ``too_large``)."""
MAX_PATHS: Final = 1_000_000
"""Most distinct relative paths over all parts (more is ``too_large``)."""
_BLOCK: Final = 1024 * 1024
_BOM: Final = b"\xef\xbb\xbf"
_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
_BINARY: Final = getattr(os, "O_BINARY", 0)
_DETAIL_MAX: Final = 400
CODES: Final = frozenset(
    {
        "header_mismatch",
        "duplicate_key",
        "type_mismatch",
        "unsafe_part",
        "too_large",
        "invalid_json",
    }
)


class CombineError(Exception):
    """The parts cannot be combined (``code`` is one of :data:`CODES`)."""

    def __init__(self, reason: str, message: str) -> None:
        if reason not in CODES:  # pragma: no cover - a programming error
            raise ValueError(f"unknown combine error code {reason!r}")
        super().__init__(message)
        self.code = reason
        self.message = _clip(message)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(slots=True)
class CombineStats:
    parts: int = 0
    files: int = 0
    bytes: int = 0


def _clip(text: str) -> str:
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in text)
    return cleaned[:_DETAIL_MAX]


def _shown(rel: str) -> str:
    """A part-relative path as it may appear in a detail (bounded)."""
    return _clip(rel)[:120]


# ------------------------------------------------------------------ the parts


def part_dirs(parts: Path) -> list[Path]:
    """The part folders in chunk order: every entry of ``parts`` must be a
    real folder named by its zero-padded chunk index, one width for all."""
    found: list[tuple[int, Path]] = []
    widths: set[int] = set()
    with os.scandir(parts) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                raise CombineError(
                    "unsafe_part",
                    f"parts/{_shown(entry.name)} is not a folder of the agent's",
                )
            if not entry.name.isdigit() or not entry.name.isascii():
                raise CombineError(
                    "unsafe_part", f"parts/{_shown(entry.name)} is not a chunk folder"
                )
            widths.add(len(entry.name))
            found.append((int(entry.name), Path(entry.path)))
    if len(widths) > 1:
        raise CombineError("unsafe_part", "the part folders have different widths")
    found.sort()
    if [i for i, _ in found] != list(range(len(found))):
        raise CombineError("unsafe_part", "the part folders are not chunks 0..n-1")
    return [path for _, path in found]


def _walk(root: Path, prefix: str = "") -> Iterator[tuple[str, bool]]:
    """``(relative POSIX path, is_folder)`` of everything under ``root``,
    never following a link; anything not a regular file or folder raises."""
    with os.scandir(root) as entries:
        listed = sorted(
            entries, key=lambda e: e.name.encode("utf-8", "surrogateescape")
        )
    for entry in listed:
        rel = f"{prefix}{entry.name}"
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            yield rel, True
            yield from _walk(Path(entry.path), rel + "/")
        elif stat.S_ISREG(info.st_mode):
            yield rel, False
        else:
            kind = "a symlink" if stat.S_ISLNK(info.st_mode) else "not a regular file"
            raise CombineError(
                "unsafe_part", f"{_shown(rel)} in {root.name}: {kind} in a part"
            )


def scan(parts: Sequence[Path]) -> list[str]:
    """Check every part (:func:`_walk`) and return the union of their file
    paths, sorted by their UTF-8 bytes. A path that is a file in one part
    and a folder in another is ``type_mismatch``."""
    files: set[str] = set()
    folders: set[str] = set()
    for part in parts:
        for rel, is_folder in _walk(part):
            (folders if is_folder else files).add(rel)
            if len(files) + len(folders) > MAX_PATHS:
                raise CombineError(
                    "too_large", f"the parts hold more than {MAX_PATHS} paths"
                )
    clash = files & folders
    if clash:
        rel = min(clash)
        raise CombineError(
            "type_mismatch",
            f"{_shown(rel)} is a file in one part and a folder in another",
        )
    return sorted(files, key=lambda r: r.encode("utf-8", "surrogateescape"))


def _open_part(path: Path) -> IO[bytes]:
    """A part file for reading - never through a link, only a regular file
    (the scan checked; this holds even if a part changed since)."""
    unsafe = CombineError("unsafe_part", f"{_shown(path.name)} is not a regular file")
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _BINARY)
    except OSError as exc:
        if os.path.islink(path):
            raise unsafe from None
        raise CombineError(
            "unsafe_part", f"{_shown(path.name)} cannot be read ({exc.strerror})"
        ) from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise unsafe
    return os.fdopen(fd, "rb", buffering=0)


# ------------------------------------------------------------------ output


class _Out:
    """One output file: exclusive create, every byte counted against the
    combine's ``max_output_bytes``."""

    def __init__(self, path: Path, stats: CombineStats, cap: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o644
        )
        self._fh = os.fdopen(fd, "wb")
        self._stats = stats
        self._cap = cap
        self.written = 0
        self.last: bytes = b""

    def write(self, data: bytes) -> None:
        if not data:
            return
        self._stats.bytes += len(data)
        if self._stats.bytes > self._cap:
            raise CombineError(
                "too_large",
                f"the combined output is over {self._cap} bytes",
            )
        self._fh.write(data)
        self.written += len(data)
        self.last = data[-1:]

    def copy_from(self, src: IO[bytes]) -> int:
        """The rest of ``src``; returns the bytes copied."""
        copied = 0
        while True:
            block = src.read(_BLOCK)
            if not block:
                return copied
            self.write(block)
            copied += len(block)

    def close(self) -> None:
        self._fh.close()


@contextlib.contextmanager
def _output(path: Path, stats: CombineStats, cap: int) -> Iterator[_Out]:
    out = _Out(path, stats, cap)
    try:
        yield out
    finally:
        out.close()
    stats.files += 1


def _concat(rel: str, sources: Sequence[Path], out: _Out) -> None:
    del rel
    for src in sources:
        with _open_part(src) as fh:
            out.copy_from(fh)


def _split_header(line: bytes) -> tuple[bytes, bytes]:
    """``(header without BOM and terminator, terminator)``."""
    body = line[len(_BOM) :] if line.startswith(_BOM) else line
    if body.endswith(b"\r\n"):
        return body[:-2], b"\r\n"
    if body.endswith(b"\n"):
        return body[:-1], b"\n"
    return body, b""


def _merge_csv(rel: str, sources: Sequence[Path], out: _Out) -> None:
    first: bytes | None = None
    style = b"\n"
    for src in sources:
        with _open_part(src) as raw, _buffered(raw) as fh:
            line = fh.readline(MAX_HEADER_BYTES + 1)
            if not line:
                continue  # an empty part file: no header, no rows
            if len(line) > MAX_HEADER_BYTES:
                raise CombineError(
                    "too_large",
                    f"{_shown(rel)}: a CSV header line is over {MAX_HEADER_BYTES} bytes",
                )
            header, term = _split_header(line)
            if first is None:
                first, style = header, term or b"\n"
                out.write(line)
                if not term:
                    out.write(style)
            elif header != first:
                raise CombineError(
                    "header_mismatch",
                    f"{_shown(rel)}: the header of part {_part_of(src, rel)} "
                    "differs from the first part's",
                )
            if out.copy_from(fh) and out.last != b"\n":
                out.write(style)


def _part_of(src: Path, rel: str) -> str:
    """The chunk folder name of a part file (``parts/NNNNN/<rel>``)."""
    depth = rel.count("/") + 1
    part = src
    for _ in range(depth):
        part = part.parent
    return part.name


def _buffered(raw: IO[bytes]) -> io.BufferedReader:
    """A buffered reader over an unbuffered part file (closes with it)."""
    return io.BufferedReader(raw, buffer_size=_BLOCK)  # type: ignore[arg-type]


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKeyError(key)
        out[key] = value
    return out


class _DuplicateKeyError(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _load_json(src: Path, rel: str, cap: int) -> Any:
    with _open_part(src) as fh:
        size = os.fstat(fh.fileno()).st_size
        if size > cap:
            raise CombineError(
                "too_large",
                f"{_shown(rel)} of part {_part_of(src, rel)} is {size} bytes; "
                f"merge-json parses at most {cap} per file "
                "([agent.split] combine_json_max_bytes)",
            )
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise CombineError("too_large", f"{_shown(rel)} grew past {cap} bytes")
    where = f"{_shown(rel)} of part {_part_of(src, rel)}"
    try:
        return json.loads(
            data.decode("utf-8-sig"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
        )
    except _DuplicateKeyError as exc:
        raise CombineError(
            "duplicate_key", f"{where}: key {_shown(repr(exc.key))} appears twice"
        ) from None
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise CombineError(
            "invalid_json", f"{where} is not one JSON document"
        ) from None


def _dumps(value: Any, rel: str) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (ValueError, RecursionError):
        raise CombineError(
            "invalid_json", f"{_shown(rel)} holds a number JSON cannot write back"
        ) from None


def _kind(value: Any) -> str:
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "scalar"


def _merge_json(rel: str, sources: Sequence[Path], out: _Out, cap: int) -> None:
    mode: str | None = None
    first = True
    keys: set[str] = set()
    for src in sources:
        value = _load_json(src, rel, cap)
        kind = _kind(value)
        if kind == "scalar":
            raise CombineError(
                "type_mismatch",
                f"{_shown(rel)} of part {_part_of(src, rel)} is not an array or an "
                "object at the top",
            )
        if mode is None:
            mode = kind
            out.write(b"[" if kind == "array" else b"{")
        elif kind != mode:
            raise CombineError(
                "type_mismatch",
                f"{_shown(rel)}: part {_part_of(src, rel)} holds an {kind}, an "
                f"earlier part an {mode}",
            )
        if kind == "array":
            for element in value:
                out.write((b"" if first else b",") + _dumps(element, rel))
                first = False
        else:
            for key, member in value.items():
                if key in keys:
                    raise CombineError(
                        "duplicate_key",
                        f"{_shown(rel)}: key {_shown(repr(key))} of part "
                        f"{_part_of(src, rel)} is also in an earlier part",
                    )
                keys.add(key)
                out.write(
                    (b"" if first else b",")
                    + _dumps(key, rel)
                    + b":"
                    + _dumps(member, rel)
                )
                first = False
        del value
    out.write(b"]\n" if mode == "array" else b"}\n")


def _merger(
    kind: str, rel: str, json_max_bytes: int
) -> Callable[[str, Sequence[Path], _Out], None]:
    lower = rel.lower()
    if kind == "merge-csv" and lower.endswith(".csv"):
        return _merge_csv
    if kind == "merge-json" and lower.endswith(".json"):
        return lambda r, s, o: _merge_json(r, s, o, json_max_bytes)
    return _concat


def combine(
    kind: str,
    parts: Path,
    out: Path,
    *,
    max_output_bytes: int,
    json_max_bytes: int,
) -> CombineStats:
    """Combine ``parts/NNNNN`` into the empty folder ``out`` (module docstring).
    Raises :class:`CombineError` (``out`` may then hold a partial result: the
    caller discards it)."""
    if kind not in BUILTINS:
        raise ValueError(f"unknown built-in combine {kind!r}")
    stats = CombineStats()
    dirs = part_dirs(parts)
    stats.parts = len(dirs)
    for rel in scan(dirs):
        segments = rel.split("/")
        sources = [d.joinpath(*segments) for d in dirs]
        present = [p for p in sources if os.path.lexists(p)]
        merge = _merger(kind, rel, json_max_bytes)
        with _output(out.joinpath(*segments), stats, max_output_bytes) as dest:
            merge(rel, present, dest)
    return stats


# ------------------------------------------------------------------ process


def _write_result(path: Path, doc: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m meshforge.agent.execution.combine KIND PARTS OUT RESULT
    --max-output-bytes N --json-max-bytes N``."""
    parser = argparse.ArgumentParser(prog="meshforge-combine")
    parser.add_argument("kind", choices=BUILTINS)
    parser.add_argument("parts", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("result", type=Path)
    parser.add_argument("--max-output-bytes", type=int, required=True)
    parser.add_argument("--json-max-bytes", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        stats = combine(
            args.kind,
            args.parts,
            args.out,
            max_output_bytes=args.max_output_bytes,
            json_max_bytes=args.json_max_bytes,
        )
    except CombineError as exc:
        _write_result(
            args.result, {"ok": False, "code": exc.code, "message": exc.message}
        )
        sys.stderr.write(f"meshforge combine {args.kind}: refused: {exc}\n")
        return EXIT_COMBINE_ERROR
    _write_result(
        args.result,
        {"ok": True, "parts": stats.parts, "files": stats.files, "bytes": stats.bytes},
    )
    sys.stdout.write(
        f"meshforge combine {args.kind}: {stats.parts} parts -> {stats.files} "
        f"files, {stats.bytes} bytes\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - run as a process by the agent
    raise SystemExit(main())
