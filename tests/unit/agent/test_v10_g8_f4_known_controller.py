"""V1.0 G8 F4: the last-known controller endpoint and the mDNS fallback.

Lab L4 (Windows 11): a joined agent with an empty ``controller_address`` looped
DISCOVERING -> BACKOFF forever because mDNS answers never arrived. These tests
pin the fix's pieces without a network: the hint file (``known_controller``),
the resolver order (configured > mDNS > last-known) and the join-time write.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshforge.agent import known_controller
from meshforge.agent.discovery import (
    LastKnownFallbackResolver,
    MdnsResolver,
    StaticResolver,
    resolver_for,
)
from meshforge.agent.enrollment import _remember_session_endpoint
from meshforge.agent.identity import CONTROLLER_FILE_NAME, load_or_create, reset
from meshforge.agent.settings import DiscoverySettings

pytestmark = pytest.mark.unit


class _Fixed:
    """A resolver that answers ``value`` and counts its calls."""

    def __init__(self, value: str | None) -> None:
        self.value = value
        self.calls = 0

    async def resolve(self) -> str | None:
        self.calls += 1
        return self.value


@pytest.fixture
def ident_dir(tmp_path: Path) -> Path:
    return load_or_create(tmp_path / "identity").path


# ------------------------------------------------------------------ the file


def test_missing_file_is_none(ident_dir: Path) -> None:
    assert known_controller.load_known(ident_dir) is None


def test_remember_then_load_round_trips(ident_dir: Path) -> None:
    assert known_controller.remember(ident_dir, "192.168.31.13:47100", source="join")
    known = known_controller.load_known(ident_dir)
    assert known is not None
    assert known.address == "192.168.31.13:47100"
    assert known.source == "join"
    body = json.loads((ident_dir / CONTROLLER_FILE_NAME).read_text())
    assert body["format"] == known_controller.KNOWN_CONTROLLER_FORMAT
    assert set(body) == {"format", "address", "source", "updated_at"}


def test_the_same_address_is_not_rewritten(ident_dir: Path) -> None:
    assert known_controller.remember(ident_dir, "10.0.0.5:47100", source="join")
    before = (ident_dir / CONTROLLER_FILE_NAME).read_bytes()
    assert known_controller.remember(ident_dir, "10.0.0.5:47100", source="session")
    assert (ident_dir / CONTROLLER_FILE_NAME).read_bytes() == before


def test_a_new_address_replaces_the_old(ident_dir: Path) -> None:
    known_controller.remember(ident_dir, "10.0.0.5:47100", source="join")
    known_controller.remember(ident_dir, "10.0.0.9:47100", source="session")
    known = known_controller.load_known(ident_dir)
    assert known is not None
    assert (known.address, known.source) == ("10.0.0.9:47100", "session")
    # Atomic write, nothing left over beside the identity.
    assert not list(ident_dir.glob(".*.tmp"))


def test_ipv6_target_round_trips(ident_dir: Path) -> None:
    assert known_controller.remember(ident_dir, "[fd00::5]:47100", source="join")
    known = known_controller.load_known(ident_dir)
    assert known is not None and known.address == "[fd00::5]:47100"


@pytest.mark.parametrize("address", ["", "no-port", "10.0.0.5:0", "10.0.0.5:99999"])
def test_a_bad_address_is_never_stored(ident_dir: Path, address: str) -> None:
    assert not known_controller.remember(ident_dir, address, source="session")
    assert not (ident_dir / CONTROLLER_FILE_NAME).exists()


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"\xff\xfe",
        b"[]",
        b'{"format": 2, "address": "10.0.0.5:47100"}',
        b'{"format": 1}',
        b'{"format": 1, "address": 7}',
        b'{"format": 1, "address": "10.0.0.5"}',
        b'{"format": 1, "address": "' + b"a" * 5000 + b':1"}',
    ],
)
def test_a_damaged_file_is_ignored_not_fatal(ident_dir: Path, content: bytes) -> None:
    (ident_dir / CONTROLLER_FILE_NAME).write_bytes(content)
    assert known_controller.load_known(ident_dir) is None
    # ...and the next good write repairs it.
    assert known_controller.remember(ident_dir, "10.0.0.5:47100", source="session")
    assert known_controller.load_known(ident_dir) is not None


def test_forget_deletes_and_tolerates_missing(ident_dir: Path) -> None:
    known_controller.forget(ident_dir)  # nothing there: fine
    known_controller.remember(ident_dir, "10.0.0.5:47100", source="join")
    known_controller.forget(ident_dir)
    assert known_controller.load_known(ident_dir) is None


def test_identity_reset_deletes_the_hint(ident_dir: Path) -> None:
    """Leave / reset: the hint belongs to the enrolment and goes with it."""
    known_controller.remember(ident_dir, "10.0.0.5:47100", source="join")
    reset(ident_dir)
    assert not (ident_dir / CONTROLLER_FILE_NAME).exists()


def test_an_unwritable_directory_is_logged_not_raised(tmp_path: Path) -> None:
    missing = tmp_path / "gone"  # no directory: the lock cannot be opened
    assert not known_controller.remember(missing, "10.0.0.5:47100", source="join")
    known_controller.forget(missing)


# ------------------------------------------------------------- join-time write


def test_join_stores_host_with_the_session_port(ident_dir: Path) -> None:
    identity = load_or_create(ident_dir)
    assert _remember_session_endpoint(identity, "192.168.31.13", 47100) == (
        "192.168.31.13:47100"
    )
    known = known_controller.load_known(ident_dir)
    assert known is not None and known.source == "join"


def test_join_brackets_an_ipv6_host(ident_dir: Path) -> None:
    identity = load_or_create(ident_dir)
    assert _remember_session_endpoint(identity, "fd00::5", 47100) == "[fd00::5]:47100"


def test_join_without_a_session_port_clears_an_old_fabrics_hint(
    ident_dir: Path,
) -> None:
    """An older controller sends 0: no hint, and a previous fabric's is gone."""
    identity = load_or_create(ident_dir)
    known_controller.remember(ident_dir, "10.9.9.9:47100", source="session")
    assert _remember_session_endpoint(identity, "192.168.31.13", 0) is None
    assert known_controller.load_known(ident_dir) is None


