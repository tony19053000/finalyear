"""V0.10 G2 (D262): the pure work-queue core - every transition, fencing,
idempotent completion, at most two live leases, expiry, release, speculation."""

from __future__ import annotations

import datetime as dt

import pytest

from meshforge.workqueue import core
from meshforge.workqueue.core import (
    EXPIRED_KIND,
    AlreadyDone,
    Committed,
    Expired,
    Fenced,
    FenceReason,
    Item,
    ItemFailed,
    ItemState,
    Lease,
    LeaseState,
    QueuePolicy,
    QueueState,
    Released,
    Renewed,
    Retried,
    SpeculationRefusal,
)

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.UTC)
P = QueuePolicy(visibility_timeout_s=60, max_attempts=3)
OPEN = QueueState.OPEN
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64


def at(seconds: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


def leased(
    item: Item | None = None,
    *,
    holder: str = "h1",
    site: str = "n1",
    now: dt.datetime = T0,
    policy: QueuePolicy = P,
) -> tuple[Item, Lease]:
    return core.lease(
        item or Item(id=1, idx=0), holder=holder, site=site, now=now, policy=policy
    )


def speculated(item: Item, *, holder: str = "h2", site: str = "n2") -> Item:
    new, copy = core.speculate(item, holder=holder, site=site, now=at(5), policy=P)
    assert isinstance(copy, Lease)
    return new


# --- policy -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"visibility_timeout_s": 0},
        {"visibility_timeout_s": float("nan")},
        {"visibility_timeout_s": 86_401},
        {"max_attempts": 0},
        {"max_attempts": 101},
        {"max_failed_sites": 0},
        {"speculative_max_frac": -0.1},
        {"speculative_max_frac": 1.5},
    ],
)
def test_policy_refuses_out_of_range_values(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        QueuePolicy(**kwargs)  # type: ignore[arg-type]


# --- lease ------------------------------------------------------------------------


def test_lease_issues_seq_1_with_the_visibility_deadline() -> None:
    item, lease = leased()
    assert item.state is ItemState.LEASED
    assert item.lease_seq == lease.seq == 1
    assert lease.deadline == at(60)
    assert lease.live and not lease.speculative
    assert item.live_leases == (lease,)


def test_lease_timeout_override() -> None:
    _item, lease = core.lease(
        Item(id=1, idx=0), holder="h", site="n", now=T0, policy=P, timeout_s=5
    )
    assert lease.deadline == at(5)
    with pytest.raises(ValueError):
        core.lease(
            Item(id=1, idx=0), holder="h", site="n", now=T0, policy=P, timeout_s=0
        )


@pytest.mark.parametrize("state", [ItemState.LEASED, ItemState.DONE, ItemState.FAILED])
def test_lease_refuses_an_item_that_is_not_pending(state: ItemState) -> None:
    with pytest.raises(ValueError):
        core.lease(
            Item(id=1, idx=0, state=state), holder="h", site="n", now=T0, policy=P
        )


@pytest.mark.parametrize(("holder", "site"), [("", "n"), ("h", ""), ("h" * 65, "n")])
def test_lease_refuses_bad_names(holder: str, site: str) -> None:
    with pytest.raises(ValueError):
        core.lease(Item(id=1, idx=0), holder=holder, site=site, now=T0, policy=P)


# --- renew ------------------------------------------------------------------------


def test_renew_moves_the_deadline_forward_never_back() -> None:
    item, _ = leased()
    item, out = core.renew(item, seq=1, holder="h1", now=at(30), policy=P)
    assert out == Renewed(1, 1, at(90))
    assert item.lease(1) is not None and item.lease(1).deadline == at(90)  # type: ignore[union-attr]
    same, out2 = core.renew(item, seq=1, holder="h1", now=at(1), policy=P)
    assert out2 == Renewed(1, 1, at(90)) and same is item


@pytest.mark.parametrize(
    ("seq", "holder", "reason"),
    [
        (2, "h1", FenceReason.UNKNOWN_LEASE),
        (1, "h9", FenceReason.WRONG_HOLDER),
    ],
)
def test_renew_fences_unknown_and_foreign_leases(
    seq: int, holder: str, reason: FenceReason
) -> None:
    item, _ = leased()
    same, out = core.renew(item, seq=seq, holder=holder, now=at(1), policy=P)
    assert out == Fenced(1, seq, reason) and same is item


def test_renew_of_an_expired_lease_is_fenced() -> None:
    item, _ = leased()
    item, _exp = core.expire(item, now=at(60), policy=P)
    same, out = core.renew(item, seq=1, holder="h1", now=at(61), policy=P)
    assert out == Fenced(1, 1, FenceReason.LEASE_NOT_LIVE) and same is item


# --- complete ---------------------------------------------------------------------


def test_complete_commits_once_and_replays_get_already_done() -> None:
    item, _ = leased()
    item, out = core.complete(item, seq=1, holder="h1", digest=D1, queue_state=OPEN)
    assert out == Committed(1, 0, 1, D1, late=False, superseded=())
    assert item.state is ItemState.DONE
    assert item.result_digest == D1 and item.done_lease_seq == 1
    assert item.lease(1).state is LeaseState.COMPLETED  # type: ignore[union-attr]
    again, out2 = core.complete(item, seq=1, holder="h1", digest=D2, queue_state=OPEN)
    assert out2 == AlreadyDone(1, 1, 1, D1) and out2.own
    assert again is item  # nothing changes, the winner's digest is kept


def test_complete_after_done_on_a_closed_queue_still_says_already_done() -> None:
    item, _ = leased()
    item, _ = core.complete(item, seq=1, holder="h1", digest=D1, queue_state=OPEN)
    _, out = core.complete(
        item, seq=1, holder="h1", digest=D1, queue_state=QueueState.DONE
    )
    assert isinstance(out, AlreadyDone)


def test_first_of_two_copies_wins_and_the_other_is_superseded() -> None:
    item, _ = leased()
    item = speculated(item)
    assert len(item.live_leases) == 2
    item, out = core.complete(item, seq=2, holder="h2", digest=D2, queue_state=OPEN)
    assert isinstance(out, Committed)
    assert [x.seq for x in out.superseded] == [1]
    assert item.lease(1).state is LeaseState.SUPERSEDED  # type: ignore[union-attr]
    assert not item.live_leases
    late, out2 = core.complete(item, seq=1, holder="h1", digest=D1, queue_state=OPEN)
    assert out2 == AlreadyDone(1, 1, 2, D2) and not out2.own
    assert late is item


def test_complete_after_expiry_with_item_still_pending_commits_late() -> None:
    item, _ = leased()
    item, exp = core.expire(item, now=at(60), policy=P)
    assert exp is not None and item.state is ItemState.PENDING
    item, out = core.complete(item, seq=1, holder="h1", digest=D1, queue_state=OPEN)
    assert isinstance(out, Committed) and out.late
    assert item.state is ItemState.DONE


def test_late_result_of_an_expired_lease_supersedes_the_new_holder() -> None:
    item, _ = leased()
    item, _ = core.expire(item, now=at(60), policy=P)
    item, _ = leased(item, holder="h2", site="n2", now=at(61))
    item, out = core.complete(item, seq=1, holder="h1", digest=D1, queue_state=OPEN)
    assert isinstance(out, Committed) and out.late
    assert [x.seq for x in out.superseded] == [2]
    _, out2 = core.complete(item, seq=2, holder="h2", digest=D2, queue_state=OPEN)
    assert out2 == AlreadyDone(1, 2, 1, D1)


def test_late_result_of_a_released_lease_commits() -> None:
    item, _ = leased()
    item, rel = core.release(item, holder="h1")
    assert rel is not None and item.state is ItemState.PENDING
    item, out = core.complete(item, seq=1, holder="h1", digest=D1, queue_state=OPEN)
    assert isinstance(out, Committed) and out.late


@pytest.mark.parametrize(
    ("seq", "holder", "reason"),
    [(9, "h1", FenceReason.UNKNOWN_LEASE), (1, "h9", FenceReason.WRONG_HOLDER)],
)
def test_complete_fences_unknown_and_foreign(
    seq: int, holder: str, reason: FenceReason
) -> None:
    item, _ = leased()
    same, out = core.complete(item, seq=seq, holder=holder, digest=D1, queue_state=OPEN)
    assert out == Fenced(1, seq, reason) and same is item


def test_a_foreign_holder_does_not_learn_the_winner_digest() -> None:
    item, _ = leased()
    item, _ = core.complete(item, seq=1, holder="h1", digest=D1, queue_state=OPEN)
    _, out = core.complete(item, seq=1, holder="h9", digest=D2, queue_state=OPEN)
    assert out == Fenced(1, 1, FenceReason.WRONG_HOLDER)


@pytest.mark.parametrize(
    "state", [QueueState.CANCELLED, QueueState.FAILED, QueueState.DONE]
)
def test_complete_on_a_closed_queue_is_fenced(state: QueueState) -> None:
    item, _ = leased()
    same, out = core.complete(item, seq=1, holder="h1", digest=D1, queue_state=state)
    assert out == Fenced(1, 1, FenceReason.QUEUE_CLOSED) and same is item


def test_result_from_a_failed_lease_is_fenced() -> None:
    item, _ = leased()
    item, _ = core.fail(
        item, seq=1, holder="h1", kind="user_error", policy=P, queue_state=OPEN
    )
    _, out = core.complete(item, seq=1, holder="h1", digest=D1, queue_state=OPEN)
    assert out == Fenced(1, 1, FenceReason.LEASE_FAILED)


def test_result_for_a_failed_item_is_fenced() -> None:
    item, _ = leased()
    item = speculated(item)
    item, out = core.fail(
        item,
        seq=1,
        holder="h1",
        kind="user_error",
        policy=P,
        queue_state=OPEN,
        retryable=False,
    )
    assert isinstance(out, ItemFailed)
    _, out2 = core.complete(item, seq=2, holder="h2", digest=D1, queue_state=OPEN)
    assert out2 == Fenced(1, 2, FenceReason.ITEM_FAILED)


@pytest.mark.parametrize("digest", ["", "x" * 129])
def test_complete_refuses_bad_digests(digest: str) -> None:
    item, _ = leased()
    with pytest.raises(ValueError):
        core.complete(item, seq=1, holder="h1", digest=digest, queue_state=OPEN)


# --- fail -------------------------------------------------------------------------


def test_retryable_failure_requeues_and_counts() -> None:
    item, _ = leased()
    item, out = core.fail(
        item, seq=1, holder="h1", kind="oom", policy=P, queue_state=OPEN
    )
    assert out == Retried(1, 1, requeued=True)
    assert item.state is ItemState.PENDING
    assert item.attempts == 1 and item.failed_sites == ("n1",)
    assert item.lease(1).state is LeaseState.FAILED  # type: ignore[union-attr]
    _, again = core.fail(
        item, seq=1, holder="h1", kind="oom", policy=P, queue_state=OPEN
    )
    assert again == Fenced(1, 1, FenceReason.LEASE_FAILED)


def test_failure_of_one_copy_leaves_the_other_running() -> None:
    item, _ = leased()
    item = speculated(item)
    item, out = core.fail(
        item, seq=1, holder="h1", kind="oom", policy=P, queue_state=OPEN
    )
    assert out == Retried(1, 1, requeued=False)
    assert item.state is ItemState.LEASED
    assert [x.seq for x in item.live_leases] == [2]


def test_attempts_exhausted_fails_the_item() -> None:
    policy = QueuePolicy(max_attempts=2)
    item = Item(id=1, idx=4)
    for n in (1, 2):
        item, _ = leased(item, holder=f"h{n}", site=f"n{n}", policy=policy)
        item, out = core.fail(
            item, seq=n, holder=f"h{n}", kind="oom", policy=policy, queue_state=OPEN
        )
    assert out == ItemFailed(1, 4, 2, "oom", released=())
    assert item.state is ItemState.FAILED and item.failed_kind == "oom"


def test_failing_at_max_failed_sites_distinct_sites_fails_the_item() -> None:
    policy = QueuePolicy(max_attempts=10, max_failed_sites=2)
    item = Item(id=1, idx=0)
    item, _ = leased(item, holder="a", site="n1", policy=policy)
    item, out = core.fail(
        item, seq=1, holder="a", kind="user_error", policy=policy, queue_state=OPEN
    )
    assert isinstance(out, Retried)
    item, _ = leased(item, holder="b", site="n1", policy=policy)  # same site again
    item, out = core.fail(
        item, seq=2, holder="b", kind="user_error", policy=policy, queue_state=OPEN
    )
    assert isinstance(out, Retried) and item.failed_sites == ("n1",)
    item, _ = leased(item, holder="c", site="n2", policy=policy)
    item, out = core.fail(
        item, seq=3, holder="c", kind="user_error", policy=policy, queue_state=OPEN
    )
    assert isinstance(out, ItemFailed)
    assert item.failed_sites == ("n1", "n2")


def test_non_retryable_failure_releases_the_other_copy() -> None:
    item, _ = leased()
    item = speculated(item)
    item, out = core.fail(
        item,
        seq=2,
        holder="h2",
        kind="user_error",
        policy=P,
        queue_state=OPEN,
        retryable=False,
    )
    assert isinstance(out, ItemFailed)
    assert [x.seq for x in out.released] == [1]
    assert not item.live_leases


def test_fail_after_done_supersedes_and_reports_already_done() -> None:
    item, _ = leased()
    item = speculated(item)
    item, _ = core.complete(item, seq=1, holder="h1", digest=D1, queue_state=OPEN)
    same, out = core.fail(
        item, seq=2, holder="h2", kind="oom", policy=P, queue_state=OPEN
    )
    assert out == AlreadyDone(1, 2, 1, D1)
    assert same.state is ItemState.DONE


def test_fail_of_an_expired_lease_is_fenced_not_counted() -> None:
    item, _ = leased()
    item, _ = core.expire(item, now=at(60), policy=P)
    same, out = core.fail(
        item, seq=1, holder="h1", kind="oom", policy=P, queue_state=OPEN
    )
    assert out == Fenced(1, 1, FenceReason.LEASE_NOT_LIVE)
    assert same.attempts == 0


def test_fail_on_a_closed_queue_is_fenced() -> None:
    item, _ = leased()
    _, out = core.fail(
        item, seq=1, holder="h1", kind="oom", policy=P, queue_state=QueueState.CANCELLED
    )
    assert out == Fenced(1, 1, FenceReason.QUEUE_CLOSED)


def test_fail_refuses_a_bad_kind() -> None:
    item, _ = leased()
    with pytest.raises(ValueError):
        core.fail(item, seq=1, holder="h1", kind="", policy=P, queue_state=OPEN)


# --- expire -----------------------------------------------------------------------


def test_expire_before_the_deadline_does_nothing() -> None:
    item, _ = leased()
    same, out = core.expire(item, now=at(59.999), policy=P)
    assert out is None and same is item


def test_expire_requeues_without_counting_by_default() -> None:
    item, _ = leased()
    item, out = core.expire(item, now=at(60), policy=P)
    assert out is not None and not out.item_failed
    assert item.state is ItemState.PENDING and item.attempts == 0
    assert item.lease(1).state is LeaseState.EXPIRED  # type: ignore[union-attr]


def test_counted_expiry_exhausts_attempts() -> None:
    policy = QueuePolicy(max_attempts=1, count_expiry=True)
    item, _ = leased(policy=policy)
    item, out = core.expire(item, now=at(60), policy=policy)
    assert out == Expired(1, 0, out.leases, item_failed=True)  # type: ignore[union-attr]
    assert item.state is ItemState.FAILED and item.failed_kind == EXPIRED_KIND


def test_expiry_of_one_copy_keeps_the_item_leased() -> None:
    item, _ = leased()  # deadline 60
    item = speculated(item)  # deadline 65
    item, out = core.expire(item, now=at(62), policy=P)
    assert out is not None and [x.seq for x in out.leases] == [1]
    assert item.state is ItemState.LEASED and [x.seq for x in item.live_leases] == [2]


def test_expire_ignores_final_items() -> None:
    item, _ = leased()
    item, _ = core.complete(item, seq=1, holder="h1", digest=D1, queue_state=OPEN)
    _, out = core.expire(item, now=at(999), policy=P)
    assert out is None


# --- release ----------------------------------------------------------------------


def test_release_holder_requeues_without_counting() -> None:
    item, _ = leased()
    item, out = core.release(item, holder="h1")
    assert isinstance(out, Released) and [x.seq for x in out.leases] == [1]
    assert item.state is ItemState.PENDING and item.attempts == 0
    assert item.lease(1).state is LeaseState.RELEASED  # type: ignore[union-attr]
    same, none = core.release(item, holder="h1")
    assert none is None and same is item


def test_release_of_one_copy_keeps_the_other() -> None:
    item, _ = leased()
    item = speculated(item)
    item, out = core.release(item, holder="h2", seq=2)
    assert out is not None and item.state is ItemState.LEASED
    assert [x.seq for x in item.live_leases] == [1]


def test_release_all() -> None:
    item, _ = leased()
    item = speculated(item)
    item, out = core.release_all(item)
    assert out is not None and len(out.leases) == 2
    assert item.state is ItemState.PENDING and not item.live_leases


# --- speculation ------------------------------------------------------------------


def test_speculative_copy_rules() -> None:
    pending = Item(id=1, idx=0)
    assert core.speculation_refusal(pending, holder="h2", site="n2") is (
        SpeculationRefusal.NOT_LEASED
    )
    item, _ = leased()
    assert core.speculation_refusal(item, holder="h2", site="n1") is (
        SpeculationRefusal.SAME_SITE
    )
    assert core.speculation_refusal(item, holder="h1", site="n2") is (
        SpeculationRefusal.SAME_HOLDER
    )
    two = speculated(item)
    assert two.speculations == 1 and two.lease_seq == 2
    assert two.lease(2).speculative  # type: ignore[union-attr]
    assert core.speculation_refusal(two, holder="h3", site="n3") is (
        SpeculationRefusal.NOT_ONE_LIVE_LEASE
    )
    same, refusal = core.speculate(two, holder="h3", site="n3", now=at(6), policy=P)
    assert refusal is SpeculationRefusal.NOT_ONE_LIVE_LEASE and same is two


def test_no_copy_of_a_copy() -> None:
    item, _ = leased()
    item = speculated(item)
    item, _ = core.expire(item, now=at(61), policy=P)  # original expired, copy live
    assert [x.seq for x in item.live_leases] == [2]
    assert core.speculation_refusal(item, holder="h3", site="n3") is (
        SpeculationRefusal.ALREADY_SPECULATIVE
    )


def test_never_more_than_two_live_leases() -> None:
    item, _ = leased()
    item = speculated(item)
    with pytest.raises(ValueError):
        core.lease(item, holder="h3", site="n3", now=at(7), policy=P)
    assert len(item.live_leases) == core.MAX_LIVE_LEASES


@pytest.mark.parametrize(
    ("total", "done", "frac", "budget"),
    [
        (10, 0, 0.1, 1),
        (100, 3, 0.1, 7),
        (3, 0, 0.1, 1),
        (5, 1, 0.1, 0),
        (10, 0, 0.0, 0),
    ],
)
def test_speculation_budget(total: int, done: int, frac: float, budget: int) -> None:
    assert (
        core.speculation_budget(total_items=total, speculated_items=done, max_frac=frac)
        == budget
    )


def test_speculation_candidates_only_at_the_tail_oldest_first() -> None:
    a, _ = leased(Item(id=1, idx=0), holder="h1", site="n1", now=at(10))
    b, _ = leased(Item(id=2, idx=1), holder="h1", site="n1", now=at(5))
    c, _ = leased(Item(id=3, idx=2), holder="h2", site="n2", now=at(1))  # same site
    items = [a, b, c]
    assert (
        core.speculation_candidates(items, holder="h2", site="n2", pending=1, budget=5)
        == []
    )
    assert (
        core.speculation_candidates(items, holder="h2", site="n2", pending=0, budget=0)
        == []
    )
    got = core.speculation_candidates(
        items, holder="h2", site="n2", pending=0, budget=5
    )
    assert [x.id for x in got] == [2, 1]


def test_next_pending_prefers_items_not_failed_here() -> None:
    items = [
        Item(id=1, idx=0, failed_sites=("n1",)),
        Item(id=2, idx=1),
        Item(id=3, idx=2, state=ItemState.DONE, result_digest=D1, done_lease_seq=1),
        Item(id=4, idx=3),
    ]
    assert [x.id for x in core.next_pending(items, site="n1", k=2)] == [2, 4]
    assert [x.id for x in core.next_pending(items, site="n1", k=5)] == [2, 4, 1]
    assert [x.id for x in core.next_pending(items, site="n2", k=1)] == [1]
    assert core.next_pending(items, site="n2", k=0) == []
