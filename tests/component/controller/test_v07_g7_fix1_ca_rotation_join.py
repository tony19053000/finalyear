"""V0.7 G7 fix round 1 (D167, weak row EN-S2): a join code made before a real
``meshforge-controller fabric create --force`` is refused by the joining laptop.

EN-S2's earlier test forged a code pinned to another CA ("same mechanism").
This one does the real rotation: the CA is made and then replaced by the real
CLI as OS processes, the controller restarts on the same enrolment address
with the new CA, and the code issued under the old CA is redeemed there. The
agent must refuse ``pin_mismatch`` before it sends anything (no ``Enroll``
reaches the server, the invite stays unused, no file is written); a code made
under the new CA then works.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_controller_harness import controller_section
from sqlalchemy import text

from meshforge.agent import enrollment as agent_enrollment
from meshforge.agent.enrollment import JoinError, JoinReason
from meshforge.agent.identity import load_or_create
from meshforge.common import join_code, pki
from meshforge.controller.enrollment import listener as enroll_listener
from meshforge.controller.enrollment.codes import issue_code
from meshforge.controller.enrollment.invites import InviteStore
from meshforge.controller.fabric_ca import load_fabric
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.security,
]

_CLI = "from meshforge.controller.app import main; main()"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _cli(config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        k: v for k, v in os.environ.items() if not k.upper().startswith("MESHFORGE_")
    }
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", _CLI, *args, "--config", str(config)],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip


async def _sql(
    pg: DatabaseSettings, statement: str, params: dict[str, Any]
) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            return list((await conn.execute(text(statement), params)).all())
    finally:
        await engine.dispose()


async def _code(runtime: ControllerRuntime, pg: DatabaseSettings) -> tuple[str, str]:
    engine = create_engine(pg)
    try:
        new = await InviteStore(engine).create(ttl_s=900, actor="test")
    finally:
        await engine.dispose()
    assert runtime.fabric is not None
    issued = issue_code(
        new, fabric=runtime.fabric, host="127.0.0.1", port=runtime.enroll_port
    )
    return issued.code, issued.invite_id


async def test_a_code_from_before_fabric_create_force_is_refused_and_sends_nothing(
    pg_settings: DatabaseSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_enroll: Callable[..., Any] = enroll_listener.EnrollmentServicer.Enroll

    async def spy(self: Any, request: Any, context: Any) -> Any:
        calls.append(context.peer())
        return await real_enroll(self, request, context)

    monkeypatch.setattr(enroll_listener.EnrollmentServicer, "Enroll", spy)

    data_dir = tmp_path / "data"
    port = _free_port()
    config = tmp_path / "config.toml"
    config.write_text(
        f"data_dir = '{data_dir.as_posix()}'\n"
        "[controller]\ngrpc_bind = '127.0.0.1:47100'\nhttp_bind = '127.0.0.1:47110'\n",
        encoding="utf-8",
    )
    settings = ControllerSettings(
        data_dir=data_dir,
        controller=controller_section(
            enroll={
                "bind": f"127.0.0.1:{port}",
                "rate_per_minute": 600.0,
                "rate_burst": 100,
            }
        ),
        database=pg_settings,
    )
    created = _cli(config, "fabric", "create")
    assert created.returncode == 0, created.stderr
    old_ca = load_fabric(settings.ca_dir).cert

    async with ControllerRuntime.start(settings) as runtime:
        assert runtime.enroll_port == port
        old_code, old_invite = await _code(runtime, pg_settings)
    assert join_code.decode(old_code).ca_pin == pki.short_fingerprint(old_ca)

    rotated = _cli(config, "fabric", "create", "--force")
    assert rotated.returncode == 0, rotated.stderr
    new_ca = load_fabric(settings.ca_dir).cert
    assert pki.cert_fingerprint(new_ca) != pki.cert_fingerprint(old_ca)

    identity = load_or_create(tmp_path / "laptop" / "identity")
    async with ControllerRuntime.start(settings) as runtime:
        assert runtime.enroll_port == port  # the old code's address is live
        with pytest.raises(JoinError) as info:
            await asyncio.to_thread(agent_enrollment.join, identity, old_code)
        assert info.value.reason is JoinReason.PIN_MISMATCH
        assert calls == []  # the token never left the laptop
        used = await _sql(
            pg_settings,
            "SELECT used_at FROM join_token WHERE id = :i",
            {"i": old_invite},
        )
        assert used == [(None,)]
        assert not any(p.suffix == ".crt" for p in identity.path.iterdir())
        # Control: a code made under the new CA joins on the same address.
        new_code, _ = await _code(runtime, pg_settings)
        result = await asyncio.to_thread(agent_enrollment.join, identity, new_code)
        assert len(calls) == 1
        assert pki.cert_fingerprint(result.enrollment.ca_cert) == pki.cert_fingerprint(
            new_ca
        )
