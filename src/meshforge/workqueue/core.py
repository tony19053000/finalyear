"""Pure core of the leased work queue: states, leases, fencing, completion.

No database, no clock, no I/O: every function takes the item as it is now and
returns the item as it must become plus a typed outcome. :mod:`.store` locks
the rows, calls these functions and writes the difference back, so the rules
live in exactly one place and the in-memory model used by the randomised tests
is the same code.

Vocabulary
----------
* **Item** - one unit of work at position ``idx`` of its queue. States:
  ``pending`` (nobody holds it) -> ``leased`` (at least one live lease) ->
  ``done`` (a result is committed; final) or ``failed`` (attempts exhausted or
  a non-retryable failure; final).
* **Lease** - permission for one *holder* (an opaque worker identity, e.g. a
  worker attempt) at one *site* (an opaque placement, e.g. a machine) to work on an
  item until ``deadline``. Each lease of an item gets the next **lease
  sequence number** ``seq`` (1, 2, ...); ``(item, seq)`` names the lease and
  fences every message about it. Lease states: ``live`` -> ``completed`` (its
  result won), ``failed`` (its holder reported a failure), ``expired`` (the
  visibility timeout passed), ``released`` (its holder went away or the queue
  closed - not the item's fault) or ``superseded`` (another lease's result
  won while it was still running).
* At most :data:`MAX_LIVE_LEASES` (2) leases of an item are live at once: one
  ordinary lease and at most one **speculative** copy, which is only ever
  issued while exactly one ordinary lease is live, at a different site.

Completion (ADR-0015: at-least-once execution, exactly-once commit)
-------------------------------------------------------------------
:func:`complete` is idempotent. The first result committed for an item wins;
any later result for that item - the speculative loser, a replay of the
winner's own report, a late result from an expired lease - gets
:class:`AlreadyDone` with the winner's sequence number and digest and changes
nothing. A result from a lease that is no longer live (expired, released) is
still **accepted** while the item is not done: the work was done, and
accepting it is what at-least-once execution means. A result from a lease
whose holder reported it failed, from the wrong holder, for an unknown lease
or after the queue closed is :class:`Fenced` and changes nothing.

Attempts
--------
``attempts`` counts executions that ended without a result *because of the
item*: :func:`fail` always, expiry only when the policy says so
(``count_expiry``; off by default, because an expired lease usually means a
partitioned worker, not a bad item). :func:`release` never counts. The item
fails when ``attempts`` reaches ``max_attempts``, when the failure is not
retryable, or when it has failed at ``max_failed_sites`` distinct sites.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

__all__ = [
    "EXPIRED_KIND",
    "MAX_DIGEST_CHARS",
    "MAX_FAILURE_KIND_CHARS",
    "MAX_LIVE_LEASES",
    "MAX_NAME_CHARS",
    "AlreadyDone",
    "Committed",
    "CompleteOutcome",
    "Expired",
    "FailOutcome",
    "FenceReason",
    "Fenced",
    "Item",
    "ItemFailed",
    "ItemState",
    "Lease",
    "LeaseState",
    "QueuePolicy",
    "QueueState",
    "Released",
    "RenewOutcome",
    "Renewed",
    "Retried",
    "SpeculationPick",
    "SpeculationRefusal",
    "complete",
    "expire",
    "fail",
    "lease",
    "next_pending",
    "pick_speculation",
    "predicted_remaining_s",
    "release",
    "release_all",
    "renew",
    "speculate",
    "speculation_budget",
    "speculation_candidates",
    "speculation_refusal",
]

MAX_LIVE_LEASES: Final = 2
"""Original lease + one speculative copy (D262)."""
EXPIRED_KIND: Final = "lease_expired"
"""``failed_kind`` of an item whose last attempt was an expiry (``count_expiry``)."""
MAX_DIGEST_CHARS: Final = 128
"""Result references are opaque strings (``sha256:<hex>`` = 71 chars today)."""
MAX_FAILURE_KIND_CHARS: Final = 32
MAX_NAME_CHARS: Final = 64
"""Upper bound of holder / site strings (prefixed ULIDs are 40 at most)."""


class ItemState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"


class LeaseState(StrEnum):
    LIVE = "live"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    RELEASED = "released"
    SUPERSEDED = "superseded"


class QueueState(StrEnum):
    OPEN = "open"
    DONE = "done"
    """Every item is done; final."""
    FAILED = "failed"
    """An item failed; final - live leases were released."""
    CANCELLED = "cancelled"
    """Closed by its owner; final - live leases were released."""


class FenceReason(StrEnum):
    """Why a message about a lease was refused (it changed nothing)."""

    UNKNOWN_LEASE = "unknown_lease"
    WRONG_HOLDER = "wrong_holder"
    LEASE_NOT_LIVE = "lease_not_live"
    """Renew or fail of an expired / released / superseded lease."""
    LEASE_FAILED = "lease_failed"
    """A result or a second failure for a lease its holder already failed."""
    ITEM_FAILED = "item_failed"
    QUEUE_CLOSED = "queue_closed"


class SpeculationRefusal(StrEnum):
    QUEUE_CLOSED = "queue_closed"
    PENDING_LEFT = "pending_left"
    """Items are still pending: speculate only at the tail (D266)."""
    BUDGET_SPENT = "budget_spent"
    UNKNOWN_ITEM = "unknown_item"
    NOT_LEASED = "not_leased"
    NOT_ONE_LIVE_LEASE = "not_one_live_lease"
    ALREADY_SPECULATIVE = "already_speculative"
    SAME_SITE = "same_site"
    SAME_HOLDER = "same_holder"


@dataclass(frozen=True, slots=True)
class QueuePolicy:
    """Per-queue rules, stored on the queue row (restart-safe)."""

    visibility_timeout_s: float = 60.0
    max_attempts: int = 3
    max_failed_sites: int | None = None
    """Fail the item once it failed at this many distinct sites (``None`` = off);
    e.g. 2 = "the same item failed on two different machines" (D267c)."""
    count_expiry: bool = False
    speculative_max_frac: float = 0.10
    """At most ``ceil(frac * items)`` items ever get a speculative copy."""

    def __post_init__(self) -> None:
        if not (
            math.isfinite(self.visibility_timeout_s)
            and 0 < self.visibility_timeout_s <= 86_400
        ):
            raise ValueError("visibility_timeout_s must be in (0, 86400]")
        if not 1 <= self.max_attempts <= 100:
            raise ValueError("max_attempts must be in [1, 100]")
        if self.max_failed_sites is not None and not 1 <= self.max_failed_sites <= 100:
            raise ValueError("max_failed_sites must be None or in [1, 100]")
        if not (
            math.isfinite(self.speculative_max_frac)
            and 0 <= self.speculative_max_frac <= 1
        ):
            raise ValueError("speculative_max_frac must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class Lease:
    item_id: int
    seq: int
    holder: str
    site: str
    leased_at: datetime
    deadline: datetime
    state: LeaseState = LeaseState.LIVE
    speculative: bool = False

    @property
    def live(self) -> bool:
        return self.state is LeaseState.LIVE


@dataclass(frozen=True, slots=True)
class Item:
    """One item and the leases loaded with it.

    ``leases`` must contain every **live** lease of the item; leases in other
    states are optional history (the store loads only the one a message names).
    """

    id: int
    idx: int
    state: ItemState = ItemState.PENDING
    attempts: int = 0
    lease_seq: int = 0
    """Sequence number of the newest lease ever issued (0 = never leased)."""
    speculations: int = 0
    result_digest: str | None = None
    done_lease_seq: int | None = None
    failed_kind: str | None = None
    failed_sites: tuple[str, ...] = ()
    leases: tuple[Lease, ...] = ()

    def lease(self, seq: int) -> Lease | None:
        for candidate in self.leases:
            if candidate.seq == seq:
                return candidate
        return None

    @property
    def live_leases(self) -> tuple[Lease, ...]:
        return tuple(x for x in self.leases if x.live)

    @property
    def final(self) -> bool:
        return self.state in (ItemState.DONE, ItemState.FAILED)


# --- outcomes ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fenced:
    item_id: int
    seq: int
    reason: FenceReason


@dataclass(frozen=True, slots=True)
class Committed:
    item_id: int
    idx: int
    seq: int
    digest: str
    late: bool
    """The winning lease was no longer live (expired / released) - accepted
    because the item was still not done."""
    superseded: tuple[Lease, ...]
    """Other leases that were live and lost: tell their holders to stop."""


@dataclass(frozen=True, slots=True)
class AlreadyDone:
    item_id: int
    seq: int
    winner_seq: int
    winner_digest: str

    @property
    def own(self) -> bool:
        """This lease *is* the winner (a replayed report)."""
        return self.seq == self.winner_seq


@dataclass(frozen=True, slots=True)
class Retried:
    item_id: int
    seq: int
    requeued: bool
    """``True`` = back to ``pending``; ``False`` = another live lease continues."""


@dataclass(frozen=True, slots=True)
class ItemFailed:
    item_id: int
    idx: int
    seq: int
    kind: str
    released: tuple[Lease, ...]
    """Other live leases of the item, released (tell their holders to stop)."""


@dataclass(frozen=True, slots=True)
class Renewed:
    item_id: int
    seq: int
    deadline: datetime


@dataclass(frozen=True, slots=True)
class Expired:
    item_id: int
    idx: int
    leases: tuple[Lease, ...]
    item_failed: bool


@dataclass(frozen=True, slots=True)
class Released:
    item_id: int
    leases: tuple[Lease, ...]


CompleteOutcome = Committed | AlreadyDone | Fenced
FailOutcome = Retried | ItemFailed | AlreadyDone | Fenced
RenewOutcome = Renewed | Fenced


# --- helpers ----------------------------------------------------------------------


def _check_name(value: str, what: str) -> None:
    if not value or len(value) > MAX_NAME_CHARS:
        raise ValueError(f"{what} must be 1..{MAX_NAME_CHARS} characters")


def _timeout(policy: QueuePolicy, timeout_s: float | None) -> timedelta:
    seconds = policy.visibility_timeout_s if timeout_s is None else timeout_s
    if not (math.isfinite(seconds) and 0 < seconds <= 86_400):
        raise ValueError("timeout_s must be in (0, 86400]")
    return timedelta(seconds=seconds)


def _with_leases(item: Item, changed: Iterable[Lease]) -> tuple[Lease, ...]:
    """``item.leases`` with each lease in ``changed`` replaced (or appended)."""
    by_seq = {x.seq: x for x in item.leases}
    for x in changed:
        by_seq[x.seq] = x
    return tuple(by_seq[s] for s in sorted(by_seq))


def _set_state(leases: Iterable[Lease], state: LeaseState) -> tuple[Lease, ...]:
    return tuple(replace(x, state=state) for x in leases)


def _check_lease(item: Item, seq: int, holder: str) -> Lease | FenceReason:
    found = item.lease(seq)
    if found is None:
        return FenceReason.UNKNOWN_LEASE
    if found.holder != holder:
        return FenceReason.WRONG_HOLDER
    return found


def _already_done(item: Item, own: Lease) -> tuple[Item, AlreadyDone]:
    """A message about ``own`` for an item that is done: supersede it if live."""
    if item.done_lease_seq is None or item.result_digest is None:
        raise ValueError(f"item {item.id} is done without a result")
    changed = _set_state([own], LeaseState.SUPERSEDED) if own.live else ()
    new = replace(item, leases=_with_leases(item, changed)) if changed else item
    return new, AlreadyDone(item.id, own.seq, item.done_lease_seq, item.result_digest)


# --- transitions ------------------------------------------------------------------


def lease(
    item: Item,
    *,
    holder: str,
    site: str,
    now: datetime,
    policy: QueuePolicy,
    timeout_s: float | None = None,
) -> tuple[Item, Lease]:
    """Lease a ``pending`` item (raises ``ValueError`` otherwise - callers pick
    pending items under a row lock, so this is a programming error)."""
    _check_name(holder, "holder")
    _check_name(site, "site")
    if item.state is not ItemState.PENDING or item.live_leases:
        raise ValueError(f"item {item.id} is {item.state.value}, not pending")
    new_lease = Lease(
        item_id=item.id,
        seq=item.lease_seq + 1,
        holder=holder,
        site=site,
        leased_at=now,
        deadline=now + _timeout(policy, timeout_s),
    )
    new = replace(
        item,
        state=ItemState.LEASED,
        lease_seq=new_lease.seq,
        leases=_with_leases(item, [new_lease]),
    )
    return new, new_lease


def speculation_refusal(
    item: Item, *, holder: str, site: str
) -> SpeculationRefusal | None:
    """Why ``item`` may not get a speculative copy at ``site`` (``None`` = it may).

    Eligible: the item is leased with exactly one live lease, that lease is an
    ordinary one, and it is held by another holder at another site.
    """
    if item.state is not ItemState.LEASED:
        return SpeculationRefusal.NOT_LEASED
    live = item.live_leases
    if len(live) != 1:
        return SpeculationRefusal.NOT_ONE_LIVE_LEASE
    if live[0].speculative:
        return SpeculationRefusal.ALREADY_SPECULATIVE
    if live[0].site == site:
        return SpeculationRefusal.SAME_SITE
    if live[0].holder == holder:
        return SpeculationRefusal.SAME_HOLDER
    return None


def speculate(
    item: Item,
    *,
    holder: str,
    site: str,
    now: datetime,
    policy: QueuePolicy,
    timeout_s: float | None = None,
) -> tuple[Item, Lease | SpeculationRefusal]:
    """Issue the speculative copy of a leased item (see :func:`speculation_refusal`).

    Queue-level limits (no pending item left, the speculative budget) are the
    caller's: :func:`speculation_candidates` / :func:`speculation_budget`.
    """
    _check_name(holder, "holder")
    _check_name(site, "site")
    refusal = speculation_refusal(item, holder=holder, site=site)
    if refusal is not None:
        return item, refusal
    copy = Lease(
        item_id=item.id,
        seq=item.lease_seq + 1,
        holder=holder,
        site=site,
        leased_at=now,
        deadline=now + _timeout(policy, timeout_s),
        speculative=True,
    )
    new = replace(
        item,
        lease_seq=copy.seq,
        speculations=item.speculations + 1,
        leases=_with_leases(item, [copy]),
    )
    return new, copy


def renew(
    item: Item,
    *,
    seq: int,
    holder: str,
    now: datetime,
    policy: QueuePolicy,
    timeout_s: float | None = None,
) -> tuple[Item, RenewOutcome]:
    """Move a live lease's deadline to ``now + timeout`` (never earlier)."""
    found = _check_lease(item, seq, holder)
    if isinstance(found, FenceReason):
        return item, Fenced(item.id, seq, found)
    if not found.live:
        return item, Fenced(item.id, seq, FenceReason.LEASE_NOT_LIVE)
    deadline = max(found.deadline, now + _timeout(policy, timeout_s))
    if deadline == found.deadline:
        return item, Renewed(item.id, seq, deadline)
    moved = replace(found, deadline=deadline)
    return replace(item, leases=_with_leases(item, [moved])), Renewed(
        item.id, seq, deadline
    )


