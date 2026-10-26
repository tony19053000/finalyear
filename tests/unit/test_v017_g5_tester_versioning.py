"""V0.17 G5 tester (brief §5e item 4, D393/Q37): agent <-> controller version
compatibility, derived from the acceptance text, not from the implementation.

Acceptance: "too-old and too-new agents get ``version_unsupported`` with no
restart loop"; same and previous minor are accepted; an unreadable version is
refused; the refusal happens before anything is stored.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any, Final, cast

import pytest

from meshforge.common.versioning import (
    VERSION_UNSUPPORTED,
    Compatibility,
    MinorVersion,
    check_agent_version,
    parse_minor,
    reason_is_version_unsupported,
)

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]


# ------------------------------------------------- acceptance, by criterion
@pytest.mark.parametrize(
    ("controller", "agent", "want"),
    [
        # same minor, any patch / pre-release / dev suffix
        ("0.17.0", "0.17.0", Compatibility.SAME),
        ("0.17.0", "0.17.9", Compatibility.SAME),
        ("0.17.3", "0.17.0.dev5", Compatibility.SAME),
        ("0.17.0", "0.17.0rc1", Compatibility.SAME),
        ("0.17.0.dev1", "0.17.0", Compatibility.SAME),
        # previous minor is accepted but flagged behind
        ("0.17.0", "0.16.0", Compatibility.BEHIND),
        ("0.17.2", "0.16.99", Compatibility.BEHIND),
        # two minors back is refused
        ("0.17.0", "0.15.9", Compatibility.TOO_OLD),
        ("0.17.0", "0.0.1", Compatibility.TOO_OLD),
        # newer minor / major is refused
        ("0.17.0", "0.18.0", Compatibility.TOO_NEW),
        ("0.17.0", "1.0.0", Compatibility.TOO_NEW),
        ("0.17.9", "0.18.0.dev0", Compatibility.TOO_NEW),
        # major boundaries: X.0 accepts any (X-1).*
        ("1.0.0", "0.99.0", Compatibility.BEHIND),
        ("1.0.0", "0.0.0", Compatibility.BEHIND),
        ("1.0.0", "1.0.5", Compatibility.SAME),
        ("2.0.0", "0.99.0", Compatibility.TOO_OLD),
        ("2.0.0", "1.7.0", Compatibility.BEHIND),
        ("1.3.0", "0.99.0", Compatibility.TOO_OLD),
        # the very first minor
        ("0.0.1", "0.0.0", Compatibility.SAME),
        ("0.0.1", "0.1.0", Compatibility.TOO_NEW),
    ],
)
def test_verdict_matrix(controller: str, agent: str, want: Compatibility) -> None:
    got = check_agent_version(agent, controller)
    assert got.compatibility is want
    assert got.accepted is (want in (Compatibility.SAME, Compatibility.BEHIND))


@pytest.mark.parametrize(
    "agent",
    [
        *("", "   ", "test", "dev", "x.y.z", "0", "17", "0.", ".17", "a0.17", "-1.2"),
        *("0,17", "0-17", "0.1 7", "0.17 beta", "0.17;DROP", "0.17\x00"),
        *("1_0.2", "1e1.2", "0x10.1", "+0.17", "0.-17", "0.17/../x"),
    ],
)
def test_unreadable_versions_are_refused_never_guessed(agent: str) -> None:
    verdict = check_agent_version(agent, "0.17.0")
    assert verdict.compatibility is Compatibility.UNKNOWN
    assert not verdict.accepted
    assert verdict.reason("n").startswith(f"{VERSION_UNSUPPORTED}: ")


@pytest.mark.xfail(
    strict=True,
    reason="G5-T1 (minor): parse_minor's regex uses \\d, which accepts non-ASCII "
    "digits, and a trailing dot ('0.17.'); the brief's rule is '<int>.<int>'",
)
@pytest.mark.parametrize("agent", ["0.17.", "\u0660.\u0661\u0667"])
def test_defect_g5_t1_version_text_is_read_as_ascii_digits_only(agent: str) -> None:
    assert check_agent_version(agent, "0.17.0").compatibility is Compatibility.UNKNOWN


def test_absurdly_large_numbers_cannot_overflow_into_acceptance() -> None:
    for text in ("99999999999999999999.0", "0.99999999999999999999", "0000000.1"):
        verdict = check_agent_version(text, "0.17.0")
        assert not verdict.accepted, text


def test_every_refusal_carries_the_reason_word_and_names_both_versions() -> None:
    for agent in ("0.1.0", "0.40.0", "banana"):
        reason = check_agent_version(agent, "0.17.2").reason("laptop-7")
        assert reason_is_version_unsupported(reason)
        assert "0.17.2" in reason
        assert agent in reason
    assert "laptop-7" in check_agent_version("0.1.0", "0.17.2").reason("laptop-7")
    # a reason for an accepted agent must never be mistaken for a refusal path
    assert check_agent_version("0.17.0", "0.17.2").accepted


def test_reason_is_version_unsupported_is_strict() -> None:
    assert reason_is_version_unsupported("version_unsupported: x")
    assert reason_is_version_unsupported("  version_unsupported : x")
    assert not reason_is_version_unsupported("protocol_version 9 not in [1, 1]")
    assert not reason_is_version_unsupported("")
    assert not reason_is_version_unsupported("my version_unsupported: x")
    assert not reason_is_version_unsupported("version_unsupported_not: x")


def test_the_verdict_is_monotone_in_the_agent_minor() -> None:
    """Sweep: as the agent's minor rises, the verdict walks
    TOO_OLD* -> BEHIND? -> SAME -> TOO_NEW* and never goes back."""
    order = {
        Compatibility.TOO_OLD: 0,
        Compatibility.BEHIND: 1,
        Compatibility.SAME: 2,
        Compatibility.TOO_NEW: 3,
    }
    for controller_major, controller_minor in itertools.product((0, 1, 4), (0, 1, 17)):
        controller = f"{controller_major}.{controller_minor}.0"
        ranks = [
            order[check_agent_version(f"{major}.{minor}.0", controller).compatibility]
            for major in range(controller_major + 2)
            for minor in range(20)
        ]
        assert ranks == sorted(ranks), (controller, ranks)
        assert 2 in ranks  # SAME exists for every controller


def test_exactly_one_minor_is_same_and_at_most_one_is_behind_per_major() -> None:
    controller = "0.17.0"
    same = [
        m
        for m in range(40)
        if check_agent_version(f"0.{m}.0", controller).compatibility
        is Compatibility.SAME
    ]
    behind = [
        m
        for m in range(40)
        if check_agent_version(f"0.{m}.0", controller).compatibility
        is Compatibility.BEHIND
    ]
    assert same == [17]
    assert behind == [16]


@pytest.mark.parametrize(
    ("floor", "agent", "want"),
    [
        ("0.17", "0.16.9", Compatibility.TOO_OLD),  # raised floor: no previous minor
        ("0.17", "0.17.0", Compatibility.SAME),
        ("0.10", "0.10.0", Compatibility.BEHIND),  # lowered floor: older accepted
        ("0.10", "0.9.0", Compatibility.TOO_OLD),
        ("0.10", "0.18.0", Compatibility.TOO_NEW),  # a floor never admits newer
    ],
)
def test_min_agent_version_moves_only_the_floor(
    floor: str, agent: str, want: Compatibility
) -> None:
    assert (
        check_agent_version(agent, "0.17.0", min_agent_version=floor).compatibility
        is want
    )


def test_a_bad_floor_or_controller_version_is_a_loud_error() -> None:
    with pytest.raises(ValueError):
        check_agent_version("0.17.0", "0.17.0", min_agent_version="seventeen")
    with pytest.raises(ValueError):
        check_agent_version("0.17.0", "weird")


def test_parse_minor_ignores_everything_after_the_minor() -> None:
    assert parse_minor("0.17.0.dev3") == MinorVersion(0, 17)
    assert parse_minor("v1.2") == MinorVersion(1, 2)
    assert parse_minor("0.17\n") == MinorVersion(0, 17)


# ------------------------------------------ the shipped version still works
def test_this_checkouts_own_version_is_accepted_by_its_own_controller() -> None:
    """The hazard recorded in the G5 note: a version bump must not make a
    controller refuse its own agent."""
    from meshforge import __version__

    assert (
        check_agent_version(__version__, __version__).compatibility
        is Compatibility.SAME
    )
    assert parse_minor(__version__) is not None


def test_pyproject_version_is_what_the_package_reports() -> None:
    import tomllib

    from meshforge import __version__

    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert parse_minor(data["project"]["version"]) == parse_minor(__version__)


# ------------------------------------------ admission: nothing stored, no loop
def _hello(agent_version: str, **overrides: Any) -> Any:
    from meshforge.common.ids import new_node_id
    from meshforge.common.proto.meshforge.v1 import agent_pb2

    return agent_pb2.Hello(
        node_id=new_node_id(),
        protocol_version=1,
        agent_version=agent_version,
        hostname="laptop",
        display_name=overrides.pop("display_name", "asha-laptop"),
        session_epoch=1,
        os="Ubuntu 24.04",
        platform="linux-x86_64",
        hardware=agent_pb2.HardwareSummary(cpu_model="cpu", cpu_cores=4),
    )


class _TouchesNothing:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.calls.append(name)
        raise AssertionError(f"registry.{name} must not be called for a refusal")


def _manager(controller_version: str, floor: str = "") -> tuple[Any, _TouchesNothing]:
    import datetime as dt

    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.liveness import LivenessConfig
    from meshforge.controller.registry import Registry
    from meshforge.controller.session.manager import SessionManager

    registry = _TouchesNothing()
    manager = SessionManager(
        cast(Registry, registry),
        LivenessConfig(heartbeat_interval_s=1.0, lease_multiplier=3),
        clock=lambda: 1000.0,
        wall_clock=lambda: dt.datetime(2026, 10, 26, tzinfo=dt.UTC),
        controller_version=controller_version,
        min_agent_version=floor,
    )
    return manager, registry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_version",
    ["0.15.0", "0.18.0", "2.0.0", "", "  ", "not-a-version", "0", "v", "\u0660.\u0661"],
)
async def test_refused_hello_is_a_helloack_reject_with_no_side_effects(
    agent_version: str,
) -> None:
    from meshforge.controller.session.admission import RejectCode
    from meshforge.controller.session.manager import Rejected

    manager, registry = _manager("0.17.1")
    result = await manager.admit(_hello(agent_version), "ipv4:10.0.0.9:1")
    assert isinstance(result, Rejected)
    # PROTOCOL_VERSION is the one code every agent since V0.3 stops on
    assert result.reject_code == RejectCode.REJECT_CODE_PROTOCOL_VERSION
    assert result.abort_status is None
    assert registry.calls == []
    assert len(manager) == 0


@pytest.mark.asyncio
async def test_a_refusal_without_a_display_name_names_the_node_id() -> None:
    from meshforge.controller.session.manager import Rejected

    manager, _ = _manager("0.17.1")
    hello = _hello("0.1.0")
    hello.display_name = ""
    result = await manager.admit(hello, "p")
    assert isinstance(result, Rejected)
    assert hello.node_id in result.reason


@pytest.mark.asyncio
async def test_a_huge_agent_version_is_cut_off_before_the_version_rule() -> None:
    """The refusal text echoes the agent's version; the Hello limits must stop a
    multi-megabyte string before it is echoed, logged or stored."""
    from meshforge.controller.session.manager import Rejected

    manager, registry = _manager("0.17.1")
    result = await manager.admit(_hello("0.1." + "a" * 2_000_000), "p")
    assert isinstance(result, Rejected)
    assert len(result.reason) < 4096
    assert registry.calls == []


@pytest.mark.asyncio
async def test_repeating_a_refused_hello_is_stable() -> None:
    """Idempotent: ten identical refused Hellos -> ten identical refusals, no
    live session, no registry call."""
    from meshforge.controller.session.manager import Rejected

    manager, registry = _manager("0.17.1")
    hello = _hello("0.1.0")
    reasons = set()
    for _ in range(10):
        result = await manager.admit(hello, "p")
        assert isinstance(result, Rejected)
        reasons.add(result.reason)
    assert len(reasons) == 1
    assert registry.calls == [] and len(manager) == 0


# ------------------------------------- "no restart loop" under the service
def test_the_exit_code_of_a_version_stop_is_one_the_units_do_not_restart() -> None:
    """Linux: ``RestartPreventExitStatus`` must contain the exit code a
    VersionUnsupportedError ends the agent with; Windows: the service-exit codes
    the installer treats as 'do not retry' must too."""
    import re

    from meshforge.agent.session.errors import VersionUnsupportedError

    exit_code = int(
        VersionUnsupportedError(
            "n", check_agent_version("0.1.0", "0.17.0").reason()
        ).exit_code
    )
    unit = (REPO / "packaging/linux/meshforge-agent.service").read_text(
        encoding="utf-8"
    )
    match = re.search(r"^RestartPreventExitStatus=([\d ]+)$", unit, re.MULTILINE)
    assert match, "the agent unit must say which exit codes are final"
    assert exit_code in {int(x) for x in match.group(1).split()}
    from meshforge.agent.winsetup import NO_RESTART_SERVICE_EXIT_CODES

    assert exit_code in NO_RESTART_SERVICE_EXIT_CODES
