"""The owner's env-build policy fingerprint (V1.0 G9-B, D433).

The controller remembers ``(node, env)`` pairs whose build failed
``env_error`` (D116) for ``env_bad_ttl_s``. When the laptop's owner then
changes ``[agent.envs]`` so the build can succeed - ``only_binary = false``
for an sdist-only package, another index, a wheelhouse, a larger
``max_env_bytes`` - the entry must not keep refusing the job. The agent
therefore advertises ``env-policy:<12 hex>`` (:mod:`meshforge.common.
capabilities`), a hash of exactly the settings that change what a build can
do, and the controller drops an entry recorded under another fingerprint.

Not in it: settings that do not decide success (``dir``, ``evicted_keep_s``,
``free_disk_floor_mb`` - a full disk is ``disk_full``, not ``env_error``).
``find_links`` counts by path only: new wheels in the same wheelhouse do not
change the fingerprint (the entry then lives its TTL - a documented limit).
[agent.envs] is read at agent start, so a change reaches the controller with
the next Hello after the owner restarts the agent.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final

from meshforge.agent.settings import EnvsSettings
from meshforge.common.capabilities import env_policy_tag

__all__ = ["FINGERPRINT_VERSION", "env_policy_capability", "env_policy_fingerprint"]

FINGERPRINT_VERSION: Final = 1
"""Bump when the hashed fields change (every node then looks "changed" once)."""


def env_policy_fingerprint(cfg: EnvsSettings) -> str:
    """12 lowercase hex digits over the build-deciding ``[agent.envs]`` fields."""
    document = {
        "v": FINGERPRINT_VERSION,
        "only_binary": cfg.only_binary,
        "index_url": cfg.index_url,
        "extra_index_urls": list(cfg.extra_index_urls),
        "find_links": None if cfg.find_links is None else str(cfg.find_links),
        "no_index": cfg.no_index,
        "allow_python_downloads": cfg.allow_python_downloads,
        "max_env_bytes": cfg.max_env_bytes,
        "max_requirements_bytes": cfg.max_requirements_bytes,
        "max_requirements_lines": cfg.max_requirements_lines,
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def env_policy_capability(cfg: EnvsSettings) -> str:
    """The ``env-policy:<12 hex>`` tag the agent puts into every Hello."""
    return env_policy_tag(env_policy_fingerprint(cfg))