def complete(
    item: Item,
    *,
    seq: int,
    holder: str,
    digest: str,
    queue_state: QueueState,
) -> tuple[Item, CompleteOutcome]:
    """Commit ``digest`` as the item's result if nobody has yet (module docstring)."""
    if not digest or len(digest) > MAX_DIGEST_CHARS:
        raise ValueError(f"digest must be 1..{MAX_DIGEST_CHARS} characters")
    found = _check_lease(item, seq, holder)
    if isinstance(found, FenceReason):
        return item, Fenced(item.id, seq, found)
    if item.state is ItemState.DONE:
        return _already_done(item, found)
    if item.state is ItemState.FAILED:
        return item, Fenced(item.id, seq, FenceReason.ITEM_FAILED)
    if queue_state is not QueueState.OPEN:
        return item, Fenced(item.id, seq, FenceReason.QUEUE_CLOSED)
    if found.state not in (LeaseState.LIVE, LeaseState.EXPIRED, LeaseState.RELEASED):
        return item, Fenced(item.id, seq, FenceReason.LEASE_FAILED)
    losers = tuple(x for x in item.live_leases if x.seq != seq)
    superseded = _set_state(losers, LeaseState.SUPERSEDED)
    winner = replace(found, state=LeaseState.COMPLETED)
    new = replace(
        item,
        state=ItemState.DONE,
        result_digest=digest,
        done_lease_seq=seq,
        leases=_with_leases(item, [winner, *superseded]),
    )
    return new, Committed(
        item.id, item.idx, seq, digest, late=not found.live, superseded=superseded
    )


