"""The V0.6 lab verdict (roadmap 0.6.13, criteria 1-2) - pure.

Inputs: the jobs the run submitted (with what each was meant to do), the
controller's view of each job (``GET /v1/jobs/{id}``: state, failure type,
attempts) and every laptop's ledger (``lab/workloads/v06-ledger``: one
``start``/``end`` line per execution).

Counted, per D117 (at-least-once execution, exactly-once commit) and D132
(the check is strict):

* **lost** - a job that is not terminal; or failed with a retryable failure
  type while it had attempts left; or **meant to succeed (exit 0) and not
  ``succeeded``** - whatever the reason (retries exhausted, cancelled, a
  failure type): the run did not deliver it;
* **duplicate commits** - a succeeded job with other than exactly one ``done``
  attempt (must be 0, always);
* **duplicate executions under kills** - (a) the **same attempt id** with more
  than one ``end`` line: one attempt executed twice, the very thing command
  dedupe exists to prevent (never excused, not even by a partition); (b) a job
  with an extra ``end`` line that belongs to an attempt that was ``lost`` on a
  node that was **not** partitioned (its run completed, then the agent died
  before the result reached the controller) or to no known attempt. Must be 0
  (criterion 1); each one is listed so the team can see the race;
* **overlaps under partition** - extra ``end`` lines of attempts lost on a
  partitioned node: allowed and reported separately (D117);
* **retried after a failure** - an extra ``end`` of an attempt that ended
  ``failed`` (e.g. its outputs could not be uploaded): a retry, not a
  duplicate;
* **committed without evidence** - a succeeded job whose committed attempt has
  no ``end`` in any ledger (a ledger file is missing - collect them all).
  Duplicates on that laptop would be invisible, so criterion 1 is then
  ``INCONCLUSIVE``: never PASS.

Ledger lines that are **record-identical** (same time to the microsecond,
kind, job, attempt and host) are one event read twice - the same ledger file
copied under two names - and are counted once (``identical_ledger_lines``).
Two executions of one attempt always differ in their time stamps, so a real
duplicate is still seen.

Criterion 1 is ``FAIL`` when anything was lost or duplicated, else
``INCONCLUSIVE`` when evidence is missing **or the run has no jobs** (nothing
was checked: never a vacuous PASS), else ``PASS``
(:attr:`Verdict.criterion_1_text`).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "FAIL",
    "INCONCLUSIVE",
    "PASS",
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
PASS: Final = "PASS"  # noqa: S105 - a verdict word, not a password
FAIL: Final = "FAIL"
INCONCLUSIVE: Final = "INCONCLUSIVE"


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
    identical_ledger_lines: int = 0

    @property
    def failed(self) -> bool:
        """Something was lost or duplicated (commits, executions under kills)."""
        return bool(self.lost or self.duplicate_commits or self.duplicate_executions)

    @property
    def criterion_1_text(self) -> str:
        """``FAIL`` / ``INCONCLUSIVE`` / ``PASS`` (module docstring, D132)."""
        if self.failed:
            return FAIL
        if self.jobs == 0 or self.committed_without_evidence:
            return INCONCLUSIVE
        return PASS

    @property
    def criterion_1(self) -> bool:
        """At least one job, 0 lost, 0 duplicated **and** ledger evidence for
        every commit."""
        return self.criterion_1_text == PASS

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
            "identical_ledger_lines": self.identical_ledger_lines,
            "criterion_1": self.criterion_1_text,
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
    runs: Counter[tuple[str, str]] = Counter()
    unique = dict.fromkeys(ledger)  # one event read twice is one event
    for line in unique:
        if line.kind == "end":
            ends[line.job_id].append(line.attempt_id)
            runs[(line.job_id, line.attempt_id)] += 1
    verdict = Verdict(
        jobs=len(planned), identical_ledger_lines=len(ledger) - len(unique)
    )
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
            elif plan.exit_code == 0:
                verdict.lost.append(
                    f"{plan.job_id}: meant to succeed, failed {ftype}"
                    + (
                        f" after {len(attempts)} of {plan.max_attempts} attempts"
                        if ftype in RETRYABLE
                        else ""
                    )
                )
        elif plan.exit_code == 0:  # cancelled: the run did not deliver it
            verdict.lost.append(f"{plan.job_id}: meant to succeed, {state}")
        committed = {str(a.get("attempt_id")) for a in done}
        ran = sorted(set(ends.get(plan.job_id, [])))
        for attempt_id in ran:  # D132: one attempt id, more than one execution
            times = runs[(plan.job_id, attempt_id)]
            if times > 1:
                verdict.duplicate_executions.append(
                    f"{plan.job_id}/{attempt_id} (the same attempt ran {times} times)"
                )
        extra = [a for a in ran if a not in committed]
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
