"""V1.0 G9-D (D437) tester: queue-level behaviour of a non-retryable
``deadline_exceeded`` chunk failure (speculative copies, duplicates, replays)."""

from __future__ import annotations

import datetime as dt

import pytest

from meshforge.workqueue import core
from meshforge.workqueue.core import (
    FenceReason,
    Fenced,
    Item,
    ItemFailed,
    ItemState,
    QueuePolicy,
    QueueState,
)

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
P = QueuePolicy(visibility_timeout_s=60, max_attempts=3, max_failed_sites=2)
KIND = "deadline_exceeded"


def _fail(item: Item, seq: int, holder: str):  # type: ignore[no-untyped-def]
    return core.fail(
        item,
        seq=seq,
        holder=holder,
        kind=KIND,
        policy=P,
        queue_state=QueueState.OPEN,
        retryable=False,
    )


def test_single_lease_fails_item_at_once_without_requeue() -> None:
    item, _ = core.lease(Item(id=1, idx=0), holder="h1", site="n1", now=T0, policy=P)
    new, out = _fail(item, 1, "h1")
    assert isinstance(out, ItemFailed) and new.state is ItemState.FAILED
    assert new.attempts == 1 and new.failed_kind == KIND  # one try, never re-leased


def test_replayed_failure_report_is_fenced_not_double_counted() -> None:
    item, _ = core.lease(Item(id=1, idx=0), holder="h1", site="n1", now=T0, policy=P)
    new, _ = _fail(item, 1, "h1")
    again, out = _fail(new, 1, "h1")
    assert isinstance(out, Fenced) and out.reason is FenceReason.ITEM_FAILED
    assert again.attempts == 1


def test_speculative_copy_is_released_when_the_primary_hits_the_limit() -> None:
    """Documents the D437 trade-off: a speculative copy (that may still finish on
    a faster node) is killed together with the chunk that hit its time limit."""
    item, _ = core.lease(Item(id=1, idx=0), holder="h1", site="slow", now=T0, policy=P)
    item, copy = core.speculate(
        item,
        holder="h2",
        site="fast",
        now=T0 + dt.timedelta(seconds=5),
        policy=P,
    )
    new, out = _fail(item, 1, "h1")
    assert isinstance(out, ItemFailed)
    assert [x.seq for x in out.released] == [copy.seq]  # type: ignore[union-attr]
    assert not new.live_leases
