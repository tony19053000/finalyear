"""Evict least-recently-used blobs and environments over quota (V0.5 G5, D91).

A pass (blocking; :class:`~meshforge.agent.gc.service.GcService` runs it in a
thread) does, per kind:

1. **reconcile** the index with what is on disk (CAS blobs; env dirs
   ``envs/{venv,subprocess}/<hash>``; env images ``meshforge-env:<hash>``),
2. while the kind's usage is over its quota (``[agent.gc] cas_bytes`` /
   ``envs_bytes``; 0 = no quota), take the next batch of LRU victims that no
   lease (a running attempt) or pin (a pinned named input) protects and that
   were last used at least ``min_age_s`` ago, and remove them **inside the
   index transaction** (see :mod:`.index` for why that closes the race with a
   starting attempt),
3. stop when usage <= quota, or when nothing evictable is left (logged
   ``agent.gc.over_quota``: what remains is in use or pinned).

Removal never leaves a half-visible item: a blob is one ``unlink`` (the CAS
observer then sends the cache-report ``removed`` delta and withdraws every
``tree`` it completed, D100); a directory env is first **renamed** to
``<hash>.gc-<id>`` (atomic: the env is gone for every reader) and deleted after
the transaction - a crash leaves only a ``.gc-`` folder, swept by the next
pass; an env image is ``docker image rm`` without force (Docker refuses while a
container uses it). An env is removed only while holding its build lock
(``envs/locks/<hash>.lock``, taken without waiting: a busy env is skipped), so
a build or a reuse in another process never races its removal. Base images
are never items, so never evicted.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from meshforge.agent.envs.fsutil import remove_tree
from meshforge.common.cas import CasStore
from meshforge.common.filelock import locked
from meshforge.common.gc_index import GcIndex, Victim, collect_kind
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger

__all__ = [
    "DIR_ENV_KINDS",
    "GarbageCollector",
    "GcConfig",
    "GcReport",
    "scan_blobs",
    "scan_env_dirs",
]

log = get_logger(__name__)

DIR_ENV_KINDS: Final = ("venv", "subprocess")
_ENV_MARKER: Final = ".meshforge-env.json"
_MAX_MARKER_BYTES: Final = 64 * 1024
_HEX_RE: Final = re.compile(r"[0-9a-f]{64}")
_GC_SUFFIX: Final = ".gc-"


@dataclass(frozen=True, slots=True)
class GcConfig:
    cas_bytes: int = 0
    """Quota of the CAS (bytes; 0 = none)."""
    envs_bytes: int = 0
    """Quota of env dirs + env images together (bytes; 0 = none)."""
    min_age_s: float = 300.0
    batch: int = 256


@dataclass(slots=True)
class GcReport:
    reason: str
    evicted: dict[str, int] = field(default_factory=dict)
    evicted_bytes: dict[str, int] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    """Bytes per kind after the pass."""
    over_quota: list[str] = field(default_factory=list)
    """Kinds still over quota (only protected items left)."""
    skipped: int = 0
    """Victims left alone (env busy/in use, image in use, removal failed)."""
    duration_s: float = 0.0

    def count(self, victim: Victim) -> None:
        self.evicted[victim.kind] = self.evicted.get(victim.kind, 0) + 1
        self.evicted_bytes[victim.kind] = (
            self.evicted_bytes.get(victim.kind, 0) + victim.size
        )


def scan_blobs(store: CasStore) -> dict[str, int]:
    """Every blob in ``store``: hex -> size (blocking)."""
    return store.listing()


def scan_env_dirs(root: Path) -> dict[str, int]:
    """Built directory envs: ``<kind>/<hash>`` -> bytes (from the marker)."""
    out: dict[str, int] = {}
    for kind in DIR_ENV_KINDS:
        try:
            entries = list((root / kind).iterdir())
        except OSError:
            continue
        for entry in entries:
            if not _HEX_RE.fullmatch(entry.name):
                continue
            try:
                with (entry / _ENV_MARKER).open("rb") as fh:
                    data = json.loads(fh.read(_MAX_MARKER_BYTES))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("env_hash") != entry.name:
                continue
            summary = data.get("summary")
            size = summary.get("bytes", 0) if isinstance(summary, dict) else 0
            out[f"{kind}/{entry.name}"] = (
                int(size) if isinstance(size, int) and size >= 0 else 0
            )
    return out


class GarbageCollector:
    """See module docstring. Blocking; one pass at a time (the service's job)."""

    def __init__(
        self,
        index: GcIndex,
        store: CasStore,
        config: GcConfig,
        *,
        envs_root: Path | None = None,
        list_images: Callable[[], Mapping[str, int]] | None = None,
        remove_image: Callable[[str], bool] | None = None,
        wall: Callable[[], float] = time.time,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if config.batch < 1:
            raise ValueError("batch >= 1")
        self.index = index
        self.store = store
        self.config = config
        self.envs_root = envs_root
        self._list_images = list_images
        self._remove_image = remove_image
        self._wall = wall
        self._clock = clock
        self._pending_dirs: list[Path] = []

    # ------------------------------------------------------------ reconcile

    def reconcile(self) -> None:
        """Index rows == what is on disk (blobs, env dirs, env images)."""
        self.index.reconcile("blob", scan_blobs(self.store))
        envs: dict[str, int] = {}
        if self.envs_root is not None:
            envs.update(scan_env_dirs(self.envs_root))
            self._sweep_gc_dirs()
        if self._list_images is not None:
            try:
                images = self._list_images()
            except Exception as exc:  # noqa: BLE001 - Docker gone: keep old rows
                log.info("agent.gc.images_unlisted", cause=type(exc).__name__)
                envs.update(self._known_images())
            else:
                envs.update({f"image/{h}": size for h, size in images.items()})
        self.index.reconcile("env", envs)

    def _known_images(self) -> dict[str, int]:
        with self.index.tx() as db:
            return dict(
                db.execute(
                    "SELECT key, size FROM item WHERE kind = 'env' "
                    "AND key LIKE 'image/%'"
                )
            )

    def _sweep_gc_dirs(self) -> None:
        """Delete ``<hash>.gc-*`` folders a crashed pass left behind."""
        if self.envs_root is None:
            return
        for kind in DIR_ENV_KINDS:
            with contextlib.suppress(OSError):
                for entry in (self.envs_root / kind).glob(f"*{_GC_SUFFIX}*"):
                    try:
                        remove_tree(entry)
                    except OSError as exc:
                        log.info(
                            "agent.gc.leftover_kept",
                            path=entry.name,
                            cause=type(exc).__name__,
                        )

    # ------------------------------------------------------------------ pass

    def run_pass(self, reason: str = "periodic", *, reconcile: bool = True) -> GcReport:
        """One pass. ``reconcile=False`` trusts the index rows (kept current by
        the CAS observer and leases) and skips the disk scan - the cheap pass
        asked for after every staging."""
        started = self._clock()
        report = GcReport(reason)
        self.index.reap_dead_owners()
        if reconcile:
            self.reconcile()
        self._collect("blob", self.config.cas_bytes, report)
        self._collect("env", self.config.envs_bytes, report)
        report.duration_s = self._clock() - started
        log.info(
            "agent.gc.pass",
            reason=reason,
            evicted=report.evicted,
            evicted_bytes=report.evicted_bytes,
            usage=report.usage,
            over_quota=report.over_quota,
            skipped=report.skipped,
            duration_s=round(report.duration_s, 3),
        )
        return report

    def _collect(self, kind: str, quota: int, report: GcReport) -> None:
        result = collect_kind(
            self.index,
            kind,
            quota,
            cutoff=lambda: self._wall() - self.config.min_age_s,
            batch=self.config.batch,
            evict=self._evict_blob if kind == "blob" else self._evict_env,
            after_batch=self._finish_env_removals,
        )
        if result.evicted:
            report.evicted[kind] = result.evicted
            report.evicted_bytes[kind] = result.evicted_bytes
        report.skipped += result.skipped
        report.usage[kind] = result.usage
        if result.over_quota:
            report.over_quota.append(kind)
            log.warning(
                "agent.gc.over_quota",
                kind=kind,
                usage=result.usage,
                quota=quota,
                detail="everything left is in use, pinned or younger than min_age_s",
            )

    # ------------------------------------------------------------- removals

    def _evict_blob(self, victim: Victim) -> bool:
        """Inside the index transaction; the CAS observer deletes the row."""
        digest = Digest.parse(f"sha256:{victim.key}")
        try:
            self.store.evict(digest)
        except OSError as exc:
            log.warning(
                "agent.gc.evict_failed",
                kind="blob",
                digest=str(digest),
                cause=type(exc).__name__,
            )
            return False
        self.index.remove("blob", victim.key)  # absent already: idempotent
        return True

    def _evict_env(self, victim: Victim) -> bool:
        kind, _, digest = victim.key.partition("/")
        if not _HEX_RE.fullmatch(digest):
            self.index.remove("env", victim.key)
            return False
        lock = (
            self.envs_root / "locks" / f"{digest}.lock"
            if self.envs_root is not None
            else None
        )
        with contextlib.ExitStack() as stack:
            if lock is not None:
                try:
                    lock.parent.mkdir(parents=True, exist_ok=True)
                    stack.enter_context(locked(lock, timeout_s=0.0))
                except (TimeoutError, OSError):
                    return False  # a build or a check of it is under way
            if kind in DIR_ENV_KINDS:
                return self._evict_env_dir(kind, digest, victim)
            if kind == "image":
                return self._evict_env_image(digest, victim)
        self.index.remove("env", victim.key)
        return False

    def _evict_env_dir(self, kind: str, digest: str, victim: Victim) -> bool:
        if self.envs_root is None:
            return False
        final = self.envs_root / kind / digest
        aside = final.with_name(f"{digest}{_GC_SUFFIX}{secrets.token_hex(4)}")
        try:
            os.replace(final, aside)
        except FileNotFoundError:
            self.index.remove("env", victim.key)
            return True
        except OSError as exc:  # Windows: a file of it is open by a process
            log.info(
                "agent.gc.evict_failed",
                kind="env",
                env_hash=digest,
                cause=type(exc).__name__,
            )
            return False
        self.index.remove("env", victim.key)
        self._pending_dirs.append(aside)
        return True

    def _evict_env_image(self, digest: str, victim: Victim) -> bool:
        if self._remove_image is None:
            return False
        try:
            removed = self._remove_image(digest)
        except Exception as exc:  # noqa: BLE001 - Docker gone: try next pass
            log.info(
                "agent.gc.image_rm_failed", env_hash=digest, cause=type(exc).__name__
            )
            return False
        if removed:
            self.index.remove("env", victim.key)
        return removed

    def _finish_env_removals(self) -> None:
        """Delete renamed-aside env dirs (outside the index transaction)."""
        pending = self._pending_dirs
        while pending:
            path = pending.pop()
            try:
                remove_tree(path)
            except OSError as exc:  # swept by the next pass
                log.info(
                    "agent.gc.leftover_kept", path=path.name, cause=type(exc).__name__
                )
