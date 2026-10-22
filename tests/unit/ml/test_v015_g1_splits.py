"""V0.15 G1 (D333/D337): group-safe splits - a property test over random datasets.

For seeded random row sets (groups recorded under one or two families, rows
without family): no group is ever on both sides of a fold, every row is in
exactly one test side per split scheme, leave-one-family-out holds out
exactly one family per fold (by the group's family), and k-fold is
deterministic for a seed.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from meshforge.controller.ml.splits import (
    NO_FAMILY,
    group_families,
    group_kfold,
    leave_one_family_out,
)

pytestmark = pytest.mark.unit


def _rows(seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)  # noqa: S311 - test data, not security
    families = ["cpu.primes", "cpu.zlib", "io.copy", None][: rng.randint(1, 4)]
    rows: list[dict[str, Any]] = []
    for g in range(rng.randint(2, 25)):
        home = rng.choice(families)
        for _ in range(rng.randint(1, 12)):
            # a fingerprint occasionally recorded under a second family
            family = rng.choice(families) if rng.random() < 0.15 else home
            rows.append({"group": f"fp_{g}", "family": family})
    rng.shuffle(rows)
    return rows


@pytest.mark.parametrize("seed", range(200))
def test_lofo_never_leaks_a_group_and_holds_out_one_family(seed: int) -> None:
    rows = _rows(seed)
    family_of = group_families(rows)
    folds = leave_one_family_out(rows)
    assert len(folds) == len(set(family_of.values()))
    seen: list[int] = []
    for fold in folds:
        test_groups = {rows[i]["group"] for i in fold.test}
        train_groups = {rows[i]["group"] for i in fold.train}
        assert not test_groups & train_groups, "a fingerprint on both sides"
        assert {family_of[g] for g in test_groups} == {fold.name}  # one family
        assert sorted(fold.train + fold.test) == list(range(len(rows)))
        seen.extend(fold.test)
    assert sorted(seen) == list(range(len(rows)))  # each row held out once


@pytest.mark.parametrize("seed", range(200))
def test_group_kfold_never_leaks_and_is_deterministic(seed: int) -> None:
    rows = _rows(seed)
    groups = len({r["group"] for r in rows})
    k = min(5, groups)
    folds = group_kfold(rows, k, seed)
    assert folds == group_kfold(rows, k, seed)
    seen: list[int] = []
    for fold in folds:
        test_groups = {rows[i]["group"] for i in fold.test}
        train_groups = {rows[i]["group"] for i in fold.train}
        assert not test_groups & train_groups
        assert fold.test  # round-robin over >= k groups: never empty
        seen.extend(fold.test)
    assert sorted(seen) == list(range(len(rows)))


def test_a_group_takes_its_majority_family_and_no_family_is_its_own_fold() -> None:
    rows = [
        {"group": "a", "family": "x"},
        {"group": "a", "family": "x"},
        {"group": "a", "family": "y"},
        {"group": "b", "family": None},
        {"group": "c", "family": "y"},
        {"group": "d", "family": "x"},
        {"group": "d", "family": "y"},  # tie -> the smaller name
    ]
    assert group_families(rows) == {"a": "x", "b": NO_FAMILY, "c": "y", "d": "x"}
    folds = leave_one_family_out(rows)
    assert [f.name for f in folds] == ["x", "y", NO_FAMILY]
    assert folds[0].test == (0, 1, 2, 5, 6)


def test_group_kfold_refuses_too_few_groups() -> None:
    rows = [{"group": "a", "family": "x"}, {"group": "b", "family": "x"}]
    with pytest.raises(ValueError, match="group_kfold"):
        group_kfold(rows, 3, 0)
    with pytest.raises(ValueError, match="group_kfold"):
        group_kfold(rows, 1, 0)
