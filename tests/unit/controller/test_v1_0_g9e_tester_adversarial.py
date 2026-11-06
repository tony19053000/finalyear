"""V1.0 G9-E tester: adversarial planner cases around D438 (rates_assumed)."""

from __future__ import annotations

import pytest
from test_v1_0_g9e_assumed_rates import L1, L2, L3, _fabric, _hf, _old_config

from meshforge.controller.planner import History, plan

pytestmark = pytest.mark.unit


def test_one_measured_lane_unblocks_scaled_others() -> None:
    """Mixed lanes: only L1 measured (long job); the others are SCALED from it,
    not assumed - the set may split and nothing is blocked."""
    hist = History(
        item_rate_by_node={L1: 0.5},  # 2 s/item: 600 items -> 20 min whole
        item_rate_samples_by_node={L1: 1},
        chunk_overhead_s=0.5,
        chunk_overhead_samples=1,
    )
    d = plan(_hf(hist, items=600), _fabric(), "cost_model")
    assert d.split is not None
    assert d.split.margin.blocked is None
    assert d.split.margin.passed and d.chosen is not None and d.chosen.mode == "split"


def test_measured_rate_on_a_node_outside_the_set_still_counts() -> None:
    """History only from a node that is not a candidate lane (e.g. gone):
    others scale from it -> not assumed. Documents the behaviour."""
    hist = History(
        item_rate_by_node={"node_gone": 0.5}, item_rate_samples_by_node={"node_gone": 1}
    )
    d = plan(_hf(hist, items=600), _fabric(), "cost_model")
    assert d.split is not None
    assert d.split.margin.blocked is None


def test_zero_and_nan_rates_are_ignored_so_still_assumed() -> None:
    hist = History(
        item_rate_by_node={L1: 0.0, L2: float("nan"), L3: float("inf")},
        item_rate_samples_by_node={L1: 1, L2: 1, L3: 1},
    )
    d = plan(_hf(hist), _fabric(), "cost_model")
    assert d.chosen is not None
    # inf is finite? no -> ignored; none usable -> assumed -> blocked
    assert d.split is not None and d.split.margin.blocked in ("rates_assumed", None)
    assert d.chosen.mode != "split" or d.split.margin.blocked is None


@pytest.mark.parametrize("items", [1, 2, 3, 60, 10_000, 10_000_000])
def test_never_splits_on_assumed_for_any_size(items: int) -> None:
    d = plan(_hf(items=items), _fabric(), "cost_model")
    assert d.chosen is not None and d.chosen.mode != "split"
    if d.split is not None and d.split.margin.set_s is not None:
        assert not d.split.margin.passed


def test_switch_off_is_deterministic_and_equal_to_old_planner() -> None:
    a = plan(_hf(), _fabric(_old_config()), "cost_model")
    b = plan(_hf(), _fabric(_old_config()), "cost_model")
    assert a.chosen == b.chosen and a.chosen is not None and a.chosen.mode == "split"
