"""V0.14 G6F: the D329 pool boundary in ``calibration.split_holdout``.

A fingerprint with a single matched run is pooled with the fingerprint-less
runs; its **second** run makes it a stratum of its own, which moves its first
run out of the shared pool (and changes what the pool holds out). The result
depends only on the runs, never on their input order.
"""

from __future__ import annotations

import datetime as dt
import random

import pytest

from meshforge.controller.planner.calibration import MatchedRun, split_holdout

pytestmark = pytest.mark.unit

_T0 = dt.datetime(2026, 10, 22, tzinfo=dt.UTC)


def _run(i: int, fingerprint: str | None) -> MatchedRun:
    return MatchedRun(
        decision_id=i,
        attempt_id=f"a{i}",
        node_id="n1",
        finished_at=_T0 + dt.timedelta(seconds=i),
        terms=(),
        fingerprint=fingerprint,
    )


def _ids(runs: list[MatchedRun]) -> list[int]:
    return [r.decision_id for r in runs]


def test_a_second_run_moves_the_first_out_of_the_pool() -> None:
    pool = [_run(i, None) for i in range(4)]  # t = 0..3, no fingerprint
    first_x = _run(4, "x")

    # one run of "x": pooled; the pool of 5 holds its newest = x's run
    fit, held = split_holdout([*pool, first_x])
    assert _ids(held) == [4]
    assert _ids(fit) == [0, 1, 2, 3]

    # x's second run: "x" is a stratum (4, 5) holding 5; its first run 4 is
    # no longer in the pool and goes to fit; the pool (0..3) holds 3
    fit, held = split_holdout([*pool, first_x, _run(5, "x")])
    assert _ids(held) == [3, 5]
    assert _ids(fit) == [0, 1, 2, 4]


def test_leaving_the_pool_can_shrink_what_the_pool_holds() -> None:
    # a pool of 10 (9 fingerprint-less + one run of "x") holds 2
    pool = [_run(i, None) for i in range(9)]
    _, held = split_holdout([*pool, _run(9, "x")])
    assert _ids(held) == [8, 9]
    # with x's second run the pool is 9 runs -> holds int(9 x 0.2) = 1
    _, held = split_holdout([*pool, _run(9, "x"), _run(10, "x")])
    assert _ids(held) == [8, 10]


def test_the_split_is_deterministic_whatever_the_input_order() -> None:
    runs = [_run(i, None) for i in range(6)] + [
        _run(6, "x"),
        _run(7, "y"),
        _run(8, "x"),
        _run(9, "z"),
    ]
    expected = split_holdout(runs)
    assert _ids(expected[1]) == [8, 9]  # pool (0-5, y, z: 8 runs) holds z; x holds 8
    rng = random.Random(332)  # noqa: S311 - test data
    for _ in range(25):
        shuffled = runs[:]
        rng.shuffle(shuffled)
        assert split_holdout(shuffled) == expected