def _exhausted(item: Item, policy: QueuePolicy) -> bool:
    if item.attempts >= policy.max_attempts:
        return True
    return (
        policy.max_failed_sites is not None
        and len(item.failed_sites) >= policy.max_failed_sites
    )


def fail(
    item: Item,
    *,
    seq: int,
    holder: str,
    kind: str,
    policy: QueuePolicy,
    queue_state: QueueState,
    retryable: bool = True,
) -> tuple[Item, FailOutcome]:
    """Record that a live lease's execution failed with ``kind``."""
    if not kind or len(kind) > MAX_FAILURE_KIND_CHARS:
        raise ValueError(f"kind must be 1..{MAX_FAILURE_KIND_CHARS} characters")
    found = _check_lease(item, seq, holder)
    if isinstance(found, FenceReason):
        return item, Fenced(item.id, seq, found)
    if item.state is ItemState.DONE:
        return _already_done(item, found)
    if item.state is ItemState.FAILED:
        return item, Fenced(item.id, seq, FenceReason.ITEM_FAILED)
    if queue_state is not QueueState.OPEN:
        return item, Fenced(item.id, seq, FenceReason.QUEUE_CLOSED)
    if found.state is LeaseState.FAILED:
        return item, Fenced(item.id, seq, FenceReason.LEASE_FAILED)
    if not found.live:
        return item, Fenced(item.id, seq, FenceReason.LEASE_NOT_LIVE)
    sites = item.failed_sites
    if found.site not in sites:
        sites = (*sites, found.site)
    failed_lease = replace(found, state=LeaseState.FAILED)
    counted = replace(item, attempts=item.attempts + 1, failed_sites=sites)
    others = tuple(x for x in item.live_leases if x.seq != seq)
    if not retryable or _exhausted(counted, policy):
        released = _set_state(others, LeaseState.RELEASED)
        new = replace(
            counted,
            state=ItemState.FAILED,
            failed_kind=kind,
            leases=_with_leases(item, [failed_lease, *released]),
        )
        return new, ItemFailed(item.id, item.idx, seq, kind, released)
    state = ItemState.LEASED if others else ItemState.PENDING
    new = replace(counted, state=state, leases=_with_leases(item, [failed_lease]))
    return new, Retried(item.id, seq, requeued=not others)


