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
dropped, and the lines sorted. Requirement lines are also put in one spelling
(format version 2): the project name and extras normalised per PEP 503 (lowercase,
runs of ``-_.`` -> ``-``; extras sorted), no whitespace inside the version
specifier, the environment marker after ``" ; "``, and per-requirement options
(``--hash=...``) kept after one space, as ``--name=value``, sorted. ``Requests == 2`` and
``requests==2`` therefore share an env. Comment, order or spelling changes keep
the hash; any version change alters it.

Lines the hash **cannot cover** are refused (``requirements_rejected``), because
two different environments would get the same hash: ``-r``/``-c`` includes and
``-e`` editables (other files), local paths / ``file:`` URLs, and any direct
reference (``name @ ...``) that is not an ``http(s)://`` or VCS (``git+``, ``hg+``,
``svn+``, ``bzr+``) URL (their contents are not in this file). Size and line count
are bounded (settings).
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

ENV_HASH_VERSION: Final = 2
"""2 (2026-10-01): PEP 503 names + canonical specifier spelling (G2 fix round)."""
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
_NAME_RUNS: Final = re.compile(r"[-_.]+")
_REQUIREMENT: Final = re.compile(
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*"
    r"(?:\[(?P<extras>[^\]]*)\])?\s*"
    r"(?:@\s*(?P<url>\S+)\s*)?"
    r"(?P<spec>[^;@]*?)\s*"
    r"(?:;\s*(?P<marker>.*))?"
)
_REMOTE_URL: Final = re.compile(
    r"(?:https?|(?:git|hg|svn|bzr)\+(?:https?|ssh|git))://\S+", re.IGNORECASE
)
"""Direct references the hash can stand for: http(s), or a VCS over a network
scheme. ``git+file://``, ``git+/path``, ``ftp:``, ``data:`` ... are refused."""
_OPTION_SPLIT: Final = re.compile(r"\s+(?=--)")


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


def _pep503(name: str) -> str:
    return _NAME_RUNS.sub("-", name).lower()


def _canonical_line(line: str, number: int) -> str:
    """One requirement line in its single spelling (module docstring); option
    lines (``--index-url ...``) and bare URLs are kept as written."""
    if line.startswith("-") or "://" in line.split("@", 1)[0]:
        return line
    parts = _OPTION_SPLIT.split(line, maxsplit=1)
    canonical = _canonical_requirement(parts[0], number)
    options = _canonical_options(parts[1:])
    return f"{canonical} {options}" if options else canonical


def _canonical_options(parts: list[str]) -> str:
    """Per-requirement options (``--hash=sha256:...`` from pip-compile
    ``--generate-hashes``): kept, each as ``--name=value``, sorted (their order
    does not matter to the installer), separated from the requirement."""
    if not parts:
        return ""
    tokens = parts[0].split()
    options: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if (
            token.startswith("--")
            and "=" not in token
            and i + 1 < len(tokens)
            and not tokens[i + 1].startswith("-")
        ):
            token = f"{token}={tokens[i + 1]}"
            i += 1
        options.append(token)
        i += 1
    return " ".join(sorted(set(options)))


def _canonical_requirement(line: str, number: int) -> str:
    match = _REQUIREMENT.fullmatch(line)
    if match is None:
        if "@" in line:  # an unparsed direct reference: cannot prove it is remote
            raise _rejected(
                f"requirements line {number}: a direct reference this parser cannot "
                "read; write it as `name @ https://...`"
            )
        return line  # pip will say what is wrong with it; the hash stays exact
    name = _pep503(match["name"])
    extras = ""
    if match["extras"] is not None:
        parts = sorted(
            {_pep503(e.strip()) for e in match["extras"].split(",") if e.strip()}
        )
        extras = f"[{','.join(parts)}]" if parts else ""
    marker = match["marker"]
    tail = f" ; {_SPACES.sub(' ', marker).strip()}" if marker else ""
    url = match["url"]
    if url is not None:
        if not _REMOTE_URL.fullmatch(url):
            raise _rejected(
                f"requirements line {number}: a direct reference must be an http(s) "
                "or VCS URL - a local path's contents are not covered by the env hash"
            )
        if match["spec"]:
            return line  # unusual shape: keep it exact
        return f"{name}{extras} @ {url}{tail}"
    spec = _SPACES.sub("", match["spec"])
    return f"{name}{extras}{spec}{tail}"


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
        kept.add(_canonical_line(line, number))
        if len(kept) > max_lines:
            raise _rejected(f"the requirements file has more than {max_lines} entries")
    if not kept:
        return b""
    return ("\n".join(sorted(kept)) + "\n").encode("utf-8")
