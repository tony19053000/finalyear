"""CI fix (2026-10-27): the revocation set is read in ONE snapshot.

Seen on a GitHub runner (``test_v07_g3_revocation_agents``): a revoke committed
between the periodic sync's two reads (revoked nodes, then serials), so the
sync saw the node's serial without the node being revoked and cut its live
session ``cert_superseded`` - the agent was told to re-join and the REST revoke
answered ``session_killed=false``. Here the commit is forced exactly between
the two reads, on real PostgreSQL: the snapshot sees the revocation completely
or not at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.ids import new_node_id
from meshforge.controller.revocation import RevocationStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

SERIAL = "ab" * 20


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings)
    yield eng
    await eng.dispose()


async def _node(engine: AsyncEngine) -> str:
    node = str(new_node_id())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO node (node_id, hostname, os, platform, hardware, state, "
                "first_seen_at, last_seen_at, session_epoch, cert_serial, "
                "cert_fingerprint, cert_not_after, public_key_fingerprint) VALUES "
                "(:n, 'h', 'l', 'p', '{}', 'online', now(), now(), 1, :s, :f, "
                "now() + interval '30 d', :k)"
            ),
            {"n": node, "s": SERIAL, "f": "cd" * 32, "k": "ef" * 32},
        )
    return node


async def test_a_revoke_committed_between_the_two_reads_is_seen_whole_or_not_at_all(
    engine: AsyncEngine,
) -> None:
    node = await _node(engine)
    store = RevocationStore(engine)
    other = RevocationStore(engine)  # another request / the CLI: own connection
    real_factory: Any = store._sessions
    fired: list[bool] = []

    def interleaving_factory() -> Any:
        session = real_factory()
        first_read = session.scalars

        async def scalars(*args: Any, **kwargs: Any) -> Any:
            result = await first_read(*args, **kwargs)
            if not fired:
                fired.append(True)
                outcome = await other.revoke_node(node, actor="test")
                assert outcome.already is False
            return result

        session.scalars = scalars
        return session

    store._sessions = interleaving_factory  # type: ignore[assignment]
    snap = await store.snapshot()
    assert fired == [True]  # the revoke really committed mid-snapshot
    # Before the fix: nodes=frozenset() but serials={SERIAL} (torn read).
    assert (node in snap.nodes) == (SERIAL in snap.serials), snap
    assert node not in snap.nodes  # the snapshot predates the commit

    store._sessions = real_factory
    after = await store.snapshot()
    assert node in after.nodes and SERIAL in after.serials
    assert after.reasons[SERIAL] == "revoked"
