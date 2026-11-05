"""V1.0 G9-D2 (D440) on real PostgreSQL: a genuine deadlock between two
connections reaches us as a SQLAlchemy ``DBAPIError`` whose SQLSTATE
:func:`sqlstate_of` reads as ``40P01`` (the asyncpg adapter's ``orig``), and
:func:`retry_conflicts` re-runs the victim's whole transaction to success."""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_workqueue import Db
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from meshforge.controller.store.conflicts import retry_conflicts, sqlstate_of

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(120)]


async def _table(db: Db) -> None:
    async with db.engine.begin() as conn:
        await conn.execute(text("CREATE TABLE g9d2 (id int PRIMARY KEY, v int)"))
        await conn.execute(text("INSERT INTO g9d2 VALUES (1, 0), (2, 0)"))


async def _cross(db: Db, first: int, second: int, both: asyncio.Barrier) -> None:
    """Lock ``first``, wait until the other side holds its row, then lock
    ``second``: the two transactions form a cycle."""
    async with db.engine.begin() as conn:
        await conn.execute(
            text("UPDATE g9d2 SET v = v + 1 WHERE id = :i"), {"i": first}
        )
        await both.wait()
        await conn.execute(
            text("UPDATE g9d2 SET v = v + 1 WHERE id = :i"), {"i": second}
        )


async def test_a_real_deadlock_has_sqlstate_40p01(db: Db) -> None:
    await _table(db)
    both = asyncio.Barrier(2)
    got = await asyncio.gather(
        _cross(db, 1, 2, both), _cross(db, 2, 1, both), return_exceptions=True
    )
    errors = [g for g in got if isinstance(g, BaseException)]
    assert len(errors) == 1, got  # PostgreSQL aborts exactly one side
    assert isinstance(errors[0], DBAPIError)
    assert sqlstate_of(errors[0]) == "40P01"


async def test_the_victim_is_re_run_whole_and_succeeds(db: Db) -> None:
    await _table(db)
    both = asyncio.Barrier(2)

    class Side:
        def __init__(self) -> None:
            self.conflict_retries: Counter[str] = Counter()
            self.runs = 0

        @retry_conflicts("test.cross")
        async def run(self, first: int, second: int) -> None:
            self.runs += 1
            if self.runs == 1:
                await _cross(db, first, second, both)
            else:  # the re-run: no barrier, the other side has finished
                async with db.engine.begin() as conn:
                    for i in (first, second):
                        await conn.execute(
                            text("UPDATE g9d2 SET v = v + 1 WHERE id = :i"), {"i": i}
                        )

    a, b = Side(), Side()
    await asyncio.gather(a.run(1, 2), b.run(2, 1))
    assert sorted([a.runs, b.runs]) == [1, 2]
    assert a.conflict_retries + b.conflict_retries == Counter({"test.cross": 1})
    async with db.engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, v FROM g9d2 ORDER BY id"))).all()
    # every update applied exactly once: the aborted try left nothing behind
    assert [tuple(r) for r in rows] == [(1, 2), (2, 2)]
