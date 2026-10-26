"""Block randomisation (D417): the order the matrix runs in.

One **block** = one repetition of every (family, size, arm) cell. Inside a
block the arms are grouped by controller profile (:mod:`.arms` - switching
profile restarts the controller, so it happens once per profile per block),
the profiles are shuffled, and inside each profile the (family, size, arm)
cells are shuffled. Drift (a laptop warming up, Wi-Fi getting busier in the
evening) then falls on every arm alike instead of on whichever ran last.

Each run gets its own workload seed (the job's input data), derived from the
campaign seed and the run id: no run reuses another's inputs, so no arm
profits from a cache another arm warmed (every run is a "new job", as a user
would submit). Everything here is a pure function of the spec - the same file
gives the same schedule, byte for byte (its digest is stored with the raw
data and checked on resume).
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass

from evalkit.arms import ARMS
from evalkit.canonical import sha256_bytes
from evalkit.spec import CampaignSpec

__all__ = ["PlannedRun", "derive_seed", "schedule", "schedule_digest"]

_SEED_MASK = 0x7FFF_FFFF


def derive_seed(*parts: object) -> int:
    """A non-negative 31-bit integer from ``parts`` (sha256; stable across
    Python versions and platforms)."""
    text = "\x1f".join(str(p) for p in parts)
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:4], "big") & (
        _SEED_MASK
    )


@dataclass(frozen=True, slots=True)
class PlannedRun:
    run_id: str
    """``b<block>-<family>-<size>-<arm>``: unique in the campaign."""
    order: int
    """Position in the whole campaign (0-based)."""
    block: int
    """The repetition this run belongs to (0-based)."""
    family: str
    size: str
    arm: str
    profile: str
    workload_seed: int


def _rng(spec: CampaignSpec, block: int, what: str) -> random.Random:
    return random.Random(derive_seed(spec.seed, spec.name, block, what))  # noqa: S311


def schedule(spec: CampaignSpec) -> list[PlannedRun]:
    """Every run of the campaign, in execution order (module docstring)."""
    profiles: list[str] = []
    for arm_id in spec.arms:
        profile = ARMS[arm_id].profile
        if profile not in profiles:
            profiles.append(profile)
    out: list[PlannedRun] = []
    for block in range(spec.repetitions):
        order = list(profiles)
        _rng(spec, block, "profiles").shuffle(order)
        for profile in order:
            cells = [
                (family, size, arm_id)
                for family, size in spec.cells()
                for arm_id in spec.arms
                if ARMS[arm_id].profile == profile
            ]
            _rng(spec, block, f"cells:{profile}").shuffle(cells)
            for family, size, arm_id in cells:
                run_id = f"b{block:03d}-{family}-{size}-{arm_id}"
                out.append(
                    PlannedRun(
                        run_id=run_id,
                        order=len(out),
                        block=block,
                        family=family,
                        size=size,
                        arm=arm_id,
                        profile=profile,
                        workload_seed=derive_seed(spec.seed, spec.name, run_id),
                    )
                )
    return out


def schedule_digest(runs: list[PlannedRun]) -> str:
    text = json.dumps([asdict(r) for r in runs], sort_keys=True, separators=(",", ":"))
    return "sha256:" + sha256_bytes(text.encode("utf-8"))
