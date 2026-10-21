"""V0.10 TD1 tester (independent): the exactly-once ledger check must FAIL on
bad ledgers and PASS on good ones - including through
``mf_split_cluster.check_exactly_once`` (the function the cluster tests call).

Written from the rule list (one completed lease per chunk, one committed record
per chunk, every lease has a record, items covered once, nothing left `live`).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from mf_split_ledger import assert_exactly_once

pytestmark = pytest.mark.unit


def _lease(
    idx: int, seq: int | None, state: str | None, *, done: int = 1, item: str = "done"
) -> Any:
    return SimpleNamespace(
        idx=idx,
        item_state=item,
        done_lease_seq=done,
        result_digest=f"d{idx}",
        seq=seq,
        state=state,
        holder=f"node-{idx}" if seq is not None else None,
    )


def _rec(
    chunk: int,
    seq: int,
    outcome: str,
    first: int,
    items: int,
    *,
    won: bool | None = None,
) -> Any:
    committed = outcome == "committed"
    return SimpleNamespace(
        chunk_index=chunk,
        lease_seq=seq,
        outcome=outcome,
        first_item=first,
        items=items,
        result_digest=f"d{chunk}" if committed else None,
        won=committed if won is None else won,
    )


def good() -> tuple[list[Any], list[Any]]:
    return (
        [_lease(0, 1, "completed"), _lease(1, 1, "completed")],
        [_rec(0, 1, "committed", 0, 5), _rec(1, 1, "committed", 5, 5)],
    )


def test_good_ledger_passes_and_single_chunk_passes() -> None:
    assert_exactly_once(*good(), 10)
    assert_exactly_once([_lease(0, 1, "completed")], [_rec(0, 1, "committed", 0, 3)], 3)


@pytest.mark.parametrize("state", ["live"])
def test_any_live_lease_fails_whatever_its_chunk_or_seq(state: str) -> None:
    for extra in (_lease(0, 2, state), _lease(1, 3, state)):
        leases, records = good()
        leases.append(extra)
        records.append(_rec(extra.idx, extra.seq, "superseded", extra.idx * 5, 5))
        with pytest.raises(AssertionError, match="still live"):
            assert_exactly_once(leases, records, 10)


def test_the_only_lease_of_a_chunk_being_live_fails() -> None:
    leases, records = good()
    leases[1].state = "live"
    with pytest.raises(AssertionError):
        assert_exactly_once(leases, records, 10)


def test_two_live_leases_are_both_reported() -> None:
    leases, records = good()
    leases += [_lease(0, 2, "live"), _lease(1, 2, "live")]
    with pytest.raises(AssertionError) as caught:
        assert_exactly_once(leases, records, 10)
    assert "(0, 2," in str(caught.value) and "(1, 2," in str(caught.value)


@pytest.mark.parametrize("state", ["released", "expired", "failed", "superseded"])
def test_non_live_terminal_leases_with_records_are_fine(state: str) -> None:
    leases, records = good()
    leases.append(_lease(1, 2, state))
    records.append(_rec(1, 2, state, 5, 5))
    assert_exactly_once(leases, records, 10)


def test_unknown_lease_state_is_not_silently_ok_when_it_lacks_a_record() -> None:
    leases, records = good()
    leases.append(_lease(1, 2, "released"))  # no record for it
    with pytest.raises(AssertionError):
        assert_exactly_once(leases, records, 10)


def test_item_without_any_lease_fails() -> None:
    leases, records = good()
    leases.append(_lease(2, None, None, item="pending"))
    with pytest.raises(AssertionError):
        assert_exactly_once(leases, records, 10)


def test_empty_ledger_fails() -> None:
    with pytest.raises(AssertionError):
        assert_exactly_once([], [], 0)


def test_chunk_index_gap_fails() -> None:
    leases = [_lease(0, 1, "completed"), _lease(2, 1, "completed")]
    records = [_rec(0, 1, "committed", 0, 5), _rec(2, 1, "committed", 5, 5)]
    with pytest.raises(AssertionError):
        assert_exactly_once(leases, records, 10)


def test_two_completed_leases_for_one_chunk_fail() -> None:
    leases, records = good()
    leases.append(_lease(0, 2, "completed", done=1))
    records.append(_rec(0, 2, "committed", 0, 5))
    with pytest.raises(AssertionError):
        assert_exactly_once(leases, records, 10)


def test_zero_completed_lease_fails() -> None:
    leases, records = good()
    leases[0].state = "released"
    with pytest.raises(AssertionError):
        assert_exactly_once(leases, records, 10)


def test_completed_lease_is_not_the_one_the_item_names() -> None:
    leases, records = good()
    leases[0].done_lease_seq = 7
    with pytest.raises(AssertionError):
        assert_exactly_once(leases, records, 10)


def test_overlapping_and_short_item_spans_fail() -> None:
    for first, items in ((0, 5), (4, 5), (6, 5), (5, 4), (5, 6)):
        leases, records = good()
        records[1].first_item, records[1].items = first, items
        if (first, items) == (5, 5):
            continue
        with pytest.raises(AssertionError):
            assert_exactly_once(leases, records, 10)


def test_zero_item_chunk_fails() -> None:
    leases = [_lease(0, 1, "completed"), _lease(1, 1, "completed")]
    records = [_rec(0, 1, "committed", 0, 10), _rec(1, 1, "committed", 10, 0)]
    with pytest.raises(AssertionError):
        assert_exactly_once(leases, records, 10)


def test_second_winner_flag_fails() -> None:
    leases, records = good()
    leases.append(_lease(0, 2, "superseded"))
    records.append(_rec(0, 2, "superseded", 0, 5, won=True))
    with pytest.raises(AssertionError):
        assert_exactly_once(leases, records, 10)


def test_committed_record_for_the_wrong_lease_or_digest_fails() -> None:
    leases, records = good()
    leases.append(_lease(0, 2, "superseded"))
    records.append(_rec(0, 2, "committed", 0, 5))
    records[0].outcome = "superseded"
    records[0].won = False
    with pytest.raises(AssertionError):
        assert_exactly_once(leases, records, 10)
    leases, records = good()
    records[1].result_digest = "forged"
    with pytest.raises(AssertionError):
        assert_exactly_once(leases, records, 10)


def test_record_for_a_lease_that_never_existed_fails() -> None:
    leases, records = good()
    records.append(_rec(1, 9, "expired", 5, 5))
    with pytest.raises(AssertionError):
        assert_exactly_once(leases, records, 10)


def test_item_not_done_fails() -> None:
    leases, records = good()
    leases[1].item_state = "leased"
    with pytest.raises(AssertionError):
        assert_exactly_once(leases, records, 10)


def test_check_exactly_once_wires_the_queries_to_the_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cluster-facing function: rows come from two SQL reads (stubbed here)
    and a leftover live lease must raise through it; a clean ledger returns
    the chunk records."""
    pytest.importorskip("psutil")
    import mf_split_cluster

    queries: list[str] = []

    def fake_sql(pg: Any, query: str, **params: Any) -> list[Any]:
        queries.append(query)
        assert params == {"job": "job_x"}
        return state["leases"] if "work_lease" in query else state["records"]

    monkeypatch.setattr(mf_split_cluster, "sql", fake_sql)
    state: dict[str, list[Any]] = {}
    state["leases"], state["records"] = good()
    got = mf_split_cluster.check_exactly_once(object(), "job_x", 10)
    assert got is state["records"] and len(queries) == 2
    state["leases"].append(_lease(1, 2, "live"))
    state["records"].append(_rec(1, 2, "superseded", 5, 5))
    with pytest.raises(AssertionError, match="still live"):
        mf_split_cluster.check_exactly_once(object(), "job_x", 10)


def test_the_sql_selects_the_columns_the_rule_reads() -> None:
    """Guard against the query and rule drifting apart (a column the rule reads
    but the SELECT does not provide would only fail in a cluster run)."""
    import inspect

    import mf_split_cluster

    src = inspect.getsource(mf_split_cluster.check_exactly_once)
    for column in (
        "i.idx",
        "item_state",
        "done_lease_seq",
        "result_digest",
        "l.seq",
        "l.state",
        "l.holder",
        "chunk_index",
        "lease_seq",
        "first_item",
        "items",
        "won",
        "outcome",
    ):
        assert column in src, column
    assert "LEFT JOIN work_lease" in src  # items with no lease must still appear
