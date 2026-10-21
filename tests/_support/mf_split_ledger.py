"""The exactly-once ledger check of a split job, as a pure function (V0.10 TD1).

:func:`mf_split_cluster.check_exactly_once` reads the rows from PostgreSQL and
hands them to :func:`assert_exactly_once`; keeping the check here, free of the
cluster harness (``psutil``, processes, a database), lets a unit test prove it
fails on a broken ledger and passes on a clean one.

Rows are anything with the named attributes (SQLAlchemy ``Row`` objects in the
cluster tests, ``SimpleNamespace`` in the unit test):

* ``leases`` - one row per (work item, lease) - a LEFT JOIN, so an item with no
  lease has ``seq``/``state``/``holder`` = None: ``idx``, ``item_state``,
  ``done_lease_seq``, ``result_digest``, ``seq``, ``state``, ``holder``;
* ``records`` - the job's ``chunk_record`` rows: ``chunk_index``,
  ``lease_seq``, ``first_item``, ``items``, ``won``, ``outcome``,
  ``result_digest`` (other columns are passed through untouched).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

__all__ = ["assert_exactly_once"]


def assert_exactly_once(
    leases: Sequence[Any], records: Sequence[Any], items: int
) -> None:
    """The ledger of a split job that has **ended** (succeeded); raises
    ``AssertionError`` naming the broken rule:

    * the queue has chunks ``0 .. n-1`` and every one is ``done``;
    * **no lease is still** ``live`` - a live lease once the job has ended is a
      leaked lease (a lane that was never told, a speculative copy never
      superseded), whatever the other rows say;
    * every chunk has **exactly one** ``completed`` lease, the one the item
      names (``done_lease_seq``);
    * the chunk records hold **exactly one** ``committed`` (``won``) row per
      chunk - that lease, that result digest - and the committed chunks cover
      items ``0 .. items-1`` once, in order, with no gap or overlap;
    * **every lease ever granted** has its chunk record (same chunk, same lease
      seq) and no record names a lease that does not exist.
    """
    chunks = sorted({r.idx for r in leases})
    assert chunks == list(range(len(chunks))) and chunks, leases
    assert all(r.item_state == "done" for r in leases), [
        (r.idx, r.item_state) for r in leases if r.item_state != "done"
    ]
    live = [(r.idx, r.seq, r.holder) for r in leases if r.state == "live"]
    assert not live, (
        f"leases still live after the job ended (chunk, seq, holder): {live}"
    )
    completed = Counter(r.idx for r in leases if r.state == "completed")
    assert completed == dict.fromkeys(chunks, 1), completed
    for r in leases:
        if r.state == "completed":
            assert r.seq == r.done_lease_seq, r

    committed = [r for r in records if r.outcome == "committed"]
    assert [r.chunk_index for r in committed] == chunks, [
        (r.chunk_index, r.lease_seq, r.outcome) for r in records
    ]
    assert all(r.won for r in committed), committed
    assert sum(1 for r in records if r.won) == len(chunks), records
    item_of = {r.idx: r for r in leases if r.state == "completed"}
    span = 0
    for r in committed:
        winner = item_of[r.chunk_index]
        assert r.lease_seq == winner.seq, (r, winner)
        assert r.result_digest == winner.result_digest, (r, winner)
        assert r.first_item == span and r.items > 0, (r, span)
        span += r.items
    assert span == items, (span, items)

    granted = {(r.idx, r.seq) for r in leases if r.seq is not None}
    recorded = {(r.chunk_index, r.lease_seq) for r in records}
    assert granted == recorded, {
        "leases without a record": sorted(granted - recorded),
        "records without a lease": sorted(recorded - granted),
    }
