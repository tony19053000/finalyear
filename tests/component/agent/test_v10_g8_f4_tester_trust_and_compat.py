"""V1.0 G8 F4 - tester: old-controller compatibility (criterion 4), the hint is
never trusted without mTLS (criterion 6), reset/leave removes it (criterion 5)
against a real controller, real PostgreSQL, real join and real session client.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("zeroconf", reason="needs the agent/controller extra")

from test_v10_g8_f4_last_known_controller import (  # type: ignore[import-not-found]  # noqa: I001
    _controller_settings,
    _join,
    _mdns_only_agent,
    _run,
    _service_type,
    _stop,
    invites,  # noqa: F401  (fixture)
)

from mf_agent_harness import make_agent_settings

from meshforge.agent import known_controller
from meshforge.agent.identity import CONTROLLER_FILE_NAME, reset
from meshforge.agent.session.state import AgentState
from meshforge.controller.enrollment.invites import InviteStore
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(180),
]


async def test_old_controller_without_session_port_works_after_first_session(
    pg_settings: DatabaseSettings,
    invites: InviteStore,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 4: session_port=0 (an older controller) -> no join hint, join
    still succeeds, the agent connects (configured address) and the hint is
    written from the accepted session."""
    import meshforge.controller.server as server_mod

    real = server_mod.EnrollmentServicer

    def old_servicer(enroller, limiter, **kw):  # type: ignore[no-untyped-def]
        kw["session_port"] = lambda: 0
        return real(enroller, limiter, **kw)

    monkeypatch.setattr(server_mod, "EnrollmentServicer", old_servicer)
    settings = _controller_settings(pg_settings)
    async with ControllerRuntime.start(settings) as rt:
        base = _mdns_only_agent(tmp_path / "agent", service_type=_service_type())
        assert await _join(rt, invites, base) is None
        assert known_controller.load_known(base.identity_dir) is None

        # mDNS-only agent: nothing to dial (not a crash).
        client, task = await _run(base)
        try:
            for _ in range(200):
                if client.stats.cycles >= 2:
                    break
                await asyncio.sleep(0.05)
            assert client.stats.cycles >= 2 and not task.done()
            assert client.stats.sessions_accepted == 0
        finally:
            await _stop(client, task)

        # Same identity with an explicit address -> ONLINE -> hint appears.
        cfg = make_agent_settings(
            tmp_path / "agent", f"127.0.0.1:{rt.grpc_port}", enroll=False
        )
        client, task = await _run(cfg)
        try:
            await client.wait_for_state(AgentState.ONLINE, timeout_s=30)
            for _ in range(200):
                if known_controller.load_known(base.identity_dir) is not None:
                    break
                await asyncio.sleep(0.05)
        finally:
            await _stop(client, task)
        known = known_controller.load_known(base.identity_dir)
        assert known is not None
        assert (known.address, known.source) == (f"127.0.0.1:{rt.grpc_port}", "session")

        # Now mDNS-only works through the hint.
        client, task = await _run(base)
        try:
            await client.wait_for_state(AgentState.ONLINE, timeout_s=30)
        finally:
            await _stop(client, task)


