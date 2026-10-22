"""Group-safe train/test splits of the ML dataset (V0.15 G1 for G2, D333/D337).

A **group** is a workload fingerprint (``group`` column; a job with no
fingerprint is its own group, ``job:<id>``). No split ever puts rows of one
group on both sides - a model must never be judged on a workload it trained
on (the D299/D316 leak rule).

* :func:`leave_one_family_out` - one fold per workload family: the test side
  is every row whose **group's family** is that family. A group's family is
  the most frequent family of its rows (ties: the smaller name), so a
  fingerprint recorded under two families still lands on one side only. Rows
  with no family form one more family, ``(none)``.
* :func:`group_kfold` - ``k`` folds of whole groups, shuffled by ``seed``
  (deterministic), round-robin.

Both take the dataset (or its rows: mappings with ``group`` and ``family``)
and return row **indices** into that sequence. Pure.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

__all__ = [
    "NO_FAMILY",
    "Fold",
    "group_families",
    "group_kfold",
    "leave_one_family_out",
]

NO_FAMILY: Final = "(none)"
"""The fold name of rows without a family."""


class _HasRows(Protocol):
    @property
    def rows(self) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class Fold:
    name: str
    """The held-out family (LOFO) or ``fold-<i>`` (k-fold)."""
    train: tuple[int, ...]
    test: tuple[int, ...]


def _rows(data: _HasRows | Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
    rows = getattr(data, "rows", None)
    if rows is not None:
        return rows  # type: ignore[no-any-return]
    return data  # type: ignore[return-value]


def _family(value: Any) -> str:
    return value if isinstance(value, str) and value else NO_FAMILY


def group_families(
    data: _HasRows | Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """group -> its family (module docstring)."""
    counts: dict[str, Counter[str]] = {}
    for row in _rows(data):
        counts.setdefault(str(row["group"]), Counter())[_family(row.get("family"))] += 1
    return {
        group: min(by.items(), key=lambda item: (-item[1], item[0]))[0]
        for group, by in sorted(counts.items())
    }


def leave_one_family_out(
    data: _HasRows | Sequence[Mapping[str, Any]],
) -> list[Fold]:
    """One fold per family, sorted by name (:data:`NO_FAMILY` last)."""
    rows = _rows(data)
    family_of = group_families(rows)
    families = sorted(set(family_of.values()), key=lambda f: (f == NO_FAMILY, f))
    folds: list[Fold] = []
    for family in families:
        test = tuple(
            i for i, row in enumerate(rows) if family_of[str(row["group"])] == family
        )
        held = set(test)
        train = tuple(i for i in range(len(rows)) if i not in held)
        folds.append(Fold(family, train, test))
    return folds


def group_kfold(
    data: _HasRows | Sequence[Mapping[str, Any]], k: int, seed: int
) -> list[Fold]:
    """``k`` folds of whole groups (module docstring); ValueError when there
    are fewer than ``k`` groups or ``k < 2``."""
    rows = _rows(data)
    groups = sorted({str(row["group"]) for row in rows})
    if k < 2 or len(groups) < k:
        raise ValueError(f"group_kfold needs 2 <= k <= groups ({len(groups)}), got {k}")
    random.Random(seed).shuffle(groups)  # noqa: S311 - a reproducible split, not a secret
    fold_of = {group: index % k for index, group in enumerate(groups)}
    folds: list[Fold] = []
    for index in range(k):
        test = tuple(
            i for i, row in enumerate(rows) if fold_of[str(row["group"])] == index
        )
        held = set(test)
        train = tuple(i for i in range(len(rows)) if i not in held)
        folds.append(Fold(f"fold-{index}", train, test))
    return folds
