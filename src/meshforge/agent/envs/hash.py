"""The env hash (V0.5 D85): same inputs -> same environment, on every node and OS.

``env_hash = sha256(canonical JSON of EnvInputs)`` where the inputs are:

=================  ==========================================================
``runtime_kind``   ``docker`` (an image), ``venv`` (a uv venv), ``subprocess``
                   (a ``--target`` dir for the agent's own interpreter)
``base``           Docker: the pinned base image's ``sha256:`` digest;
                   venv/subprocess: the interpreter, e.g. ``cpython-3.12.3``
``python``         the manifest's ``env.python`` (``""`` when not set)
``requirements``   the **normalised** requirements bytes (below), hashed
``platform``       Docker: ``<os>/<arch>`` of the local base image;
                   venv/subprocess: the interpreter's ``sysconfig.get_platform()``
=================  ==========================================================

The canonical form is JSON with sorted keys, no whitespace, ASCII only, plus a
format version (:data:`ENV_HASH_VERSION`): it holds no path, timestamp, user or
OS-specific separator, so a fixed input gives a fixed hash everywhere (golden
tests pin it).

**Requirements normalisation.** V0.5 hashes *what the user wrote*, never a network
resolution: the text is decoded as UTF-8 (a BOM is dropped), newlines unified,
``\\`` continuations joined, comments (``#`` at the start or after whitespace)
and blank lines removed, runs of whitespace collapsed to one space, duplicates
dropped, and the lines sorted. Comment or order changes therefore keep the hash;
any version change alters it.

Lines the hash **cannot cover** are refused (``requirements_rejected``), because
two different environments would get the same hash: ``-r``/``-c`` includes and
``-e`` editables (other files), and local paths / ``file:`` URLs (their contents
are not in this file). Size and line count are bounded (settings).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final

from meshforge.agent.envs.errors import EnvError

__all__ = [
    "ENV_HASH_VERSION",
    "RUNTIME_KINDS",
    "EnvInputs",
    "canonical_bytes",
    "env_hash",
    "normalize_requirements",
]

ENV_HASH_VERSION: Final = 1
RUNTIME_KINDS: Final = frozenset({"docker", "venv", "subprocess"})
_FIELD_MAX: Final = 256
_COMMENT: Final = re.compile(r"(^|\s)#.*$")
_SPACES: Final = re.compile(r"\s+")
_INCLUDE_OPTIONS: Final = (
    "-r",
    "--requirement",
    "-c",
    "--constraint",
    "-e",
    "--editable",
)
_DRIVE: Final = re.compile(r"[A-Za-z]:[\\/]")


@dataclass(frozen=True, slots=True)
class EnvInputs:
    """Everything an environment depends on (module docstring)."""

    runtime_kind: str
    base: str
    python: str
    requirements: bytes
    """Already normalised (:func:`normalize_requirements`)."""
    platform: str

    def __post_init__(self) -> None:
        if self.runtime_kind not in RUNTIME_KINDS:
            raise ValueError(f"runtime_kind must be one of {sorted(RUNTIME_KINDS)}")
        for name in ("base", "python", "platform"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > _FIELD_MAX:
                raise ValueError(f"{name} must be a string of <= {_FIELD_MAX} chars")
            if not value.isascii() or not value.isprintable():
                raise ValueError(f"{name} must be printable ASCII")
        if not self.base or not self.platform:
            raise ValueError("base and platform must not be empty")


def canonical_bytes(inputs: EnvInputs) -> bytes:
    """The exact bytes that are hashed (stable across OSes and Python versions)."""
    document = {
        "v": ENV_HASH_VERSION,
        "runtime_kind": inputs.runtime_kind,
        "base": inputs.base,
        "python": inputs.python,
        "requirements_sha256": hashlib.sha256(inputs.requirements).hexdigest(),
        "platform": inputs.platform,
    }
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def env_hash(inputs: EnvInputs) -> str:
    """64 lowercase hex characters (no ``sha256:`` prefix: it names dirs/tags)."""
    return hashlib.sha256(canonical_bytes(inputs)).hexdigest()


def _rejected(message: str) -> EnvError:
    return EnvError(message, reason="requirements_rejected")


def _logical_lines(text: str) -> list[str]:
    """Physical lines with ``\\`` continuations joined (pip's rule)."""
    lines: list[str] = []
    pending = ""
    for raw in text.split("\n"):
        if raw.endswith("\\"):
            pending += raw[:-1]
            continue
        lines.append(pending + raw)
        pending = ""
    if pending:
        lines.append(pending)
    return lines


def _check_line(line: str, number: int) -> None:
    if any(not ch.isprintable() for ch in line):
        raise _rejected(f"requirements line {number} has a control character")
    head = line.split(" ", 1)[0].split("=", 1)[0]
    if head in _INCLUDE_OPTIONS or any(
        line.startswith(opt) and len(opt) == 2 and line[2:3] != "-"
        for opt in ("-r", "-c", "-e")
    ):
        raise _rejected(
            f"requirements line {number}: -r/-c/-e (another file or an editable "
            "install) is not supported - the env hash covers only this file; inline "
            "the requirements instead"
        )
    lowered = line.lower()
    if (
        line.startswith((".", "/", "\\", "~"))
        or _DRIVE.match(line)
        or lowered.startswith("file:")
        or "@ file:" in lowered
        or "@file:" in lowered
    ):
        raise _rejected(
            f"requirements line {number}: a local path or file: URL is not "
            "supported - its contents are not covered by the env hash"
        )


def normalize_requirements(
    data: bytes, *, max_bytes: int = 65_536, max_lines: int = 1000
) -> bytes:
    """Canonical requirements bytes (module docstring). Raises :class:`EnvError`
    (``requirements_rejected``) for an oversize, non-UTF-8 or unhashable file."""
    if len(data) > max_bytes:
        raise _rejected(
            f"the requirements file is {len(data)} bytes; the maximum is {max_bytes}"
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _rejected("the requirements file is not UTF-8") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    kept: set[str] = set()
    for number, raw in enumerate(_logical_lines(text), start=1):
        line = _SPACES.sub(" ", _COMMENT.sub("", raw)).strip()
        if not line:
            continue
        _check_line(line, number)
        kept.add(line)
        if len(kept) > max_lines:
            raise _rejected(f"the requirements file has more than {max_lines} entries")
    if not kept:
        return b""
    return ("\n".join(sorted(kept)) + "\n").encode("utf-8")
