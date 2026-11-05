"""Transparent retry of a whole transaction on a PostgreSQL deadlock or
serialization failure (V1.0 G9-D2, D440).

PostgreSQL aborts one transaction of a lock cycle (``40P01``) or of a
serialization conflict (``40001``); the aborted transaction is rolled back in
full, so running the **whole** transaction again is the documented remedy. The
work queue meets ``40P01`` rarely but for real: a ``FOR UPDATE`` (``fail``,
``expire_due``) and a ``FOR KEY SHARE`` (``lease_next``, ``release``,
``renew``) following the queue row's update chain while a completion updates
its ``done_count`` (G9-D r1, reproduced with the server log).

:func:`retry_conflicts` wraps a method that opens **and commits its own
transaction** and is safe to run again after a rollback: its only effects
outside the database are idempotent (content-addressed blob writes, read
caches, log lines). Bounded (``tries``), jittered exponential backoff; every
retry is logged at info with the operation and the try number, and counted on
the instance's ``conflict_retries`` mapping when it has one - duck-typed: the
first positional argument (``self`` of a method) is checked for a
``collections.Counter`` named ``conflict_retries``; anything else (a plain
function, an object without one) is simply not counted. Any other error
propagates unchanged; the last try's conflict is logged as a warning
(``controller.store.conflict_exhausted``: op, sqlstate, tries) and
propagates (the caller's typed-error wrapper -
:func:`~meshforge.controller.jobs.store.typed_store_call` - turns it
into ``JobStoreError``; the dispatcher then retries the step).
"""

from __future__ import annotations

import asyncio
import functools
import random
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Final

from sqlalchemy.exc import DBAPIError

from meshforge.common.logging import get_logger

__all__ = [
    "CONFLICT_SQLSTATES",
    "DEFAULT_TRIES",
    "retry_conflicts",
    "sqlstate_of",
]

log = get_logger(__name__)

CONFLICT_SQLSTATES: Final = frozenset({"40P01", "40001"})
"""``deadlock_detected`` and ``serialization_failure``."""
DEFAULT_TRIES: Final = 4
"""Tries in all (the first + 3 retries)."""
BASE_DELAY_S: Final = 0.02
CAP_DELAY_S: Final = 0.5


def sqlstate_of(exc: BaseException) -> str | None:
    """The SQLSTATE of a driver error wrapped by SQLAlchemy (``None``: none)."""
    seen: BaseException | None = exc
    for _ in range(4):  # the DBAPI error, its driver cause, ...
        if seen is None:
            return None
        orig = getattr(seen, "orig", None)
        for holder in (orig, seen):
            code = getattr(holder, "sqlstate", None) or getattr(holder, "pgcode", None)
            if isinstance(code, str):
                return code
        seen = seen.__cause__
    return None


def retry_conflicts[**P, R](
    op: str,
    *,
    tries: int = DEFAULT_TRIES,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random.random,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Coroutine[Any, Any, R]]]:
    """Decorate a method whose whole transaction may run again (module doc)."""
    if tries < 1:
        raise ValueError("tries must be >= 1")

    def decorate(
        method: Callable[P, Awaitable[R]],
    ) -> Callable[P, Coroutine[Any, Any, R]]:
        @functools.wraps(method)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            attempt = 1
            while True:
                try:
                    return await method(*args, **kwargs)
                except DBAPIError as exc:
                    code = sqlstate_of(exc)
                    if code not in CONFLICT_SQLSTATES:
                        raise
                    if attempt >= tries:
                        log.warning(
                            "controller.store.conflict_exhausted",
                            op=op,
                            sqlstate=code,
                            tries=tries,
                        )
                        raise
                    delay = min(CAP_DELAY_S, BASE_DELAY_S * 2 ** (attempt - 1))
                    delay *= 0.5 + jitter()  # 0.5x .. 1.5x
                    counter = (
                        getattr(args[0], "conflict_retries", None) if args else None
                    )
                    if isinstance(counter, Counter):
                        counter[op] += 1
                    log.info(
                        "controller.store.conflict_retry",
                        op=op,
                        sqlstate=code,
                        attempt=attempt,
                        tries=tries,
                        delay_s=round(delay, 4),
                    )
                    await sleep(delay)
                    attempt += 1

        return wrapper

    return decorate
