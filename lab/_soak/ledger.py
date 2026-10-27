"""The submitted-jobs ledger (``<run dir>/ledger.jsonl``) - D410 rule 1's
"submit log".

Written by ``soak_load.py run`` only, one fsynced line per fact:

* ``intent`` - *before* ``POST /v1/jobs``: seq, kind, idempotency key;
* ``submitted`` - the controller's answer: ``job_id`` (201 new, or 200 = a
  replay of the same key, which is how a resumed generator learns the id of a
  job whose answer it never saw - the key is UNIQUE in the controller DB);
* ``submit_failed`` - a refused submission (``final`` = a 4xx that a retry
  cannot fix; otherwise it is retried with the same key);
* ``cancel`` - a planned cancel and its HTTP status.

:func:`reduce` folds the lines into one :class:`LedgerState`: which seq is
settled, which job id it got, which planned cancels are done - the resume
point and the analyzer's input. A ``submitted`` whose job id differs from an
earlier one for the same seq is a duplicate submission and is kept as a
problem (it must never happen with idempotency keys).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

LEDGER_SCHEMA: Final = "meshforge.soak-ledger/1"


@dataclass(slots=True)
class Entry:
    seq: int
    kind: str = ""
    key: str = ""
    intent_at: str | None = None
    job_id: str | None = None
    submitted_at: str | None = None
    failures: int = 0
    final_failure: str | None = None
    cancel_status: int | None = None
    cancel_at: str | None = None

    @property
    def settled(self) -> bool:
        return self.job_id is not None or self.final_failure is not None


@dataclass(slots=True)
class LedgerState:
    entries: dict[int, Entry] = field(default_factory=dict)
    header: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)

    def entry(self, seq: int) -> Entry:
        return self.entries.setdefault(seq, Entry(seq))

    def job_ids(self) -> dict[str, int]:
        return {e.job_id: s for s, e in self.entries.items() if e.job_id}

    def unresolved(self) -> list[int]:
        """Intents without a final answer (a crash between POST and answer
        that was never resumed)."""
        return sorted(s for s, e in self.entries.items() if not e.settled)


def reduce(records: Iterable[Mapping[str, Any]]) -> LedgerState:
    state = LedgerState()
    for r in records:
        kind = r.get("type")
        if kind == "header":
            state.header = dict(r)
            continue
        if not isinstance(r.get("seq"), int):
            continue
        e = state.entry(int(r["seq"]))
        if kind == "intent":
            e.kind = str(r.get("kind") or e.kind)
            e.key = str(r.get("key") or e.key)
            e.intent_at = e.intent_at or r.get("at")
        elif kind == "submitted":
            job_id = str(r.get("job_id"))
            if e.job_id is not None and e.job_id != job_id:
                state.problems.append(
                    f"seq {e.seq}: two job ids {e.job_id} and {job_id} (duplicate submission)"
                )
            e.job_id = e.job_id or job_id
            e.submitted_at = e.submitted_at or r.get("at")
        elif kind == "submit_failed":
            e.failures += 1
            if r.get("final"):
                e.final_failure = str(r.get("error") or r.get("status"))
        elif kind == "cancel":
            status = r.get("status")
            e.cancel_status = int(status) if isinstance(status, int) else None
            e.cancel_at = r.get("at")
    return state