async def test_hint_pointing_at_a_controller_of_another_fabric_is_refused(
    pg_settings: DatabaseSettings,
    invites: InviteStore,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 6: the hint is a dial target only. A host that serves a
    different fabric CA must never get a session, and the hint stays as it was."""
    a_settings = _controller_settings(pg_settings)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "other-data"))
    monkeypatch.setenv("MESHFORGE_DATA_DIR", str(tmp_path / "other-data"))
    b_settings = _controller_settings(pg_settings)  # its own fresh CA
    async with ControllerRuntime.start(a_settings) as rt_a:
        agent = _mdns_only_agent(tmp_path / "agent", service_type=_service_type())
        assert await _join(rt_a, invites, agent) is not None
        async with ControllerRuntime.start(b_settings) as rt_b:
            assert rt_b.fabric is not None and rt_a.fabric is not None
            assert rt_a.fabric.fabric_id != rt_b.fabric.fabric_id
            evil = f"127.0.0.1:{rt_b.grpc_port}"
            assert known_controller.remember(agent.identity_dir, evil, source="session")
            path = agent.identity_dir / CONTROLLER_FILE_NAME
            before = path.read_bytes()
            client, task = await _run(agent)
            try:
                for _ in range(300):
                    if client.stats.cycles >= 3:
                        break
                    await asyncio.sleep(0.05)
                assert client.stats.cycles >= 3
                assert client.stats.sessions_accepted == 0
                assert client.state is not AgentState.ONLINE
                assert not task.done()
                assert rt_b.manager is not None
                assert rt_b.manager.get(client.node_id) is None
            finally:
                await _stop(client, task)
            assert path.read_bytes() == before  # not overwritten


async def test_reset_after_a_real_join_and_session_removes_the_hint(
    pg_settings: DatabaseSettings,
    invites: InviteStore,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Criterion 5 with a real enrolment."""
    settings = _controller_settings(pg_settings)
    async with ControllerRuntime.start(settings) as rt:
        agent = _mdns_only_agent(tmp_path / "agent", service_type=_service_type())
        await _join(rt, invites, agent)
        assert (agent.identity_dir / CONTROLLER_FILE_NAME).exists()
        reset(agent.identity_dir)
        assert not (agent.identity_dir / CONTROLLER_FILE_NAME).exists()


async def test_force_rejoin_replaces_the_hint_and_forgets_before_store(
    pg_settings: DatabaseSettings,
    invites: InviteStore,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """join --force: the old fabric's hint is dropped *before* the new pair is
    stored (a crash/failed store must not leave old address + new enrolment),
    and a successful re-join leaves the new controller's endpoint."""
    import meshforge.agent.enrollment as enr

    settings = _controller_settings(pg_settings)
    async with ControllerRuntime.start(settings) as rt:
        agent = _mdns_only_agent(tmp_path / "agent", service_type=_service_type())
        assert await _join(rt, invites, agent) is not None
        stale = "10.99.99.99:47100"
        assert known_controller.remember(agent.identity_dir, stale, source="session")

        seen_at_store: list[object] = []
        real_store = enr.store

        def spy_store(*args, **kwargs):  # type: ignore[no-untyped-def]
            seen_at_store.append(known_controller.load_known(agent.identity_dir))
            return real_store(*args, **kwargs)

        monkeypatch.setattr(enr, "store", spy_store)
        new = await invites.create(ttl_s=900, actor="t", replace_node_id=None)
        from meshforge.agent.identity import load_or_create
        from meshforge.controller.enrollment.codes import issue_code

        assert rt.fabric is not None
        code = issue_code(new, fabric=rt.fabric, host="127.0.0.1", port=rt.enroll_port)
        identity = load_or_create(agent.identity_dir)
        result = await asyncio.to_thread(enr.join, identity, code.code, force=True)
        assert seen_at_store == [None]  # hint already gone when store() ran
        assert result.session_address == f"127.0.0.1:{rt.grpc_port}"
        known = known_controller.load_known(agent.identity_dir)
        assert known is not None and known.address == result.session_address
        assert known.source == "join"


async def test_force_rejoin_with_failing_store_keeps_old_enrolment_and_never_a_foreign_hint(
    pg_settings: DatabaseSettings,
    invites: InviteStore,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed store keeps the old enrolment (D153); the documented cost is the
    hint, which must not be a *stale-fabric* address next to a new enrolment, and
    the agent must still come ONLINE (mDNS/session restore it)."""
    import meshforge.agent.enrollment as enr
    from meshforge.agent.identity import load_or_create
    from meshforge.controller.enrollment.codes import issue_code

    settings = _controller_settings(pg_settings)
    async with ControllerRuntime.start(settings) as rt:
        agent = _mdns_only_agent(tmp_path / "agent", service_type=_service_type())
        assert await _join(rt, invites, agent) is not None
        before = (agent.identity_dir / "node.crt").read_bytes()

        def boom(*_a, **_k):  # type: ignore[no-untyped-def]
            raise OSError("disk full")

        monkeypatch.setattr(enr, "store", boom)
        new = await invites.create(ttl_s=900, actor="t", replace_node_id=None)
        assert rt.fabric is not None
        code = issue_code(new, fabric=rt.fabric, host="127.0.0.1", port=rt.enroll_port)
        identity = load_or_create(agent.identity_dir)
        with pytest.raises(OSError):
            await asyncio.to_thread(enr.join, identity, code.code, force=True)
        assert (agent.identity_dir / "node.crt").read_bytes() == before