def expire(
    item: Item, *, now: datetime, policy: QueuePolicy
) -> tuple[Item, Expired | None]:
    """Expire every live lease whose deadline is ``<= now`` (``None`` = none due)."""
    if item.final:
        return item, None
    due = tuple(x for x in item.live_leases if x.deadline <= now)
    if not due:
        return item, None
    expired = _set_state(due, LeaseState.EXPIRED)
    attempts = item.attempts + (len(due) if policy.count_expiry else 0)
    counted = replace(item, attempts=attempts, leases=_with_leases(item, expired))
    if counted.live_leases:
        return counted, Expired(item.id, item.idx, expired, item_failed=False)
    if policy.count_expiry and _exhausted(counted, policy):
        new = replace(counted, state=ItemState.FAILED, failed_kind=EXPIRED_KIND)
        return new, Expired(item.id, item.idx, expired, item_failed=True)
    new = replace(counted, state=ItemState.PENDING)
    return new, Expired(item.id, item.idx, expired, item_failed=False)


def _release(item: Item, leases: Sequence[Lease]) -> tuple[Item, Released | None]:
    if not leases:
        return item, None
    released = _set_state(leases, LeaseState.RELEASED)
    rest = [x for x in item.live_leases if x.seq not in {y.seq for y in leases}]
    state = item.state
    if state is ItemState.LEASED and not rest:
        state = ItemState.PENDING
    new = replace(item, state=state, leases=_with_leases(item, released))
    return new, Released(item.id, released)


