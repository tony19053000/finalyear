"""Known-bad environments per node (V0.6 G1, D116; roadmap 0.6.10).

An attempt that failed ``env_error`` (the image or requirements could not be
built on that node) will fail the same way on the same node for a while: the
dispatcher records ``(node_id, env key)`` here for ``[controller.jobs]
env_bad_ttl_s`` and the planner's feasibility filter (V0.6 G2) asks
:meth:`KnownBadEnvs.is_bad` - a plain synchronous query on this in-memory map.

**The env key** is what the controller can compute for any job before it runs
(:func:`env_key`): the manifest's ``env`` block (base, python, requirements
path) plus the project it reads the requirements from (``tree_id``, or the
bundle digest of a V0.4 job). The agent's own ``env_hash`` cannot be predicted
by the controller (it covers the node's interpreter, platform, base-image digest
and package sources), so it is stored alongside for operators and telemetry,
not used as the key. The key is deliberately narrow - a new commit of the
project is a new key - so a stale entry can never wrongly reject a job that
might now build; it exactly covers the retry of the same job (V0.6 G3).

Not persisted (no migration in G1): a controller restart forgets every entry,
which at worst costs one more failing attempt per (node, env). Bounded by
``known_bad_envs_max`` (oldest dropped). Monotonic clock only.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from meshforge.common.logging import get_logger

__all__ = ["KnownBadEnv", "KnownBadEnvs", "env_key"]

log = get_logger(__name__)


def env_key(
    manifest: Mapping[str, Any], *, tree_id: str | None, bundle_digest: str | None
) -> str:
    """64-hex key of a job's environment request (module docstring)."""
    env = manifest.get("env")
    block = env if isinstance(env, Mapping) else {}
    canonical = {
        "base": str(block.get("base") or "python-cpu"),
        "python": str(block.get("python") or ""),
        "requirements": str(block.get("requirements") or ""),
        "project": str(tree_id or bundle_digest or ""),
    }
    data = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("ascii", "backslashreplace")).hexdigest()


@dataclass(frozen=True, slots=True)
class KnownBadEnv:
    node_id: str
    key: str
    env_hash: str | None
    """The agent's env hash when it reported one."""
    until: float
    """Controller monotonic time when the entry expires."""
    job_id: str
    attempt_id: str


class KnownBadEnvs:
    """See module docstring. One writer (the dispatcher task); readers are
    synchronous calls on the same event loop."""

    def __init__(
        self, *, ttl_s: float, clock: Callable[[], float], max_entries: int = 10_000
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._ttl_s = ttl_s
        self._clock = clock
        self._max = max_entries
        self._entries: OrderedDict[tuple[str, str], KnownBadEnv] = OrderedDict()
        self.recorded = 0

    def record(
        self,
        node_id: str,
        key: str,
        *,
        env_hash: str | None,
        job_id: str,
        attempt_id: str,
    ) -> KnownBadEnv | None:
        """Remember ``(node_id, key)`` as known-bad for ``ttl_s`` (None = off)."""
        if self._ttl_s <= 0:
            return None
        entry = KnownBadEnv(
            node_id=node_id,
            key=key,
            env_hash=env_hash,
            until=self._clock() + self._ttl_s,
            job_id=job_id,
            attempt_id=attempt_id,
        )
        self._entries.pop((node_id, key), None)
        self._entries[(node_id, key)] = entry
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
        self.recorded += 1
        log.warning(
            "controller.jobs.env_known_bad",
            node_id=node_id,
            job_id=job_id,
            attempt_id=attempt_id,
            env_key=key,
            env_hash=env_hash,
            ttl_s=self._ttl_s,
        )
        return entry

    def is_bad(self, node_id: str, key: str) -> bool:
        """Is ``key`` known not to build on ``node_id`` right now?"""
        entry = self._entries.get((node_id, key))
        if entry is None:
            return False
        if self._clock() >= entry.until:
            del self._entries[(node_id, key)]
            return False
        return True

    def for_node(self, node_id: str) -> list[KnownBadEnv]:
        """Every live entry of ``node_id`` (expired ones are dropped)."""
        now = self._clock()
        live: list[KnownBadEnv] = []
        for ident, entry in list(self._entries.items()):
            if now >= entry.until:
                del self._entries[ident]
            elif entry.node_id == node_id:
                live.append(entry)
        return live

    def __len__(self) -> int:
        return len(self._entries)