# --------------------------------------------------------------- the resolver


def test_resolver_for_wraps_mdns_only_when_given_an_identity_dir(
    ident_dir: Path,
) -> None:
    plain = resolver_for("", DiscoverySettings(), node_id="n")
    assert isinstance(plain, MdnsResolver)
    wrapped = resolver_for("", DiscoverySettings(), node_id="n", identity_dir=ident_dir)
    assert isinstance(wrapped, LastKnownFallbackResolver)
    # A configured address always wins: no mDNS, no hint.
    static = resolver_for(
        "10.0.0.5:47100", DiscoverySettings(), node_id="n", identity_dir=ident_dir
    )
    assert isinstance(static, StaticResolver)


@pytest.mark.asyncio
async def test_mdns_finds_nothing_uses_the_last_known_endpoint(
    ident_dir: Path,
) -> None:
    known_controller.remember(ident_dir, "192.168.31.13:47100", source="join")
    mdns = _Fixed(None)
    resolver = LastKnownFallbackResolver(mdns, ident_dir, node_id="n")
    assert await resolver.resolve() == "192.168.31.13:47100"
    assert mdns.calls == 1  # mDNS is always asked first


@pytest.mark.asyncio
async def test_mdns_answer_wins_over_a_stale_hint(ident_dir: Path) -> None:
    """DHCP moved the controller: mDNS has the new address, the hint the old."""
    known_controller.remember(ident_dir, "192.168.31.13:47100", source="join")
    resolver = LastKnownFallbackResolver(
        _Fixed("192.168.31.40:47100"), ident_dir, node_id="n"
    )
    assert await resolver.resolve() == "192.168.31.40:47100"


@pytest.mark.asyncio
async def test_nothing_anywhere_is_none(ident_dir: Path) -> None:
    resolver = LastKnownFallbackResolver(_Fixed(None), ident_dir, node_id="n")
    assert await resolver.resolve() is None


@pytest.mark.asyncio
async def test_the_hint_is_read_fresh_every_cycle(ident_dir: Path) -> None:
    """A join in the same process (console) is seen by the next cycle."""
    resolver = LastKnownFallbackResolver(_Fixed(None), ident_dir, node_id="n")
    assert await resolver.resolve() is None
    known_controller.remember(ident_dir, "10.0.0.5:47100", source="join")
    assert await resolver.resolve() == "10.0.0.5:47100"


def test_a_controller_address_at_the_join_port_logs_a_hint() -> None:
    """47101 is the port in the join code; agents connect to 47100."""
    import structlog

    with structlog.testing.capture_logs() as logs:
        resolver_for("192.168.31.13:47101", DiscoverySettings(), node_id="n")
        resolver_for("192.168.31.13:47100", DiscoverySettings(), node_id="n")
    hints = [e for e in logs if e["event"] == "agent.discovery.enroll_port_configured"]
    assert len(hints) == 1
    assert hints[0]["controller_address"] == "192.168.31.13:47101"
    assert "47100" in hints[0]["hint"] and "join" in hints[0]["hint"]