def release(
    item: Item, *, holder: str, seq: int | None = None
) -> tuple[Item, Released | None]:
    """Release ``holder``'s live leases of the item (one if ``seq`` is given).

    Not the item's fault (holder lost, pre-empted, cancelled): ``attempts`` is
    unchanged. The item goes back to ``pending`` when no live lease remains.
    """
    mine = [
        x
        for x in item.live_leases
        if x.holder == holder and (seq is None or x.seq == seq)
    ]
    return _release(item, mine)


def release_all(item: Item) -> tuple[Item, Released | None]:
    """Release every live lease (the queue is closing)."""
    return _release(item, item.live_leases)


# --- queue-level selection (pure helpers for the store and its callers) -----------


def next_pending(items: Iterable[Item], *, site: str, k: int) -> list[Item]:
    """The ``k`` pending items a puller at ``site`` gets next: items that have not
    failed at this site first (retry elsewhere when possible, D267c), then by
    ``idx``."""
    pending = [x for x in items if x.state is ItemState.PENDING]
    pending.sort(key=lambda x: (site in x.failed_sites, x.idx))
    return pending[: max(k, 0)]


def speculation_budget(
    *, total_items: int, speculated_items: int, max_frac: float
) -> int:
    """How many more items may get a speculative copy (``ceil(frac * total)``
    minus those that already got one; never negative)."""
    if max_frac <= 0 or total_items <= 0:
        return 0
    return max(0, math.ceil(max_frac * total_items - 1e-9) - speculated_items)


