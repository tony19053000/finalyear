"""The local score cache, its fingerprint, and the one-scorer lock (brief §5g D34/D35).

Files, both in the agent's ``data_dir`` next to ``identity/`` (ARCHITECTURE §23):

* ``scores.json`` - the last :class:`~.model.ScoreSet` plus the fingerprint inputs
  it was measured under, written atomically (``common/atomic.py``).
* ``scores.lock`` - an advisory lock (``common/filelock.py``) held for a whole
  scoring run, so the agent's background run and ``meshforge-agent hardware
  score`` never measure at the same time (they would slow each other down and
  both numbers would be wrong). The second scorer does not wait: it gets
  :class:`ScorerBusyError` ("already running").

**Fingerprint** = SHA-256 of the canonical JSON of: CPU model and thread count, RAM
rounded to GiB, each GPU's NVML index/model/VRAM/driver, the installed torch and
numpy versions, and :data:`~.scores.SCORE_VERSION`. A different fingerprint means
the cached numbers describe other hardware or software - the cache is *stale* and
the node re-scores. Backend versions come from ``importlib.metadata``, so computing
the fingerprint never imports torch or numpy into the agent.

A cache that cannot be read or parsed is **not fatal** (unlike the identity): it is
logged as ``agent.scores.cache_corrupt`` and treated as missing.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from meshforge.agent.benchmarks.model import ScoreSet
from meshforge.agent.benchmarks.scores import SCORE_VERSION
from meshforge.agent.hardware.summary import HardwareFacts
from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.filelock import locked
from meshforge.common.logging import get_logger

__all__ = [
    "CACHE_FILE_NAME",
    "CACHE_FORMAT",
    "LOCK_FILE_NAME",
    "CacheState",
    "CacheStatus",
    "ScorerBusyError",
    "backend_versions",
    "cache_mtime_ns",
    "cache_path",
    "fingerprint",
    "fingerprint_inputs",
    "load_cache",
    "save_cache",
    "scorer_lock",
]

log = get_logger(__name__)

CACHE_FILE_NAME: Final = "scores.json"
LOCK_FILE_NAME: Final = "scores.lock"
CACHE_FORMAT: Final = 1
_MAX_CACHE_BYTES: Final = 1 << 20
_GIB: Final = 2**30
_BACKENDS: Final = ("torch", "numpy")


class ScorerBusyError(Exception):
    """Another process holds ``scores.lock``: a scoring run is already in progress."""


def backend_versions() -> dict[str, str | None]:
    """Installed torch/numpy versions from package metadata (no import)."""
    out: dict[str, str | None] = {}
    for name in _BACKENDS:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = None
    return out


def fingerprint_inputs(
    facts: HardwareFacts, backends: Mapping[str, str | None]
) -> dict[str, Any]:
    """Everything a score depends on, as a JSON-safe mapping (see module docstring)."""
    return {
        "score_version": SCORE_VERSION,
        "cpu_model": facts.cpu.model,
        "cpu_threads": facts.cpu.logical_threads,
        "ram_gib": round(max(facts.cpu.ram_bytes, 0) / _GIB),
        "gpus": [
            {
                "index": gpu.index,
                "model": gpu.model,
                "vram_bytes": gpu.vram_bytes,
                "driver_version": gpu.driver_version,
            }
            for gpu in sorted(facts.gpus, key=lambda g: g.index)
        ],
        "backends": {name: backends.get(name) for name in sorted(backends)},
    }


def fingerprint(inputs: Mapping[str, Any]) -> str:
    """SHA-256 hex of the canonical JSON of :func:`fingerprint_inputs`."""
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CacheStatus(StrEnum):
    VALID = "valid"
    """Measured on this hardware/software with this score version."""
    STALE = "stale"
    """Readable, but the fingerprint or score version differs: re-score."""
    MISSING = "missing"
    CORRUPT = "corrupt"
    """Unreadable or malformed; logged and treated like MISSING."""


@dataclass(frozen=True, slots=True)
class CacheState:
    status: CacheStatus
    scores: ScoreSet | None = None
    mtime_ns: int | None = None
    reason: str = ""

    @property
    def valid(self) -> bool:
        return self.status is CacheStatus.VALID


def cache_path(data_dir: Path) -> Path:
    return data_dir / CACHE_FILE_NAME


def load_cache(
    data_dir: Path, current_fingerprint: str, **log_fields: Any
) -> CacheState:
    """Read ``scores.json`` and judge it against ``current_fingerprint``. Never raises."""
    path = cache_path(data_dir)
    try:
        stat = path.stat()
        if stat.st_size > _MAX_CACHE_BYTES:
            return _corrupt(f"larger than {_MAX_CACHE_BYTES} bytes", log_fields)
        raw = path.read_bytes()
    except FileNotFoundError:
        return CacheState(CacheStatus.MISSING)
    except OSError as exc:
        return _corrupt(f"unreadable: {exc.strerror or type(exc).__name__}", log_fields)
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict) or doc.get("format") != CACHE_FORMAT:
            return _corrupt("unknown cache format", log_fields, stat.st_mtime_ns)
        scores = ScoreSet.model_validate(doc.get("scores"))
    except (ValueError, ValidationError) as exc:
        # ValidationError subclasses ValueError; named for the reader.
        return _corrupt(
            f"malformed: {type(exc).__name__}", log_fields, stat.st_mtime_ns
        )
    if scores.score_version != SCORE_VERSION:
        return CacheState(
            CacheStatus.STALE,
            scores,
            stat.st_mtime_ns,
            f"score_version {scores.score_version} != {SCORE_VERSION}",
        )
    if scores.fingerprint != current_fingerprint:
        return CacheState(
            CacheStatus.STALE,
            scores,
            stat.st_mtime_ns,
            "hardware or backend changed since the last run",
        )
    return CacheState(CacheStatus.VALID, scores, stat.st_mtime_ns)


def _corrupt(
    reason: str, log_fields: Mapping[str, Any], mtime_ns: int | None = None
) -> CacheState:
    log.warning(
        "agent.scores.cache_corrupt",
        **log_fields,
        file=CACHE_FILE_NAME,
        reason=reason,
        action="treated as missing; the node re-scores",
    )
    return CacheState(CacheStatus.CORRUPT, None, mtime_ns, reason)


def save_cache(data_dir: Path, scores: ScoreSet, inputs: Mapping[str, Any]) -> None:
    """Atomically replace ``scores.json``. Raises ``OSError`` on failure."""
    data_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "format": CACHE_FORMAT,
        "fingerprint_inputs": dict(inputs),
        "scores": json.loads(scores.to_json()),
    }
    data = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write_bytes(cache_path(data_dir), data, mode=0o600)


def cache_mtime_ns(data_dir: Path) -> int | None:
    try:
        return cache_path(data_dir).stat().st_mtime_ns
    except OSError:
        return None


@contextlib.contextmanager
def scorer_lock(data_dir: Path) -> Iterator[None]:
    """Hold ``scores.lock`` for a whole run; :class:`ScorerBusyError` if taken.

    Never waits (``timeout_s=0``): the agent's background run skips (its poller will
    pick the other scorer's result up) and the CLI reports "already running".
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    stack = contextlib.ExitStack()
    try:
        # Only *acquiring* is translated: a TimeoutError from the caller's body
        # keeps its own meaning.
        stack.enter_context(locked(data_dir / LOCK_FILE_NAME, timeout_s=0.0))
    except TimeoutError:
        raise ScorerBusyError("another scoring run holds scores.lock") from None
    with stack:
        yield
