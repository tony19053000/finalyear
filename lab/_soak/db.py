"""The controller database probe of ``soak.py run`` (D410 rule 5).

One short connection per probe (a soak must not hold a session the controller's
own pool would compete with, and a probe across a Postgres restart must simply
fail once and work again). Read-only statistics queries:

* ``pg_database_size(current_database())`` - the whole database;
* per user table: ``pg_total_relation_size`` (heap + indexes + TOAST) and the
  live / dead tuple estimates from ``pg_stat_user_tables``;
* exact ``count(*)`` of ``job`` (the rule's denominator - estimates lag);
* WAL: ``sum(size) FROM pg_ls_waldir()`` - needs a superuser or ``pg_monitor``
  (the managed database's ``meshforge_admin``); without that right the value is
  ``null`` with the reason, never a guess - and ``max_wal_size`` in bytes.

A failure is a record too (``{"ok": false, "failure_type": ..., "error": ...}``):
a database that is down during a chaos action is data, not a crash of the tool.
The password never appears in a record or a message.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

PROBE_TIMEOUT_S: Final = 20.0

_TABLES_SQL: Final = """
SELECT relname, pg_total_relation_size(relid) AS bytes, n_live_tup, n_dead_tup
FROM pg_stat_user_tables ORDER BY relname
"""
_MAX_WAL_SQL: Final = (
    "SELECT setting::bigint * CASE unit WHEN 'MB' THEN 1048576 WHEN 'kB' THEN 1024 "
    "WHEN '8kB' THEN 8192 ELSE 1 END FROM pg_settings WHERE name = 'max_wal_size'"
)


class DbProbe:
    """``probe()`` -> one JSON-able record (see the module docstring)."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str | None,
    ) -> None:
        self._kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "database": database,
            "user": user,
            "password": password,
            "timeout": PROBE_TIMEOUT_S,
            "ssl": False,  # loopback only (D390)
        }

    def __call__(self) -> dict[str, Any]:
        try:
            return asyncio.run(asyncio.wait_for(self._probe(), PROBE_TIMEOUT_S * 2))
        except Exception as exc:  # noqa: BLE001 - every failure is a record
            return {
                "ok": False,
                "failure_type": _failure_type(exc),
                "error": _safe(exc, self._kwargs.get("password")),
            }

    async def _probe(self) -> dict[str, Any]:
        import asyncpg  # controller extra

        conn = await asyncpg.connect(**self._kwargs)
        try:
            db_bytes = int(
                await conn.fetchval("SELECT pg_database_size(current_database())")
            )
            tables = {
                str(r["relname"]): {
                    "bytes": int(r["bytes"]),
                    "live": int(r["n_live_tup"]),
                    "dead": int(r["n_dead_tup"]),
                }
                for r in await conn.fetch(_TABLES_SQL)
            }
            jobs: int | None = None
            if "job" in tables:
                jobs = int(await conn.fetchval("SELECT count(*) FROM job"))
            wal_bytes: int | None = None
            wal_error: str | None = None
            try:
                wal_bytes = int(
                    await conn.fetchval(
                        "SELECT coalesce(sum(size), 0) FROM pg_ls_waldir()"
                    )
                )
            except asyncpg.PostgresError as exc:
                wal_error = type(exc).__name__  # InsufficientPrivilegeError
            max_wal = await conn.fetchval(_MAX_WAL_SQL)
        finally:
            await conn.close()
        return {
            "ok": True,
            "db_bytes": db_bytes,
            "jobs": jobs,
            "tables": tables,
            "wal_bytes": wal_bytes,
            "wal_error": wal_error,
            "max_wal_bytes": int(max_wal) if max_wal is not None else None,
        }


def _failure_type(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return "db_timeout"
    if isinstance(exc, ConnectionError | OSError):
        return "db_unreachable"
    return "db_error"


def _safe(exc: BaseException, password: object) -> str:
    text = f"{type(exc).__name__}: {exc}".strip()
    if isinstance(password, str) and password:
        text = text.replace(password, "***")
    return text[:300]
