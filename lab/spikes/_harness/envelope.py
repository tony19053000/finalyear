"""The result envelope every spike writes: one deterministic JSON document.

Shape (``schema_version`` 1)::

    {
      "schema_version": 1, "spike": "S1", "topic": "S1-network",
      "tool": {...}, "git": {"commit", "dirty", ...}, "host": {"hostname", "os", ...},
      "timestamp_utc": "2026-09-15T10:00:00Z", "link": "wifi" | "ethernet",
      "params": {...}, "cases": {<case_id>: CaseResult.to_json()},
      "errors": [...run-level errors...], "extra": {...spike specific...}
    }

``link`` is given by the operator (the tool cannot know which physical link a
route uses). Raw files go to ``lab/reports/raw/<YYYY-MM-DD>-<topic>/<host>.json``
(``<host>__<suffix>.json`` when one host writes several runs, e.g. one per peer).
The ``<host>`` part is case-folded and hash-suffixed when that changes it (see
:func:`safe_name`) so hosts cannot collide on Windows; the original spelling
lives in ``host.label``, which is what matrix and report display.
Every string passes the ``_inventory`` privacy sanitizer before writing.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import socket
import sys
import unicodedata
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

from _inventory import runner
from _inventory.parse_common import parse_git_status
from _inventory.sanitize import Sanitizer
from spikes._harness.atomic import atomic_write_text
from spikes._harness.reps import JSON, CaseResult

SCHEMA_VERSION: Final = 1
LINKS: Final = ("wifi", "ethernet")
LAB_DIR: Final = Path(__file__).resolve().parents[2]
REPO_ROOT: Final = LAB_DIR.parent
RAW_ROOT: Final = LAB_DIR / "reports" / "raw"
_REQUIRED: Final = (
    "schema_version",
    "spike",
    "topic",
    "host",
    "timestamp_utc",
    "link",
    "params",
    "cases",
    "errors",
)
_UNSAFE_NAME: Final = re.compile(r"[^A-Za-z0-9._-]+")


class EnvelopeError(ValueError):
    """A result file is missing, unreadable, or not a valid envelope."""


class ResultCollisionError(FileExistsError):
    """The target result file belongs to a different host (an ``OSError``)."""


def git_info(repo: Path = REPO_ROOT) -> dict[str, JSON]:
    """Short commit + dirty flag (file names are not recorded). Never raises."""
    head = runner.run("git", ["rev-parse", "--short", "HEAD"], cwd=repo, timeout=10)
    if not head.ok:
        return {"commit": None, "dirty": None, "error": head.describe_failure()}
    info: dict[str, JSON] = {"commit": head.stdout.strip()}
    status = runner.run("git", ["status", "--porcelain"], cwd=repo, timeout=10)
    if status.ok:
        info.update(parse_git_status(status.stdout))
    else:
        info.update(dirty=None, error=status.describe_failure())
    return info


def host_info() -> dict[str, JSON]:
    return {
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def utc_timestamp(now: datetime | None = None) -> str:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_envelope(
    *,
    spike: str,
    topic: str,
    link: str,
    params: Mapping[str, JSON],
    cases: list[CaseResult],
    errors: list[dict[str, JSON]] | None = None,
    extra: Mapping[str, JSON] | None = None,
    tool: Mapping[str, JSON] | None = None,
    git: Mapping[str, JSON] | None = None,
    host: Mapping[str, JSON] | None = None,
    host_label: str | None = None,
    now: datetime | None = None,
) -> dict[str, JSON]:
    """Build (and sanitise) an envelope. ``git``/``host``/``now`` are test seams.

    ``host_label`` is the identity reports use for this host (e.g. ``--name``);
    it defaults to the hostname and is stored as ``host.label``.
    """
    if link not in LINKS:
        raise ValueError(f"link must be one of {LINKS}, got {link!r}")
    ids = [c.case_id for c in cases]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate case ids: {ids}")
    host_doc: dict[str, JSON] = dict(host) if host is not None else host_info()
    host_doc["label"] = host_label or host_doc.get("label") or host_doc.get("hostname")
    doc: dict[str, JSON] = {
        "schema_version": SCHEMA_VERSION,
        "spike": spike,
        "topic": topic,
        "tool": dict(tool or {}),
        "git": dict(git) if git is not None else git_info(),
        "host": host_doc,
        "timestamp_utc": utc_timestamp(now),
        "link": link,
        "params": dict(params),
        "cases": {c.case_id: c.to_json() for c in cases},
        "errors": list(errors or []),
        "extra": dict(extra or {}),
    }
    return sanitize(doc)


def sanitize(doc: dict[str, JSON]) -> dict[str, JSON]:
    try:
        home: Path | None = Path.home()
    except (RuntimeError, KeyError):
        home = None
    clean: dict[str, JSON] = Sanitizer(home).tree(doc)
    return clean


def dumps(doc: Mapping[str, JSON]) -> str:
    """Deterministic JSON: sorted keys, 2-space indent, no NaN, newline-terminated."""
    return (
        json.dumps(doc, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n"
    )


def safe_name(text: str) -> str:
    """A string usable as a file-name part on Linux and Windows.

    The name is NFC-normalised, **case-folded**, and reduced to
    ``[a-z0-9._-]``. Case folding matters because NTFS (and macOS) compare file
    names case-insensitively: hosts ``Lab1`` and ``lab1`` would otherwise write
    to the same file on the team's Windows laptops while looking distinct on
    Linux. Whenever the derived name differs from the original string -- a case
    difference, a sanitised character, or both -- the first 8 hex digits of the
    SHA-256 of the original NFC UTF-8 form are appended, so distinct hosts never
    share a file name (including two all-non-ASCII hostnames that would both
    reduce to ``unknown``).

    The original spelling is kept inside the JSON as ``host.label``; matrix and
    report identify hosts by that label, never by the file name.
    """
    norm = unicodedata.normalize("NFC", text)
    cleaned = _UNSAFE_NAME.sub("_", norm.casefold()).strip("._")
    if cleaned == text:
        return cleaned or "unknown"
    digest = hashlib.sha256(norm.encode("utf-8"))
    return f"{cleaned or 'unknown'}-{digest.hexdigest()[:8]}"


def safe_topic(topic: str) -> str:
    """Sanitise a topic for the raw directory name, **keeping its case**.

    Topics are fixed identifiers chosen in code (``S1-network``), not host
    input, so they cannot collide by case; and the mixed-case directory name is
    what docs/LAB.md, lab/README.md and the report header all spell out
    (``lab/reports/raw/YYYY-MM-DD-S1-network/``).
    """
    norm = unicodedata.normalize("NFC", topic)
    cleaned = _UNSAFE_NAME.sub("_", norm).strip("._")
    if cleaned == topic:
        return cleaned or "unknown"
    digest = hashlib.sha256(norm.encode("utf-8"))
    return f"{cleaned or 'unknown'}-{digest.hexdigest()[:8]}"


def raw_dir(topic: str, day: date, root: Path = RAW_ROOT) -> Path:
    return root / f"{day.isoformat()}-{safe_topic(topic)}"


def raw_result_path(
    topic: str,
    hostname: str,
    day: date,
    *,
    suffix: str | None = None,
    root: Path = RAW_ROOT,
) -> Path:
    name = safe_name(hostname)
    if suffix:
        name += "__" + safe_name(suffix)
    return raw_dir(topic, day, root) / f"{name}.json"


def _hostname_of(doc: Mapping[str, JSON]) -> str | None:
    host = doc.get("host")
    name = host.get("hostname") if isinstance(host, dict) else None
    return name if isinstance(name, str) else None


def write_envelope(doc: Mapping[str, JSON], path: Path) -> None:
    """Atomically write ``doc`` to ``path`` (parents created). Raises ``OSError``.

    An existing file written by a *different* host (``host.hostname``) is never
    overwritten: :class:`ResultCollisionError` is raised instead. A re-run on
    the same host replaces its own file; an unreadable/invalid existing file
    is replaced (it holds no usable result).
    """
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            old = None
        old_host = _hostname_of(old) if isinstance(old, dict) else None
        new_host = _hostname_of(doc)
        if old_host is not None and old_host != new_host:
            raise ResultCollisionError(
                f"{path} already holds results from host {old_host!r}; "
                f"refusing to overwrite it with results from {new_host!r}"
            )
    atomic_write_text(path, dumps(doc))


def host_label(doc: Mapping[str, JSON]) -> str:
    """``host.label`` if set, else ``host.hostname``."""
    host = doc.get("host") or {}
    label = host.get("label") if isinstance(host, dict) else None
    if isinstance(label, str) and label:
        return label
    return str(host.get("hostname") if isinstance(host, dict) else "unknown")


def validate(doc: JSON, *, source: str = "<doc>") -> dict[str, JSON]:
    if not isinstance(doc, dict):
        raise EnvelopeError(f"{source}: not a JSON object")
    missing = [k for k in _REQUIRED if k not in doc]
    if missing:
        raise EnvelopeError(f"{source}: missing keys {missing}")
    if doc["schema_version"] != SCHEMA_VERSION:
        raise EnvelopeError(
            f"{source}: schema_version {doc['schema_version']!r} != {SCHEMA_VERSION}"
        )
    if doc["link"] not in LINKS:
        raise EnvelopeError(f"{source}: link {doc['link']!r} not in {LINKS}")
    if not isinstance(doc["cases"], dict) or not isinstance(doc["host"], dict):
        raise EnvelopeError(f"{source}: 'cases' and 'host' must be objects")
    if not isinstance(doc["host"].get("hostname"), str):
        raise EnvelopeError(f"{source}: host.hostname missing")
    return doc


def load_envelope(path: Path) -> dict[str, JSON]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EnvelopeError(f"{path}: cannot read: {exc}") from exc
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"{path}: invalid JSON: {exc}") from exc
    return validate(doc, source=str(path))


def load_dir(directory: Path) -> tuple[list[dict[str, JSON]], list[str]]:
    """Every ``*.json`` envelope in ``directory`` (sorted by name), plus a list of
    ``"<file>: <reason>"`` for files that were skipped as invalid."""
    docs: list[dict[str, JSON]] = []
    skipped: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            doc = load_envelope(path)
        except EnvelopeError as exc:
            skipped.append(str(exc))
            continue
        doc.setdefault("_source", path.name)
        docs.append(doc)
    return docs, skipped


def interpreter() -> str:
    """Interpreter description for ``tool`` (no path: it may contain a user name)."""
    return (
        f"{platform.python_implementation()} {platform.python_version()} {sys.platform}"
    )
