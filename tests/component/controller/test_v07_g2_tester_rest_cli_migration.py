"""V0.7 G2 tester: invite REST auth + input hostility, controller CLI hostility,
and migration 0015 with live data (real PostgreSQL, real runtime).

Derived from brief 5b item 2 ("REST, fabric token = interim admin, D151"), TTL
bounds 1 min - 24 h, "invite refused when no CA exists", and item 1 (upgrade AND
downgrade). A request that fails validation must create no row.
"""

# ruff: noqa: E501  (tester file: adversarial literals, blunt asserts)

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

import httpx
from alembic import command
from mf_controller_harness import make_settings
from sqlalchemy import text
from typer.testing import CliRunner

from meshforge.common import join_code
from meshforge.common.errors import ExitCode
from meshforge.common.fabric_token import read_token_file
from meshforge.common.ids import new_invite_id, new_node_id
from meshforge.controller.app import app as controller_app
from meshforge.controller.fabric_ca import create_fabric
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]


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
async def rt(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = make_settings(
        pg_settings,
        enroll={"bind": "127.0.0.1:0", "rate_burst": 100, "rate_per_minute": 600.0},
    )
    create_fabric(settings.ca_dir)
    async with ControllerRuntime.start(settings) as runtime:
        yield runtime


def _auth(rt: ControllerRuntime) -> dict[str, str]:
    token = read_token_file(rt.settings.token_path).get_secret_value()
    return {"authorization": f"Bearer {token}"}


async def _rows(pg: DatabaseSettings, table: str = "join_token") -> int:
    return (await _sql(pg, f"SELECT count(*) FROM {table}"))[0][0]  # noqa: S608


# ----------------------------------------------------------------------- REST auth


async def test_every_invite_endpoint_refuses_missing_and_malformed_credentials(
    rt: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    good = _auth(rt)["authorization"]
    token = good.split(" ", 1)[1]
    bad_headers: list[dict[str, str]] = [
        {},
        {"authorization": ""},
        {"authorization": "Bearer"},
        {"authorization": "bearer"},
        {"authorization": "Basic " + token},
        {"authorization": "Token " + token},
        {"authorization": token},  # no scheme
        {"authorization": good[:-1]},  # one char short
        {"authorization": good + "x"},  # one char long
        {"authorization": "Bearer " + token[::-1]},
        {"authorization": "Bearer " + "A" * 43},
        {"authorization": "Bearer " + "A" * 100_000},
        {"x-api-key": token},
        {"cookie": f"authorization={token}"},
    ]
    paths = [
        ("POST", "/v1/fabric/invites"),
        ("GET", "/v1/fabric/invites"),
        ("POST", f"/v1/fabric/invites/{new_invite_id()}/revoke"),
    ]
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{rt.http_port}") as http:
        for method, path in paths:
            for headers in bad_headers:
                response = await http.request(method, path, json={}, headers=headers)
                assert response.status_code in (401, 403), (
                    method,
                    path,
                    {k: v[:12] + "..." + v[-3:] for k, v in headers.items()},
                )
                assert token not in response.text
        # token in the query string or body is not a credential
        for url in (
            f"/v1/fabric/invites?token={token}",
            f"/v1/fabric/invites?access_token={token}",
        ):
            assert (await http.get(url)).status_code in (401, 403)
        assert (
            await http.post("/v1/fabric/invites", json={"token": token})
        ).status_code in (401, 403)
    assert await _rows(pg_settings) == 0
    assert await _rows(pg_settings, "audit_event") == 0


async def test_unauthenticated_requests_are_refused_before_body_validation(
    rt: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    """A 422 for a bad body must not be a pre-auth oracle about the API."""
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{rt.http_port}") as http:
        for content in (b"{not json", b'{"ttl_s": 59}', b'{"replace_node_id": 5}'):
            response = await http.post(
                "/v1/fabric/invites",
                content=content,
                headers={"content-type": "application/json"},
            )
            assert response.status_code in (401, 403), (content, response.text)


# --------------------------------------------------------------- REST input fuzz


async def test_hostile_invite_bodies_create_nothing_and_never_500(
    rt: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    node = str(new_node_id())
    bodies: list[Any] = [
        {"ttl_s": 59}, {"ttl_s": 86401}, {"ttl_s": 0}, {"ttl_s": -1},
        {"ttl_s": 1.5}, {"ttl_s": 1e9}, {"ttl_s": 2**63},
        {"ttl_s": [900]}, {"ttl_s": {"a": 1}},
        {"replace_node_id": "x" * 41}, {"replace_node_id": "x" * 100_000},
        {"replace_node_id": "'; DROP TABLE node;--"}, {"replace_node_id": "node_é"},
        {"replace_node_id": str(new_invite_id())}, {"replace_node_id": node + "\n"},
        {"replace_node_id": ""}, {"replace_node_id": 5}, {"replace_node_id": [node]},
        [], "x", 5, None,
    ]  # fmt: skip
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{rt.http_port}") as http:
        for body in bodies:
            response = await http.post(
                "/v1/fabric/invites", json=body, headers=_auth(rt)
            )
            if body is None:
                continue  # an empty body is "no options": see the next test
            assert response.status_code in (400, 422), (
                body,
                response.status_code,
                response.text,
            )
        raw = {"content-type": "application/json", **_auth(rt)}
        for content in (
            b"",
            b"{",
            b"\xff\xfe",
            b"[1,2",
            b'{"ttl_s": NaN}',
            b"{" * 100_000,
        ):
            response = await http.post(
                "/v1/fabric/invites", content=content, headers=raw
            )
            assert response.status_code < 500, (content[:20], response.status_code)
    assert await _rows(pg_settings, "node") == 0
    assert (await _sql(pg_settings, "SELECT count(*) FROM node"))[0][0] == 0
    # nothing was created by a REJECTED request (an empty body may legitimately
    # have created the default invite: count only well-formed outcomes)
    assert await _rows(pg_settings) <= 1


async def test_exact_ttl_bounds_over_rest_and_unknown_fields(
    rt: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{rt.http_port}") as http:
        for ttl in (60, 86400):
            made = await http.post(
                "/v1/fabric/invites", json={"ttl_s": ttl}, headers=_auth(rt)
            )
            assert made.status_code == 201, made.text
            row = (
                await _sql(
                    pg_settings,
                    "SELECT extract(epoch FROM expires_at - created_at) FROM join_token WHERE id=:i",
                    {"i": made.json()["invite_id"]},
                )
            )[0][0]
            assert float(row) == ttl
        # replace_node_id for a node nobody has seen yet is allowed (re-key a
        # not-yet-seen laptop is harmless: it is single-use and admin-issued)
        made = await http.post(
            "/v1/fabric/invites",
            json={"replace_node_id": str(new_node_id())},
            headers=_auth(rt),
        )
        assert made.status_code == 201
        # Responses never contain the token hash
        listing = (await http.get("/v1/fabric/invites", headers=_auth(rt))).json()
        flat = json.dumps(listing)
        for row_ in await _sql(pg_settings, "SELECT token_hash FROM join_token"):
            assert row_[0] not in flat
        for entry in listing["invites"]:
            assert not {"code", "token", "token_hash", "join_token"} & set(entry)


async def test_revoke_endpoint_hostile_ids_and_states(
    rt: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{rt.http_port}") as http:
        made = (
            await http.post("/v1/fabric/invites", json={}, headers=_auth(rt))
        ).json()
        for bad in (
            "nope",
            "inv_",
            "inv_" + "Z" * 30,
            "..%2f..%2fv1",
            "%00",
            "inv_é",
            "x" * 500,
        ):
            r = await http.post(f"/v1/fabric/invites/{bad}/revoke", headers=_auth(rt))
            assert r.status_code in (400, 404, 405, 422), (bad, r.status_code)
        unknown = await http.post(
            f"/v1/fabric/invites/{new_invite_id()}/revoke", headers=_auth(rt)
        )
        assert unknown.status_code == 404
        first = await http.post(
            f"/v1/fabric/invites/{made['invite_id']}/revoke", headers=_auth(rt)
        )
        assert first.status_code == 200 and first.json()["state"] == "revoked"
        again = await http.post(
            f"/v1/fabric/invites/{made['invite_id']}/revoke", headers=_auth(rt)
        )
        assert again.status_code == 409
        # used invites cannot be revoked
        used = (
            await http.post("/v1/fabric/invites", json={}, headers=_auth(rt))
        ).json()
        await _sql(
            pg_settings,
            "UPDATE join_token SET used_at = now(), used_by_node = :n WHERE id = :i",
            {"n": str(new_node_id()), "i": used["invite_id"]},
        )
        conflict = await http.post(
            f"/v1/fabric/invites/{used['invite_id']}/revoke", headers=_auth(rt)
        )
        assert conflict.status_code == 409
        # the revoked invite's code no longer enrols (state is visible in the list)
        states = {
            i["id"]: i["state"]
            for i in (await http.get("/v1/fabric/invites", headers=_auth(rt))).json()[
                "invites"
            ]
        }
        assert (
            states[made["invite_id"]] == "revoked"
            and states[used["invite_id"]] == "used"
        )


async def test_codes_made_over_rest_name_the_bound_port_and_this_fabrics_pin(
    rt: ControllerRuntime,
) -> None:
    from meshforge.common import pki

    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{rt.http_port}") as http:
        made = (
            await http.post("/v1/fabric/invites", json={}, headers=_auth(rt))
        ).json()
    code = join_code.decode(made["code"])
    assert rt.fabric is not None
    assert code.port == rt.enroll_port and code.ca_pin == pki.short_fingerprint(
        rt.fabric.cert
    )


# ----------------------------------------------------------------- controller CLI


@pytest.fixture
def cli_env(
    pg_settings: DatabaseSettings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    command.upgrade(alembic_config(pg_settings), "head")
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    for key, value in {
        "HOST": pg_settings.host,
        "PORT": str(pg_settings.port),
        "NAME": pg_settings.name,
        "USER": pg_settings.user,
        "PASSWORD": pg_settings.password.get_secret_value(),
    }.items():
        monkeypatch.setenv(f"MESHFORGE_DATABASE__{key}", value)
    config = tmp_path / "config.toml"
    config.write_text(
        f"data_dir = '{(tmp_path / 'data').as_posix()}'\n"
        "[controller]\ngrpc_bind = '127.0.0.1:47100'\n",
        encoding="utf-8",
    )
    create_fabric(tmp_path / "data" / "controller" / "ca")
    return config


def _cli(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(controller_app, list(args))
    return result.exit_code, result.stdout, result.stderr


async def test_controller_cli_ttl_boundaries_and_hostile_options(
    cli_env: Path, pg_settings: DatabaseSettings
) -> None:
    refused = [
        ("--ttl", "59s"), ("--ttl", "0"), ("--ttl", "-5m"), ("--ttl", "1441m"),
        ("--ttl", "86401"), ("--ttl", "99999999999999999999h"), ("--ttl", ""),
        ("--replace", "node_" + "A" * 200), ("--replace", "node_x\ny"),
        ("--address", "http://evil:80"), ("--address", "host:0"), ("--address", "host:99999"),
        ("--address", ":47101"), ("--address", "a b:80"), ("--address", "::"),
    ]  # fmt: skip
    for args in refused:
        code, out, err = await asyncio.to_thread(
            _cli, "fabric", "invite", *args, "--config", str(cli_env)
        )
        assert code in (ExitCode.USAGE_ERROR, ExitCode.CONFIG_ERROR, 2), (
            args,
            code,
            err,
        )
        assert out == "", (args, out)
    assert await _sql(pg_settings, "SELECT count(*) FROM join_token") == [(0,)]
    for ttl in ("60s", "1m", "86400", "24h", "1440m"):
        code, out, err = await asyncio.to_thread(
            _cli,
            "fabric",
            "invite",
            "--ttl",
            ttl,
            "--address",
            "10.0.0.5:47101",
            "--config",
            str(cli_env),
        )
        assert code == ExitCode.OK, (ttl, err)
        assert join_code.decode(out.strip()).address == "10.0.0.5:47101"
    # stdout is the code and nothing else; the code is not repeated on stderr
    assert out.strip() not in err
    rows = await _sql(
        pg_settings,
        "SELECT max(extract(epoch FROM expires_at - created_at)) FROM join_token",
    )
    assert float(rows[0][0]) == 86400


# ------------------------------------------------------------- migration 0015 + data


async def test_downgrade_with_live_data_then_upgrade_again(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    await asyncio.to_thread(command.upgrade, cfg, "head")
    node = str(new_node_id())
    await _sql(
        pg_settings,
        "INSERT INTO node (node_id, hostname, os, platform, hardware, state, first_seen_at, "
        "last_seen_at, session_epoch, cert_serial, cert_fingerprint, cert_not_after, "
        "public_key_fingerprint) VALUES (:n, 'h', 'l', 'p', '{}', 'online', now(), now(), 3, "
        ":s, :f, now() + interval '30 d', :k)",
        {"n": node, "s": "ab" * 20, "f": "cd" * 32, "k": "ef" * 32},
    )
    await _sql(
        pg_settings,
        "INSERT INTO join_token (id, token_hash, created_by, created_at, expires_at) "
        "VALUES (:i, :h, 't', now(), now() + interval '1 h')",
        {"i": str(new_invite_id()), "h": "12" * 32},
    )
    await _sql(
        pg_settings,
        "INSERT INTO audit_event (action, actor, details) VALUES ('invite_created', 't', '{}')",
    )
    await asyncio.to_thread(command.downgrade, cfg, "0014")
    cols = {
        c
        for (c,) in await _sql(
            pg_settings,
            "SELECT column_name FROM information_schema.columns WHERE table_name='node'",
        )
    }
    assert not cols & {
        "cert_serial",
        "cert_fingerprint",
        "cert_not_after",
        "public_key_fingerprint",
    }
    tables = {
        t
        for (t,) in await _sql(
            pg_settings,
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'",
        )
    }
    assert not tables & {"join_token", "audit_event"}
    survivor = await _sql(
        pg_settings,
        "SELECT state, session_epoch FROM node WHERE node_id=:n",
        {"n": node},
    )
    assert survivor == [("online", 3)]  # the node itself survives the downgrade
    await asyncio.to_thread(command.upgrade, cfg, "head")
    again = await _sql(
        pg_settings,
        "SELECT cert_serial, public_key_fingerprint FROM node WHERE node_id=:n",
        {"n": node},
    )
    assert again == [(None, None)]  # certificate facts were dropped, not resurrected
    await asyncio.to_thread(command.downgrade, cfg, "base")
    await asyncio.to_thread(command.upgrade, cfg, "head")
