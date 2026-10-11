"""V0.7 G4 (D141): ``meshforge-controller user create|list|disable|enable|
reset-password`` against real PostgreSQL (the controller does not run: the
commands write the database directly, like ``node revoke``).

Pinned: the password is read from stdin (never argv) and never printed; a weak
password and a bad name are usage errors that write nothing; a duplicate is a
conflict; disable / reset-password revoke the user's live tokens; every action
leaves an audit row without a secret.
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

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import text
from typer.testing import CliRunner

from meshforge.common.errors import ExitCode
from meshforge.controller.app import app as controller_app
from meshforge.controller.auth.users import UserStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import upgrade_to_head
from meshforge.controller.store.settings import DatabaseSettings

PASSWORD = "cli-password-123"


async def _sql(pg: DatabaseSettings, statement: str) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            return list((await conn.execute(text(statement))).all())
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def migrated(pg_settings: DatabaseSettings) -> AsyncIterator[DatabaseSettings]:
    await asyncio.to_thread(upgrade_to_head, pg_settings)
    yield pg_settings


@pytest.fixture
def config(
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
    path = tmp_path / "config.toml"
    path.write_text(f"data_dir = '{(tmp_path / 'data').as_posix()}'\n", "utf-8")
    return path


def _run(config: Path, *args: str, stdin: str | None = None) -> tuple[int, str]:
    result = CliRunner().invoke(
        controller_app, [*args, "--config", str(config)], input=stdin
    )
    output = result.stdout + result.stderr
    assert PASSWORD not in output
    return result.exit_code, output


def test_user_lifecycle_through_the_cli(
    config: Path, migrated: DatabaseSettings
) -> None:
    code, out = _run(
        config, "user", "create", "alice", "--admin", "--password-stdin",
        stdin=PASSWORD + "\n",
    )  # fmt: skip
    assert code == 0, out
    assert "created admin alice (usr_" in out
    code, out = _run(config, "user", "create", "alice", "--password-stdin",
                     stdin=PASSWORD + "\n")  # fmt: skip
    assert code == ExitCode.CONFLICT, out
    code, out = _run(config, "user", "create", "bob", "--password-stdin",
                     stdin=PASSWORD + "\n")  # fmt: skip
    assert code == 0, out

    code, out = _run(config, "user", "list", "--json")
    assert code == 0
    users = {u["name"]: u for u in json.loads(out)["users"]}
    assert users["alice"]["role"] == "admin" and users["bob"]["role"] == "user"
    assert "pw_hash" not in out and "scrypt" not in out

    # Two live tokens for bob, then disable: both revoked at once.
    async def tokens() -> None:
        engine = create_engine(migrated)
        try:
            store = UserStore(engine)
            bob = await store.login_record("bob")
            assert bob is not None
            for _ in range(2):
                await store.issue_token(bob, ttl_s=3600)
        finally:
            await engine.dispose()

    asyncio.run(tokens())
    code, out = _run(config, "user", "disable", "bob")
    assert code == 0 and "2 API token(s) revoked" in out
    code, out = _run(config, "user", "enable", "bob")
    assert code == 0
    asyncio.run(tokens())
    code, out = _run(config, "user", "reset-password", "bob", "--password-stdin",
                     stdin="another-password-9\n")  # fmt: skip
    assert code == 0 and "2 API token(s) revoked" in out
    code, out = _run(config, "user", "disable", "nobody")
    assert code != 0 and "no such user" in out

    rows = asyncio.run(
        _sql(migrated, "SELECT action, details::text FROM audit_event ORDER BY id")
    )
    actions = [r[0] for r in rows]
    for action in ("user_created", "user_disabled", "user_enabled", "password_reset"):
        assert action in actions
    assert PASSWORD not in json.dumps([r[1] for r in rows])


@pytest.mark.parametrize(
    ("args", "stdin"),
    [
        (("user", "create", "carol", "--password-stdin"), "short\n"),
        (("user", "create", "Carol", "--password-stdin"), PASSWORD + "\n"),
        (("user", "create", "../x", "--password-stdin"), PASSWORD + "\n"),
    ],
)
def test_bad_input_writes_nothing(
    config: Path, migrated: DatabaseSettings, args: tuple[str, ...], stdin: str
) -> None:
    code, out = _run(config, *args, stdin=stdin)
    assert code == ExitCode.USAGE_ERROR, out
    assert asyncio.run(_sql(migrated, "SELECT count(*) FROM app_user")) == [(0,)]
