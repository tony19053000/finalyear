"""V0.7 G2 (0.7.3, D135, D151): invites on real PostgreSQL.

* the token is stored as SHA-256 only - neither the token nor the code appears
  in any column of ``join_token`` / ``audit_event``;
* TTL bounds 1 min - 24 h (max accepted, max+1 refused, both ends);
* **single use under a race**: 16 concurrent transactions redeem one code,
  exactly one wins; a redeem that rolls back does not burn the code;
* refusal reasons: unknown / used / revoked / expired;
* revoke: pending -> revoked (+ audit row), twice / used -> conflict, unknown ->
  not found;
* ``meshforge-controller fabric invite|invites``: no CA -> config_error and
  nothing written; a code that decodes to the right address and CA pin; port 0
  without ``--address`` refused; list and revoke.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text
from typer.testing import CliRunner

from meshforge.common import join_code, pki
from meshforge.common.errors import ExitCode, FailureType
from meshforge.common.ids import new_node_id
from meshforge.controller.app import app as controller_app
from meshforge.controller.enrollment.invites import (
    InviteConflictError,
    InviteError,
    InviteNotFoundError,
    InviteState,
    InviteStore,
    redeem,
    why_unusable,
)
from meshforge.controller.fabric_ca import create_fabric
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]


async def _sql(
    pg: DatabaseSettings, statement: str, params: dict[str, Any] | None = None
) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params or {})
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def migrated(pg_settings: DatabaseSettings) -> AsyncIterator[DatabaseSettings]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    yield pg_settings


@pytest_asyncio.fixture
async def store(migrated: DatabaseSettings) -> AsyncIterator[InviteStore]:
    engine = create_engine(migrated)
    try:
        yield InviteStore(engine)
    finally:
        await engine.dispose()


async def _dump(pg: DatabaseSettings) -> str:
    rows = await _sql(
        pg,
        "SELECT coalesce(string_agg(row_to_json(j)::text, ' '), '') FROM join_token j "
        "UNION ALL SELECT coalesce(string_agg(row_to_json(a)::text, ' '), '') "
        "FROM audit_event a",
    )
    return " ".join(str(r[0]) for r in rows)


@pytest.mark.asyncio
async def test_token_is_stored_hashed_only(
    store: InviteStore, migrated: DatabaseSettings
) -> None:
    new = await store.create(ttl_s=900, actor="test")
    code = join_code.encode(
        join_code.JoinCode(
            host="10.0.0.1", port=47101, token=new.token, ca_pin="a" * 32
        )
    )
    dump = await _dump(migrated)
    assert join_code.token_hash(new.token) in dump
    assert new.token.hex() not in dump
    for group in code.split("-")[1:-1]:
        assert group not in dump
    assert new.record.state is InviteState.PENDING
    life = new.record.expires_at - new.record.created_at
    assert life.total_seconds() == pytest.approx(900, abs=1)
    audit = await _sql(migrated, "SELECT action, actor, invite_id FROM audit_event")
    assert audit == [("invite_created", "test", new.record.id)]


@pytest.mark.asyncio
async def test_ttl_bounds(store: InviteStore) -> None:
    for ttl in (60, 86_400):
        await store.create(ttl_s=ttl, actor="test")
    for ttl in (59, 86_401, 0, -1):
        with pytest.raises(InviteError, match="1 min"):
            await store.create(ttl_s=ttl, actor="test")


@pytest.mark.asyncio
async def test_single_use_under_a_16_way_race(
    store: InviteStore, migrated: DatabaseSettings
) -> None:
    new = await store.create(ttl_s=900, actor="test")
    digest = join_code.token_hash(new.token)
    engine = create_engine(migrated.model_copy(update={"pool_size": 20}))
    sessions = create_session_factory(engine)
    start = asyncio.Event()

    async def consumer() -> str | None:
        node = str(new_node_id())
        await start.wait()
        async with sessions() as db, db.begin():
            won = await redeem(db, digest, node_id=node)
            await asyncio.sleep(0.05)  # hold the row lock: the others queue on it
        return node if won is not None else None

    try:
        tasks = [asyncio.create_task(consumer()) for _ in range(16)]
        await asyncio.sleep(0.05)
        start.set()
        results = await asyncio.gather(*tasks)
    finally:
        await engine.dispose()
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    rows = await _sql(migrated, "SELECT used_by_node FROM join_token")
    assert rows == [(winners[0],)]


@pytest.mark.asyncio
async def test_a_rolled_back_redeem_does_not_burn_the_code(
    store: InviteStore, migrated: DatabaseSettings
) -> None:
    new = await store.create(ttl_s=900, actor="test")
    digest = join_code.token_hash(new.token)
    engine = create_engine(migrated)
    sessions = create_session_factory(engine)
    try:
        async with sessions() as db, db.begin():
            assert await redeem(db, digest, node_id=str(new_node_id()))
            await db.rollback()
        async with sessions() as db, db.begin():
            assert await redeem(db, digest, node_id=str(new_node_id())) is not None
        async with sessions() as db, db.begin():
            assert await redeem(db, digest, node_id=str(new_node_id())) is None
            assert (await why_unusable(db, digest))[0] == "token_used"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_refusal_reasons(store: InviteStore, migrated: DatabaseSettings) -> None:
    expired = await store.create(ttl_s=60, actor="test")
    await _sql(
        migrated,
        "UPDATE join_token SET created_at = now() - interval '2 hours', "
        "expires_at = now() - interval '1 second' WHERE id = :i",
        {"i": expired.record.id},
    )
    revoked = await store.create(ttl_s=60, actor="test")
    await store.revoke(revoked.record.id, actor="test")
    engine = create_engine(migrated)
    sessions = create_session_factory(engine)
    try:
        async with sessions() as db, db.begin():
            for token, reason in (
                (expired.token, "token_expired"),
                (revoked.token, "token_revoked"),
                (join_code.new_token(), "token_unknown"),
            ):
                digest = join_code.token_hash(token)
                assert await redeem(db, digest, node_id=str(new_node_id())) is None
                assert (await why_unusable(db, digest))[0] == reason
    finally:
        await engine.dispose()
    states = {r.id: r.state for r in await store.list()}
    assert states[expired.record.id] is InviteState.EXPIRED
    assert states[revoked.record.id] is InviteState.REVOKED


@pytest.mark.asyncio
async def test_revoke_rules(store: InviteStore, migrated: DatabaseSettings) -> None:
    new = await store.create(ttl_s=900, actor="test")
    record = await store.revoke(new.record.id, actor="admin")
    assert record.state is InviteState.REVOKED
    with pytest.raises(InviteConflictError, match="already revoked") as twice:
        await store.revoke(new.record.id, actor="admin")
    assert twice.value.failure_type is FailureType.CONFLICT
    used = await store.create(ttl_s=900, actor="test")
    engine = create_engine(migrated)
    try:
        async with create_session_factory(engine)() as db, db.begin():
            digest = join_code.token_hash(used.token)
            await redeem(db, digest, node_id=str(new_node_id()))
    finally:
        await engine.dispose()
    with pytest.raises(InviteConflictError, match="already used"):
        await store.revoke(used.record.id, actor="admin")
    with pytest.raises(InviteNotFoundError):
        await store.revoke("inv_" + "0" * 26, actor="admin")
    with pytest.raises(InviteError, match="not an invite id"):
        await store.revoke("node_" + "0" * 26, actor="admin")
    actions = await _sql(migrated, "SELECT action FROM audit_event ORDER BY id")
    assert [a for (a,) in actions].count("invite_revoked") == 1


# ------------------------------------------------------------- controller CLI


@pytest.fixture
def cli_env(
    migrated: DatabaseSettings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    for key, value in {
        "HOST": migrated.host,
        "PORT": str(migrated.port),
        "NAME": migrated.name,
        "USER": migrated.user,
        "PASSWORD": migrated.password.get_secret_value(),
    }.items():
        monkeypatch.setenv(f"MESHFORGE_DATABASE__{key}", value)
    config = tmp_path / "config.toml"
    config.write_text(
        f"data_dir = '{(tmp_path / 'data').as_posix()}'\n"
        "[controller]\ngrpc_bind = '127.0.0.1:47100'\n",
        encoding="utf-8",
    )
    return config


def _run(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(controller_app, list(args))
    return result.exit_code, result.stdout, result.stderr


def test_cli_invite_needs_a_ca_and_writes_nothing_without_one(
    cli_env: Path, migrated: DatabaseSettings
) -> None:
    code, out, err = _run("fabric", "invite", "--config", str(cli_env))
    assert code == ExitCode.CONFIG_ERROR, err
    assert "fabric create" in err and out == ""
    assert asyncio.run(_sql(migrated, "SELECT count(*) FROM join_token")) == [(0,)]


def test_cli_invite_prints_a_code_for_this_fabric(
    cli_env: Path, tmp_path: Path, migrated: DatabaseSettings
) -> None:
    fabric = create_fabric(tmp_path / "data" / "controller" / "ca")
    code, out, err = _run("fabric", "invite", "--ttl", "2h", "--config", str(cli_env))
    assert code == ExitCode.OK, err
    decoded = join_code.decode(out.strip())
    assert decoded.address == "127.0.0.1:47101"  # grpc host, default enroll port
    assert decoded.ca_pin == pki.short_fingerprint(fabric.cert)
    assert pki.fingerprint_matches(fabric.cert, decoded.ca_pin, require_full=False)
    assert "meshforge-agent join" in err and out.strip() not in err
    rows = asyncio.run(
        _sql(migrated, "SELECT token_hash, expires_at - created_at FROM join_token")
    )
    assert rows[0][0] == decoded.token_hash
    assert rows[0][1].total_seconds() == pytest.approx(7200, abs=1)
    code, out, err = _run(
        "fabric", "invite", "--address", "ctl.lan:9000", "--replace",
        str(new_node_id()), "--json", "--config", str(cli_env),
    )  # fmt: skip
    assert code == ExitCode.OK, err
    issued = json.loads(out)
    assert join_code.decode(issued["code"]).address == "ctl.lan:9000"
    assert issued["replace_node_id"].startswith("node_")


@pytest.mark.parametrize(
    ("args", "exit_code"),
    [
        (("--ttl", "30s"), ExitCode.USAGE_ERROR),
        (("--ttl", "25h"), ExitCode.USAGE_ERROR),
        (("--ttl", "soon"), ExitCode.USAGE_ERROR),
        (("--replace", "nobody"), ExitCode.USAGE_ERROR),
        (("--address", "0.0.0.0"), ExitCode.CONFIG_ERROR),  # noqa: S104 - refused
    ],
)
def test_cli_invite_refusals(
    cli_env: Path,
    tmp_path: Path,
    migrated: DatabaseSettings,
    args: tuple[str, ...],
    exit_code: ExitCode,
) -> None:
    create_fabric(tmp_path / "data" / "controller" / "ca")
    code, out, err = _run("fabric", "invite", *args, "--config", str(cli_env))
    assert code == exit_code, err
    assert out == ""
    assert asyncio.run(_sql(migrated, "SELECT count(*) FROM join_token")) == [(0,)]


def test_cli_invite_port_zero_needs_an_address(cli_env: Path, tmp_path: Path) -> None:
    create_fabric(tmp_path / "data" / "controller" / "ca")
    cli_env.write_text(
        cli_env.read_text() + "[controller.enroll]\nbind = '127.0.0.1:0'\n",
        encoding="utf-8",
    )
    code, _, err = _run("fabric", "invite", "--config", str(cli_env))
    assert code == ExitCode.CONFIG_ERROR and "--address" in err
    code, out, err = _run(
        "fabric", "invite", "--address", "127.0.0.1:5555", "--config", str(cli_env)
    )
    assert code == ExitCode.OK, err
    assert join_code.decode(out.strip()).port == 5555


def test_cli_invites_list_and_revoke(cli_env: Path, tmp_path: Path) -> None:
    create_fabric(tmp_path / "data" / "controller" / "ca")
    _, out, _ = _run("fabric", "invite", "--json", "--config", str(cli_env))
    invite_id = json.loads(out)["invite_id"]
    code, out, err = _run("fabric", "invites", "--json", "--config", str(cli_env))
    assert code == ExitCode.OK, err
    listed = json.loads(out)["invites"]
    assert [i["id"] for i in listed] == [invite_id]
    assert listed[0]["state"] == "pending" and "code" not in listed[0]
    code, out, _ = _run(
        "fabric", "invites", "--revoke", invite_id, "--config", str(cli_env)
    )
    assert code == ExitCode.OK and invite_id in out
    code, _, err = _run(
        "fabric", "invites", "--revoke", invite_id, "--config", str(cli_env)
    )
    assert code == ExitCode.CONFLICT and "already revoked" in err
    code, out, _ = _run("fabric", "invites", "--config", str(cli_env))
    assert code == ExitCode.OK and "revoked" in out