def speculation_candidates(
    items: Iterable[Item],
    *,
    holder: str,
    site: str,
    pending: int,
    budget: int,
) -> list[Item]:
    """Items an idle ``holder`` at ``site`` may duplicate, oldest live lease first.

    Empty while any item is still pending (D266: speculate only at the tail).
    With the budget spent, only items that already had a copy (whose copy has
    ended) remain eligible: the budget counts items, not copies. Choosing
    *which* candidate is worth a copy (its predicted remaining time) is the
    caller's policy.
    """
    if pending > 0:
        return []
    eligible = [
        x
        for x in items
        if speculation_refusal(x, holder=holder, site=site) is None
        and (budget > 0 or x.speculations > 0)
    ]
    eligible.sort(key=lambda x: (x.live_leases[0].leased_at, x.idx))
    return eligible


@dataclass(frozen=True, slots=True)
class SpeculationPick:
    """The candidate an idle holder should duplicate, and why (for logs)."""

    item: Item
    original: Lease
    """The live ordinary lease being duplicated."""
    elapsed_s: float
    expected_s: float
    """What the original's site was expected to need for the item."""
    remaining_s: float
    """:func:`predicted_remaining_s` of the original."""
    copy_s: float
    """What the idle site is expected to need for the item."""


def predicted_remaining_s(expected_s: float, elapsed_s: float) -> float:
    """Predicted seconds until a running lease finishes, without a progress signal.

    On schedule (``elapsed <= expected``) it is ``expected - elapsed``. Overdue,
    the expectation has been proven wrong by ``elapsed - expected`` seconds and
    nothing says how much is left, so the overdue time itself is the estimate:
    the longer a lease is late, the longer it is predicted to stay late. Both
    halves meet at 0 when ``elapsed == expected`` (``|expected - elapsed|``).
    """
    return abs(expected_s - elapsed_s)


def pick_speculation(
    candidates: Iterable[Item],
    *,
    site: str,
    now: datetime,
    copy_s: Callable[[Item], float | None],
    expected_s: Callable[[Item, Lease], float | None],
    factor: float,
    exclude: Collection[int] = (),
) -> SpeculationPick | None:
    """Which of ``candidates`` (from :func:`speculation_candidates`) an idle
    holder at ``site`` should duplicate now, or ``None``.

    For each candidate's live ordinary lease: its predicted remaining time
    (:func:`predicted_remaining_s` of ``expected_s(item, lease)`` and the
    time since it was leased) is compared with ``copy_s(item)``, the idle
    site's expected time for the item. A copy is worth it only when
    ``remaining > factor * copy`` (the copy is clearly predicted to finish
    first); among those the largest remaining time wins (ties: lower ``idx``).
    Skipped: items in ``exclude``, items that already failed at ``site`` (a
    copy there is the least likely to succeed), and items whose times are
    unknown (``None``) - no estimate, no duplicate work.
    """
    if not (math.isfinite(factor) and factor >= 1.0):
        raise ValueError("factor must be a finite number >= 1")
    best: SpeculationPick | None = None
    for item in candidates:
        live = item.live_leases
        if item.id in exclude or site in item.failed_sites or len(live) != 1:
            continue
        original = live[0]
        if original.speculative or original.site == site:
            continue
        expected = expected_s(item, original)
        copy = copy_s(item)
        if expected is None or copy is None:
            continue
        if not (math.isfinite(expected) and math.isfinite(copy)) or copy < 0:
            continue
        elapsed = max(0.0, (now - original.leased_at).total_seconds())
        remaining = predicted_remaining_s(expected, elapsed)
        if remaining <= factor * copy:
            continue
        pick = SpeculationPick(item, original, elapsed, expected, remaining, copy)
        if best is None or (remaining, -item.idx) > (best.remaining_s, -best.item.idx):
            best = pick
    return best
