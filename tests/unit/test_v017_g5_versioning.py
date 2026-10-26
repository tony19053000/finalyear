"""V0.17 G5 (D393 second half, Q37): agent <-> controller version compatibility.

The pure rule (``common/versioning.py``), the controller setting, the Hello
admission refusal (PROTOCOL_VERSION + ``version_unsupported:`` reason, nothing
registered), what the agent makes of it (a typed stop, no retry) and the Nodes
page's version status.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

import pytest

from meshforge.common.versioning import (
    VERSION_UNSUPPORTED,
    Compatibility,
    MinorVersion,
    check_agent_version,
    default_min_agent,
    parse_minor,
    reason_is_version_unsupported,
)

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ the rule
@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("0.17.2", MinorVersion(0, 17)),
        ("0.17", MinorVersion(0, 17)),
        ("0.17.0.dev3", MinorVersion(0, 17)),
        ("0.17.0rc1", MinorVersion(0, 17)),
        ("1.0.0+local", MinorVersion(1, 0)),
        ("v0.16.1", MinorVersion(0, 16)),
        (" 0.1.0.dev0 ", MinorVersion(0, 1)),
        ("x", None),
        ("", None),
        ("0", None),
        ("17", None),
        ("0.x", None),
        ("0.17 beta", None),
    ],
)
def test_parse_minor(text: str, want: MinorVersion | None) -> None:
    assert parse_minor(text) == want


def test_default_floor_is_the_previous_minor() -> None:
    assert default_min_agent(MinorVersion(0, 17)) == MinorVersion(0, 16)
    assert default_min_agent(MinorVersion(1, 0)) == MinorVersion(0, 0)
    assert default_min_agent(MinorVersion(0, 0)) == MinorVersion(0, 0)


@pytest.mark.parametrize(
    ("agent", "controller", "floor", "want"),
    [
        ("0.17.0", "0.17.3", "", Compatibility.SAME),
        ("0.17.9.dev1", "0.17.0", "", Compatibility.SAME),
        ("0.16.4", "0.17.0", "", Compatibility.BEHIND),
        ("0.15.9", "0.17.0", "", Compatibility.TOO_OLD),
        ("0.18.0", "0.17.5", "", Compatibility.TOO_NEW),
        ("1.0.0", "0.17.5", "", Compatibility.TOO_NEW),
        ("0.9.0", "1.0.0", "", Compatibility.BEHIND),  # X.0 accepts (X-1).*
        ("test", "0.17.0", "", Compatibility.UNKNOWN),
        ("0.16.0", "0.17.0", "0.17", Compatibility.TOO_OLD),  # floor raised
        ("0.15.0", "0.17.0", "0.15", Compatibility.BEHIND),  # floor lowered
    ],
)
def test_verdicts(agent: str, controller: str, floor: str, want: Compatibility) -> None:
    verdict = check_agent_version(agent, controller, min_agent_version=floor)
    assert verdict.compatibility is want
    assert verdict.accepted is (want in (Compatibility.SAME, Compatibility.BEHIND))


def test_refusal_texts_name_both_versions_and_the_fix() -> None:
    old = check_agent_version("0.15.2", "0.17.0").reason("asha-laptop")
    assert old.startswith(f"{VERSION_UNSUPPORTED}: ")
    assert "0.15.2" in old and "0.17.0" in old and "0.16" in old
    assert "upgrade the MeshForge agent on asha-laptop" in old
    new = check_agent_version("0.18.0", "0.17.0").reason("x")
    assert "upgrade the controller first" in new
    unknown = check_agent_version("weird", "0.17.0").reason()
    assert "agent_version" in unknown and "'weird'" in unknown
    for text in (old, new, unknown):
        assert reason_is_version_unsupported(text)
    assert not reason_is_version_unsupported("protocol_version 2 not in [1, 1]")
    assert not reason_is_version_unsupported("version_unsupported_x: y")


def test_bad_inputs_are_refused_loudly() -> None:
    with pytest.raises(ValueError, match="controller version"):
        check_agent_version("0.17.0", "dev")
    with pytest.raises(ValueError, match="min_agent_version"):
        check_agent_version("0.17.0", "0.17.0", min_agent_version="old")


# ------------------------------------------------------------ the setting
def test_min_agent_version_setting() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.settings import ControllerSection

    assert ControllerSection().min_agent_version == ""
    assert ControllerSection(min_agent_version=" 0.0.7 ").min_agent_version == "0.0"
    with pytest.raises(ValueError, match=r"not <major>\.<minor>"):
        ControllerSection(min_agent_version="sixteen")
    with pytest.raises(ValueError, match="no agent could connect"):
        ControllerSection(min_agent_version="99.0")


# ------------------------------------------------------- Hello admission
def _hello(agent_version: str) -> Any:
    from meshforge.common.ids import new_node_id
    from meshforge.common.proto.meshforge.v1 import agent_pb2

    return agent_pb2.Hello(
        node_id=new_node_id(),
        protocol_version=1,
        agent_version=agent_version,
        hostname="laptop",
        display_name="asha-laptop",
        session_epoch=1,
        os="Ubuntu 24.04",
        platform="linux-x86_64",
        hardware=agent_pb2.HardwareSummary(cpu_model="cpu", cpu_cores=4),
    )


class _NoRegistry:
    """Any registry call is a failure: a refused Hello must not touch the DB."""

    calls: list[str]

    def __init__(self) -> None:
        self.calls = []

    def __getattr__(self, name: str) -> Any:
        self.calls.append(name)
        raise AssertionError(f"registry.{name} called for a refused Hello")


def _manager(controller_version: str, floor: str = "") -> Any:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.liveness import LivenessConfig
    from meshforge.controller.registry import Registry
    from meshforge.controller.session.manager import SessionManager

    return SessionManager(
        cast(Registry, _NoRegistry()),
        LivenessConfig(heartbeat_interval_s=1.0, lease_multiplier=3),
        clock=lambda: 1000.0,
        wall_clock=lambda: dt.datetime(2026, 10, 26, tzinfo=dt.UTC),
        controller_version=controller_version,
        min_agent_version=floor,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "fix"),
    [
        ("0.15.0", "upgrade the MeshForge agent on asha-laptop"),
        ("0.18.0", "upgrade the controller first"),
        ("not-a-version", "agent_version"),
    ],
)
async def test_unsupported_agents_are_refused_before_anything_is_stored(
    agent: str, fix: str
) -> None:
    from meshforge.controller.session.admission import RejectCode
    from meshforge.controller.session.manager import Rejected

    manager = _manager("0.17.1")
    result = await manager.admit(_hello(agent), "ipv4:127.0.0.1:1")
    assert isinstance(result, Rejected)
    assert result.reject_code == RejectCode.REJECT_CODE_PROTOCOL_VERSION
    assert result.abort_status is None  # a HelloAck, not a stream abort
    assert result.reason.startswith("version_unsupported: ")
    assert fix in result.reason
    assert len(manager) == 0


@pytest.mark.asyncio
async def test_a_raised_floor_refuses_the_previous_minor() -> None:
    from meshforge.controller.session.manager import Rejected

    manager = _manager("0.17.1", floor="0.17")
    result = await manager.admit(_hello("0.16.9"), "p")
    assert isinstance(result, Rejected)
    assert "accepts agents 0.17 or newer" in result.reason


def test_a_bad_controller_version_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="controller version"):
        _manager("dev")


# ------------------------------------------------------- the agent's side
def test_the_agent_stops_with_a_typed_reason_and_never_retries() -> None:
    pytest.importorskip("grpc")
    from meshforge.agent.console.supervisor import _reason_for
    from meshforge.agent.session.errors import (
        AgentContractError,
        VersionUnsupportedError,
    )
    from meshforge.agent.session.policy import HelloRejectAction, hello_reject_action
    from meshforge.common.errors import ExitCode
    from meshforge.common.proto.meshforge.v1 import agent_pb2

    code = agent_pb2.HelloAck.RejectCode.REJECT_CODE_PROTOCOL_VERSION
    # Every agent since V0.3 stops on this code (old ones included): no loop.
    assert hello_reject_action(code, 0) is HelloRejectAction.FATAL_CONFIG
    reason = check_agent_version("0.15.0", "0.17.0").reason("me")
    error = VersionUnsupportedError("node_x", reason)
    assert isinstance(error, AgentContractError)
    assert error.exit_code == ExitCode.CONFIG_ERROR  # not restarted (exit 4)
    assert error.message == reason
    assert error.details["reason"] == VERSION_UNSUPPORTED
    stop = _reason_for(error)
    assert stop.code == VERSION_UNSUPPORTED
    assert stop.message == reason


# ---------------------------------------------------------- Nodes page
def test_nodes_page_version_status() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge import __version__
    from meshforge.controller.api.shape import version_status

    own = parse_minor(__version__)
    assert own is not None
    assert version_status(None) is None
    assert version_status("") is None
    assert version_status(__version__) == "same"
    assert version_status(f"{own.major}.{own.minor + 1}.0") == "unsupported"
    if own.minor > 0:
        assert version_status(f"{own.major}.{own.minor - 1}.5") == "behind"
    assert version_status("junk") == "unsupported"
