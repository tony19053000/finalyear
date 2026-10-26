"""The seeded mixed job load of the soak (D409) - pure.

:func:`job_specs` turns ``(plan)`` into the complete, ordered list of jobs the
run will submit: Poisson arrivals (exponential gaps, mean ``job_gap_s``) over
``[0, hours)``, each with a kind drawn from :data:`MIX`:

* ``m1_short`` / ``m1_long`` - Mode 1 offload of the V0.6 ledger workload
  (``lab/workloads/v06-ledger``: busy CPU for ``work_s``, sleep ``sleep_s``,
  write ``out/result.txt`` naming the job and attempt, exit ``exit_code``);
  ~3 % are meant to fail (exit 3) so the failure path is exercised too;
* ``m3_split`` - a Mode 3 split job (``split.hash_files``, pure Python, so no
  package index is needed on any laptop);
* ``bench`` - a benchmark-family job (``cpu.primes``, the V0.9 workload
  library), a normal auto-placed job.

5 % of all jobs get a planned cancel ``cancel_after_s`` after submission.
Policies rotate over the shipped planner policies so every placement path
runs. The list is a pure function of the plan, so a resumed load generator
rebuilds it and continues at the first job the ledger has not settled.
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass
from typing import Any, Final

from _soak.plan import RunPlan

MIX: Final = (
    ("m1_short", 0.50),
    ("m1_long", 0.15),
    ("m3_split", 0.20),
    ("bench", 0.15),
)
POLICIES: Final = (
    "cost_model",
    "cost_model",
    "least_loaded",
    "random",
    "cache_affinity",
)
CANCEL_PCT: Final = 0.05
FAIL_PCT: Final = 0.03
MAX_ATTEMPTS: Final = 5
BACKOFF_S: Final = 5
SPLIT_FAMILY: Final = "split.hash_files"
BENCH_FAMILY: Final = "cpu.primes"
SEED_POOL: Final = 8
"""Split / bench jobs draw their seed (their input data) from a small pool, so
inputs repeat like a real user's would: caches and cache-affinity placement
are exercised, and the load generator uploads each folder once."""


@dataclass(frozen=True, slots=True)
class JobSpec:
    seq: int
    offset_s: float
    kind: str
    policy: str
    priority: int
    exit_code: int
    sleep_s: float
    work_s: float
    split_params: dict[str, int] | None
    seed: int
    cancel_after_s: float | None
    timeout_s: int

    @property
    def intent(self) -> str:
        """``succeed`` / ``fail`` (exit 3) - a planned cancel may override both."""
        return "fail" if self.exit_code != 0 else "succeed"

    @property
    def deadline_s(self) -> float:
        """Latest reasonable end after submission (D410 rule 1: a job must not
        be stuck > 10 min past this): every attempt timing out, plus backoffs."""
        return float(self.timeout_s * MAX_ATTEMPTS + BACKOFF_S * MAX_ATTEMPTS)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _kind(rng: random.Random) -> str:
    roll = rng.random()
    acc = 0.0
    for kind, weight in MIX:
        acc += weight
        if roll < acc:
            return kind
    return MIX[-1][0]


def job_specs(plan: RunPlan) -> list[JobSpec]:
    rng = random.Random(f"soak-load-{plan.seed}")  # noqa: S311 - a reproducible mix
    scale = plan.duration_scale
    out: list[JobSpec] = []
    t = 0.0
    limit = plan.hours * 3600.0
    while True:
        t += -math.log(1.0 - rng.random()) * plan.job_gap_s
        if t >= limit:
            break
        kind = _kind(rng)
        exit_code = 3 if kind.startswith("m1") and rng.random() < FAIL_PCT else 0
        if kind == "m1_short":
            sleep_s, work_s, timeout_s = (
                rng.uniform(5, 60),
                rng.choice((0.0, 2.0, 5.0)),
                600,
            )
        elif kind == "m1_long":
            sleep_s = rng.uniform(300, 1200) * scale
            work_s = rng.choice((0.0, 5.0, 15.0))
            timeout_s = int(max(600.0, 1800 * scale))
        else:
            sleep_s, work_s, timeout_s = 0.0, 0.0, 1800
        split_params = (
            {
                "files": rng.choice((16, 32, 64)),
                "size_kb": 64,
                "rounds": rng.choice((8, 16, 32)),
            }
            if kind == "m3_split"
            else None
        )
        cancel = round(rng.uniform(2.0, 30.0), 1) if rng.random() < CANCEL_PCT else None
        out.append(
            JobSpec(
                seq=len(out),
                offset_s=round(t, 3),
                kind=kind,
                policy=rng.choice(POLICIES),
                priority=rng.randint(-2, 2),
                exit_code=exit_code,
                sleep_s=round(sleep_s, 1),
                work_s=work_s,
                split_params=split_params,
                seed=rng.randint(1, SEED_POOL),
                cancel_after_s=cancel,
                timeout_s=timeout_s,
            )
        )
    return out


def ledger_manifest(spec: JobSpec, nonce: str) -> dict[str, Any]:
    """The compute.yaml-equivalent manifest of an ``m1_*`` job (V0.6 workload)."""
    return {
        "name": f"soak-{nonce}-{spec.seq:05d}-{spec.kind}",
        "entrypoint": [
            "python",
            "main.py",
            str(spec.sleep_s),
            str(spec.work_s),
            str(spec.exit_code),
        ],
        "outputs": ["out/*"],
        "priority": spec.priority,
        "retry": {"max_attempts": MAX_ATTEMPTS, "backoff_s": BACKOFF_S},
        "limits": {"timeout": spec.timeout_s},
    }


def idempotency_key(plan: RunPlan, spec: JobSpec) -> str:
    return f"soak-{plan.seed}-{plan.nonce}-{spec.seq}"


def mix_summary(specs: list[JobSpec]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for s in specs:
        by_kind[s.kind] = by_kind.get(s.kind, 0) + 1
    return {
        "jobs": len(specs),
        "by_kind": dict(sorted(by_kind.items())),
        "planned_cancels": sum(1 for s in specs if s.cancel_after_s is not None),
        "meant_to_fail": sum(1 for s in specs if s.exit_code != 0),
    }
