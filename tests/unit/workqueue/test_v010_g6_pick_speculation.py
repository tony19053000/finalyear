"""V0.10 G6 (D266): the pure speculation decision - which leased item an idle
holder duplicates, and only when the copy is clearly predicted to win."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping

import pytest

from meshforge.workqueue import core
from meshforge.workqueue.core import Item, Lease, QueuePolicy, SpeculationPick

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.UTC)
P = QueuePolicy(visibility_timeout_s=600, max_attempts=3)


def at(seconds: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


def held(
    item_id: int, *, site: str, leased_s: float = 0.0, failed: tuple[str, ...] = ()
) -> Item:
    item, _ = core.lease(
        Item(id=item_id, idx=item_id, failed_sites=failed),
        holder=f"h-{site}",
        site=site,
        now=at(leased_s),
        policy=P,
    )
    return item


def pick(
    items: list[Item],
    *,
    now_s: float,
    site: str = "idle",
    times: Mapping[str, float | None] | None = None,
    factor: float = 1.5,
    exclude: tuple[int, ...] = (),
) -> SpeculationPick | None:
    table = {"idle": 1.0, "slow": 10.0, "fast": 1.0, **(times or {})}

    def copy_s(_item: Item) -> float | None:
        return table.get(site)

    def expected_s(_item: Item, lease: Lease) -> float | None:
        return table.get(lease.site)

    return core.pick_speculation(
        items,
        site=site,
        now=at(now_s),
        copy_s=copy_s,
        expected_s=expected_s,
        factor=factor,
        exclude=exclude,
    )


@pytest.mark.parametrize(
    ("expected", "elapsed", "remaining"),
    [(10, 0, 10), (10, 4, 6), (10, 10, 0), (10, 13, 3), (1, 25, 24), (0, 2, 2)],
)
def test_predicted_remaining(expected: float, elapsed: float, remaining: float) -> None:
    assert core.predicted_remaining_s(expected, elapsed) == remaining


def test_a_slow_holder_at_the_tail_is_duplicated_at_once() -> None:
    got = pick([held(1, site="slow")], now_s=0.5)
    assert got is not None and got.item.id == 1
    assert got.original.site == "slow"
    assert got.remaining_s == pytest.approx(9.5) and got.copy_s == 1.0


def test_an_on_schedule_holder_close_to_done_is_not_duplicated() -> None:
    # slow holder expected 10 s, 9 s in: 1 s left < 1.5 x 1 s
    assert pick([held(1, site="slow")], now_s=9.0) is None


def test_an_equal_holder_is_duplicated_only_once_clearly_overdue() -> None:
    # both 1 s per item: remaining = |1 - elapsed|; copy only past 1 + 1.5 s
    item = held(1, site="fast")
    assert pick([item], now_s=0.2) is None
    assert pick([item], now_s=2.4) is None
    got = pick([item], now_s=2.6)
    assert got is not None and got.remaining_s == pytest.approx(1.6)


def test_the_largest_remaining_time_wins_ties_by_index() -> None:
    items = [
        held(1, site="fast", leased_s=0),
        held(2, site="slow", leased_s=0),
        held(3, site="other", leased_s=0),
    ]
    got = pick(items, now_s=0.0, times={"other": 10.0})
    assert got is not None and got.item.id == 2  # 10 = 10: lower idx first
    got = pick(items, now_s=0.0, times={"other": 12.0})
    assert got is not None and got.item.id == 3


def test_never_the_idle_site_itself_nor_a_copy_nor_two_live_leases() -> None:
    own = held(1, site="idle")
    assert pick([own], now_s=50) is None
    item = held(2, site="slow")
    copied, copy = core.speculate(item, holder="h-x", site="x", now=at(1), policy=P)
    assert isinstance(copy, Lease)
    assert pick([copied], now_s=50) is None  # two live leases
    # only the copy is live (the original ended): no copy of a copy
    only_copy = core.release(copied, holder="h-slow")[0]
    assert len(only_copy.live_leases) == 1 and only_copy.live_leases[0].speculative
    assert pick([only_copy], now_s=50) is None


def test_items_failed_at_the_idle_site_or_excluded_are_skipped() -> None:
    assert pick([held(1, site="slow", failed=("idle",))], now_s=0) is None
    assert pick([held(1, site="slow")], now_s=0, exclude=(1,)) is None
    got = pick([held(1, site="slow", failed=("elsewhere",))], now_s=0)
    assert got is not None


def test_unknown_times_mean_no_copy() -> None:
    assert pick([held(1, site="slow")], now_s=0, times={"slow": None}) is None
    assert pick([held(1, site="slow")], now_s=0, times={"idle": None}) is None
    assert pick([held(1, site="slow")], now_s=0, times={"slow": float("nan")}) is None
    assert pick([held(1, site="slow")], now_s=0, times={"idle": float("inf")}) is None


@pytest.mark.parametrize("factor", [0.99, float("nan"), float("inf"), -1.0])
def test_factor_must_be_at_least_one(factor: float) -> None:
    with pytest.raises(ValueError, match="factor"):
        pick([held(1, site="slow")], now_s=0, factor=factor)


def test_a_bigger_factor_waits_longer() -> None:
    item = held(1, site="slow")
    assert pick([item], now_s=0.0, factor=9.0) is not None  # 10 > 9
    assert pick([item], now_s=0.0, factor=10.0) is None  # 10 = 10: not clearly


def test_clock_skew_never_gives_negative_elapsed() -> None:
    item = held(1, site="slow", leased_s=100)
    got = pick([item], now_s=0)  # now before leased_at
    assert got is not None and got.elapsed_s == 0.0
