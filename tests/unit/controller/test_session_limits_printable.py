"""V0.3 G6 (D22): agent-supplied Hello strings are validated at ingestion.

A non-printable character anywhere in the four Hello strings or inside
``Hello.hardware`` is an ``INVALID_ARGUMENT`` rejection with nothing persisted; the
reason names the field path and the first offending code point as ``U+XXXX`` and
never the value. Letters of every script stay legal.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

import grpc
import pytest

from meshforge import __version__
from meshforge.common.ids import new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig
from meshforge.controller.registry import Registry
from meshforge.controller.session.limits import (
    check_hardware,
    check_hello_strings,
    first_non_printable,
)
from meshforge.controller.session.manager import Rejected, SessionManager

pytestmark = pytest.mark.unit

FIELDS = ("hostname", "os", "platform", "agent_version")
BAD = {
    "ESC": "\x1b",
    "BEL": "\x07",
    "NUL": "\x00",
    "DEL": "\x7f",
    "C1 CSI": "\x9b",
    "bidi RLO": "\u202e",
    "TAB": "\t",
    "LF": "\n",
    "zero-width space": "\u200b",
}
MARKER = "EVILVALUE"


def strings(**override: str) -> dict[str, str]:
    base = {
        "hostname": "lab-laptop-1",
        "os": "Linux 6.8.0",
        "platform": "x86_64",
        "agent_version": __version__,
    }
    base.update(override)
    return base


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("name", list(BAD))
def test_every_hello_string_rejects_non_printable(field: str, name: str) -> None:
    char = BAD[name]
    value = f"ab{char}[31m{MARKER}"
    reason = check_hello_strings(**strings(**{field: value}))
    assert reason is not None
    assert f"Hello.{field}" in reason
    assert f"U+{ord(char):04X}" in reason
    assert "at index 2" in reason
    assert MARKER not in reason and char not in reason  # never the value


@pytest.mark.parametrize(
    "value",
    [
        "Łódź-laptop",
        "\u30ce\u30fc\u30c8PC",  # Japanese "notebook PC" (katakana)
        "Ünïcødé ñame",
        "a b c",
        "x" * 64,
        "",
    ],
)
def test_unicode_letters_and_spaces_are_accepted(value: str) -> None:
    assert check_hello_strings(**strings(hostname=value)) is None
    assert check_hardware(agent_pb2.HardwareSummary(cpu_model=value)) is None


def test_length_is_checked_before_printability() -> None:
    reason = check_hello_strings(**strings(hostname="\x1b" * 300))
    assert reason is not None and "300 characters" in reason


def test_first_non_printable_reports_the_first_only() -> None:
    assert first_non_printable("ok\u202e\x1b") == (2, "U+202E")
    assert first_non_printable("fine") is None
    assert first_non_printable("\U0001f600") is None  # emoji is printable
    assert first_non_printable("\U000e0001") == (0, "U+E0001")  # tag char (Cf)


@pytest.mark.parametrize(
    ("summary", "path"),
    [
        (
            agent_pb2.HardwareSummary(cpu_model="Intel\x00i7"),
            "Hello.hardware.cpu_model",
        ),
        (
            agent_pb2.HardwareSummary(
                gpus=[
                    agent_pb2.HardwareSummary.Gpu(model="RTX 3050"),
                    agent_pb2.HardwareSummary.Gpu(model="RTX\x1b[2J 2050"),
                ]
            ),
            "Hello.hardware.gpus[1].model",
        ),
        (
            agent_pb2.HardwareSummary(
                gpus=[agent_pb2.HardwareSummary.Gpu(driver_version="560\x9b1")]
            ),
            "Hello.hardware.gpus[0].driver_version",
        ),
        (
            agent_pb2.HardwareSummary(
                gpus=[agent_pb2.HardwareSummary.Gpu(compute_capability="8.\u202e6")]
            ),
            "Hello.hardware.gpus[0].compute_capability",
        ),
        (
            agent_pb2.HardwareSummary(capabilities=["v0.3", "cuda", "dock\x7fer"]),
            "Hello.hardware.capabilities[2]",
        ),
    ],
)
def test_every_string_inside_hardware_is_checked_with_its_path(
    summary: agent_pb2.HardwareSummary, path: str
) -> None:
    reason = check_hardware(summary)
    assert reason is not None
    assert reason.startswith(f"{path} contains the non-printable character U+")


def test_hardware_caps_are_checked_before_printability() -> None:
    reason = check_hardware(agent_pb2.HardwareSummary(cpu_model="\x1b" * 500))
    assert reason is not None and "500 characters" in reason


# ------------------------------------------------------------- at admission


class _NoWrites:
    """Any registry call is a test failure: nothing may be persisted."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"registry.{name} called for a rejected Hello")


def _hello(**override: Any) -> agent_pb2.Hello:
    fields: dict[str, Any] = {
        "node_id": new_node_id(),
        "protocol_version": 1,
        "agent_version": __version__,
        "hostname": "laptop",
        "session_epoch": 1,
        "os": "Ubuntu 24.04",
        "platform": "linux-x86_64",
    }
    fields.update(override)
    return agent_pb2.Hello(**fields)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hello",
    [
        _hello(hostname="lap\x1b]0;pwned\x07top"),
        _hello(
            hardware=agent_pb2.HardwareSummary(
                gpus=[agent_pb2.HardwareSummary.Gpu(model="\u202eRTX")]
            )
        ),
    ],
)
async def test_admission_rejects_invalid_argument_and_persists_nothing(
    hello: agent_pb2.Hello,
) -> None:
    manager = SessionManager(
        cast(Registry, _NoWrites()),
        LivenessConfig(),
        clock=lambda: 0.0,
        wall_clock=lambda: dt.datetime(2026, 9, 24, tzinfo=dt.UTC),
    )
    result = await manager.admit(hello, "ipv4:127.0.0.1:1")
    assert isinstance(result, Rejected)
    assert result.abort_status is grpc.StatusCode.INVALID_ARGUMENT
    assert "non-printable character U+" in result.reason
    assert "pwned" not in result.reason
    assert len(manager) == 0
