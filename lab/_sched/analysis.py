"""The V0.6 lab verdict (roadmap 0.6.13, criteria 1-2) - pure.

Inputs: the jobs the run submitted (with what each was meant to do), the
controller's view of each job (``GET /v1/jobs/{id}``: state, failure type,
attempts) and every laptop's ledger (``lab/workloads/v06-ledger``: one
``start``/``end`` line per execution).

Counted, per D117 (at-least-once execution, exactly-once commit):

* **lost** - a job that is not terminal, or failed with a retryable failure
  type while it had attempts left, or failed although it was meant to succeed
  and nothing about its attempts explains it;
* **duplicate commits** - a succeeded job with other than exactly one ``done``
  attempt (must be 0, always);
* **duplicate executions under kills** - a job with more than one ``end`` line
  where the extra one belongs to an attempt that was ``lost`` on a node that
  was **not** partitioned (its run completed, then the agent died before the
  result reached the controller) or to no known attempt. Must be 0
  (criterion 1); each one is listed so the team can see the race;
* **overlaps under partition** - extra ``end`` lines of attempts lost on a
  partitioned node: allowed and reported separately (D117);
* **retried after a failure** - an extra ``end`` of an attempt that ended
  ``failed`` (e.g. its outputs could not be uploaded): a retry, not a
  duplicate;
* **committed without evidence** - a succeeded job whose committed attempt has
  no ``end`` in any ledger (a ledger file is missing - collect them all).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "RETRYABLE",
    "LedgerLine",
    "Planned",
    "Verdict",
    "analyse",
    "parse_ledger",
]

RETRYABLE: Final = frozenset(
    {
        "vram_oom",
        "node_lost",
        "gpu_session_lost",
        "transfer_error",
        "integrity_error",
        "timeout",
        "resource_busy",
        "network_error",
        "disk_full",
    }
)
"""ARCHITECTURE §13 defaults for an auto job (disk_full: D126)."""
TERMINAL: Final = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class LedgerLine:
    at: float
    kind: str
    job_id: str
    attempt_id: str
    host: str


@dataclass(frozen=True, slots=True)
class Planned:
    job_id: str
    exit_code: int
    max_attempts: int
    pinned: bool = False


@dataclass(slots=True)
class Verdict:
    jobs: int = 0
    states: Counter[str] = field(default_factory=Counter)
    attempts_histogram: Counter[int] = field(default_factory=Counter)
    lost: list[str] = field(default_factory=list)
    duplicate_commits: list[str] = field(default_factory=list)
    duplicate_executions: list[str] = field(default_factory=list)
    partition_overlaps: list[str] = field(default_factory=list)
    retried_after_failure: list[str] = field(default_factory=list)
    committed_without_evidence: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)

    @property
    def criterion_1(self) -> bool:
        """0 lost, 0 duplicated (commits and executions under kills)."""
        return not (self.lost or self.duplicate_commits or self.duplicate_executions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "jobs": self.jobs,
            "states": dict(sorted(self.states.items())),
            "attempts_per_job": dict(sorted(self.attempts_histogram.items())),
            "lost": self.lost,
            "duplicate_commits": self.duplicate_commits,
            "duplicate_executions_under_kills": self.duplicate_executions,
            "overlaps_under_partition": self.partition_overlaps,
            "retried_after_failure": self.retried_after_failure,
            "committed_without_ledger_evidence": self.committed_without_evidence,
            "unexpected": self.unexpected,
            "criterion_1": "PASS" if self.criterion_1 else "FAIL",
        }


def parse_ledger(lines: Iterable[str]) -> list[LedgerLine]:
    """Ledger lines; malformed ones (a torn write) are skipped."""
    out: list[LedgerLine] = []
    for raw in lines:
        parts = raw.split()
        if len(parts) != 5:
            continue
        try:
            at = float(parts[0])
        except ValueError:
            continue
        out.append(LedgerLine(at, parts[1], parts[2], parts[3], parts[4]))
    return out


def analyse(
    planned: Sequence[Planned],
    views: Mapping[str, Mapping[str, Any] | None],
    ledger: Sequence[LedgerLine],
    *,
    partitioned_nodes: Iterable[str] = (),
) -> Verdict:
    """See module docstring."""
    partitioned = set(partitioned_nodes)
    ends: dict[str, list[str]] = defaultdict(list)
    for line in ledger:
        if line.kind == "end":
            ends[line.job_id].append(line.attempt_id)
    verdict = Verdict(jobs=len(planned))
    for plan in planned:
        view = views.get(plan.job_id)
        if view is None:
            verdict.lost.append(f"{plan.job_id}: no answer from the controller")
            continue
        state = str(view.get("state"))
        ftype = view.get("failure_type")
        attempts = [a for a in view.get("attempts") or [] if isinstance(a, Mapping)]
        verdict.states[state if state != "failed" else f"failed:{ftype}"] += 1
        verdict.attempts_histogram[len(attempts)] += 1
        by_id = {str(a.get("attempt_id")): a for a in attempts}
        done = [a for a in attempts if a.get("state") == "done"]
        if state not in TERMINAL:
            verdict.lost.append(f"{plan.job_id}: still {state}")
            continue
        if state == "succeeded":
            if len(done) != 1:
                verdict.duplicate_commits.append(
                    f"{plan.job_id}: {len(done)} done attempts"
                )
            elif str(done[0].get("attempt_id")) not in ends.get(plan.job_id, []):
                verdict.committed_without_evidence.append(plan.job_id)
            if plan.exit_code != 0:
                verdict.unexpected.append(f"{plan.job_id}: meant to fail, succeeded")
        elif state == "failed":
            if (
                ftype in RETRYABLE
                and len(attempts) < plan.max_attempts
                and not (plan.pinned and ftype == "disk_full")
            ):
                verdict.lost.append(
                    f"{plan.job_id}: failed {ftype} after {len(attempts)} of "
                    f"{plan.max_attempts} attempts"
                )
            elif plan.exit_code == 0 and ftype not in RETRYABLE:
                verdict.lost.append(f"{plan.job_id}: meant to succeed, failed {ftype}")
        committed = {str(a.get("attempt_id")) for a in done}
        extra = [a for a in ends.get(plan.job_id, []) if a not in committed]
        if state != "succeeded" and plan.exit_code != 0:
            # the runs that exit non-zero on purpose end with an `end` line too
            failed_ids = {
                str(a.get("attempt_id"))
                for a in attempts
                if a.get("state") == "failed" and a.get("failure_type") == "user_error"
            }
            extra = [a for a in extra if a not in failed_ids]
        for attempt_id in extra:
            attempt = by_id.get(attempt_id)
            if (
                attempt is not None
                and attempt.get("state") == "lost"
                and str(attempt.get("node_id")) in partitioned
            ):
                verdict.partition_overlaps.append(f"{plan.job_id}/{attempt_id}")
            elif attempt is not None and attempt.get("state") == "failed":
                verdict.retried_after_failure.append(
                    f"{plan.job_id}/{attempt_id} ({attempt.get('failure_type')})"
                )
            else:
                where = "unknown attempt" if attempt is None else attempt.get("state")
                verdict.duplicate_executions.append(
                    f"{plan.job_id}/{attempt_id} ({where})"
                )
    return verdict
