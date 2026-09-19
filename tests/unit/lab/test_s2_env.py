"""Unit tests for the S2 environment probes (roadmap 0.2.6).

WSL detection is the load-bearing part of spike S2: a result produced inside
WSL2 would answer a different question than ADR-0009 asks. These tests feed it
recorded ``/proc/version`` text and environment markers, so they run the same
on Linux and on Windows CI.
"""

from __future__ import annotations

import sys

import pytest

from spikes._s2 import env

_NATIVE_PROC = (
    "Linux version 6.8.0-139-generic (buildd@lcy02-amd64-001) "
    "(x86_64-linux-gnu-gcc-13 (Ubuntu 13.2.0)) #139-Ubuntu SMP"
)
_WSL2_PROC = (
    "Linux version 5.15.153.1-microsoft-standard-WSL2 "
    "(root@941d701f84f1) (gcc (GCC) 11.2.0) #1 SMP"
)
_WSL1_PROC = "Linux version 4.4.0-19041-Microsoft (Microsoft@Microsoft.com)"


def test_native_linux_is_native() -> None:
    status = env.wsl_status(
        proc_version=_NATIVE_PROC,
        osrelease="6.8.0-139-generic",
        env={},
        platform_name="linux",
    )
    assert status["is_wsl"] is False
    assert status["native"] is True
    assert status["wsl_version"] is None


@pytest.mark.parametrize(
    ("proc", "osrelease", "version"),
    [
        (_WSL2_PROC, "5.15.153.1-microsoft-standard-WSL2", 2),
        (_WSL1_PROC, "4.4.0-19041-Microsoft", 1),
    ],
)
def test_wsl_kernels_are_detected(proc: str, osrelease: str, version: int) -> None:
    status = env.wsl_status(
        proc_version=proc, osrelease=osrelease, env={}, platform_name="linux"
    )
    assert status["is_wsl"] is True
    assert status["native"] is False
    assert status["wsl_version"] == version


def test_env_markers_alone_prove_wsl() -> None:
    """A rebuilt kernel can hide the name; the interop socket cannot."""
    status = env.wsl_status(
        proc_version=_NATIVE_PROC,
        osrelease="6.8.0",
        env={"WSL_INTEROP": "/run/WSL/8_interop"},
        platform_name="linux",
    )
    assert status["is_wsl"] is True
    assert status["evidence"]["WSL_INTEROP"] is True


def test_distro_name_marks_wsl() -> None:
    status = env.wsl_status(
        proc_version=_NATIVE_PROC,
        osrelease="6.8.0",
        env={"WSL_DISTRO_NAME": "Ubuntu"},
        platform_name="linux",
    )
    assert status["is_wsl"] is True
    assert status["distro"] == "Ubuntu"


@pytest.mark.parametrize("platform_name", ["win32", "darwin"])
def test_non_linux_platforms_are_native(platform_name: str) -> None:
    """Windows itself is never "inside WSL" - WSL runs a Linux kernel."""
    status = env.wsl_status(
        proc_version=_WSL2_PROC, osrelease="x", env={}, platform_name=platform_name
    )
    assert status["is_wsl"] is False
    assert status["native"] is True


def test_evidence_is_truncated_not_dumped() -> None:
    status = env.wsl_status(
        proc_version="x" * 5000, osrelease="y" * 5000, env={}, platform_name="linux"
    )
    assert len(status["evidence"]["proc_version"]) <= 200
    assert len(status["evidence"]["osrelease"]) <= 200


def test_refuse_if_wsl_blocks_by_default() -> None:
    status = env.wsl_status(
        proc_version=_WSL2_PROC,
        osrelease="5.15-WSL2",
        env={"WSL_DISTRO_NAME": "Ubuntu"},
        platform_name="linux",
    )
    refusal = env.refuse_if_wsl(status, allow_wsl=False)
    assert refusal is not None
    assert "ADR-0009" in refusal
    assert "Ubuntu" in refusal
    assert env.refuse_if_wsl(status, allow_wsl=True) is None


def test_refuse_if_wsl_allows_native() -> None:
    status = env.wsl_status(
        proc_version=_NATIVE_PROC, osrelease="6.8.0", env={}, platform_name="linux"
    )
    assert env.refuse_if_wsl(status, allow_wsl=False) is None


def test_wsl_status_here_reads_a_fake_root(tmp_path: object) -> None:
    """The real machine's /proc is not needed: the root is a seam."""
    from pathlib import Path

    root = Path(str(tmp_path))
    (root / "proc" / "sys" / "kernel").mkdir(parents=True)
    (root / "proc" / "version").write_text(_WSL2_PROC, encoding="utf-8")
    (root / "proc" / "sys" / "kernel" / "osrelease").write_text(
        "5.15.153.1-microsoft-standard-WSL2", encoding="utf-8"
    )
    status = env.wsl_status_here(root)
    if sys.platform.startswith("linux"):
        assert status["is_wsl"] is True
    else:  # on Windows the answer does not depend on the files at all
        assert status["is_wsl"] is False


def test_read_text_never_raises(tmp_path: object) -> None:
    from pathlib import Path

    assert env.read_text(Path(str(tmp_path)) / "nope") == ""


# ------------------------------------------------------- firewall profiles
_PROFILE_JSON = (
    '[{"InterfaceAlias":"Ethernet","InterfaceIndex":12,"Name":"Network 3",'
    '"NetworkCategory":"Private","IPv4Connectivity":"Internet"},'
    '{"InterfaceAlias":"Wi-Fi","InterfaceIndex":7,"Name":"lab-wifi",'
    '"NetworkCategory":"Public","IPv4Connectivity":"LocalNetwork"}]'
)
_SINGLE_PROFILE_JSON = (
    '{"InterfaceAlias":"Ethernet","InterfaceIndex":12,"Name":"Network 3",'
    '"NetworkCategory":"Private","IPv4Connectivity":"Internet"}'
)


def test_parse_connection_profiles_list() -> None:
    rows = env.parse_connection_profiles(_PROFILE_JSON)
    assert [r["interface"] for r in rows] == ["Ethernet", "Wi-Fi"]
    assert rows[0]["category"] == "Private"


def test_parse_connection_profiles_single_object() -> None:
    """One connection: ConvertTo-Json emits an object, not a list."""
    rows = env.parse_connection_profiles(_SINGLE_PROFILE_JSON)
    assert len(rows) == 1
    assert rows[0]["interface_index"] == 12


def test_parse_connection_profiles_empty() -> None:
    assert env.parse_connection_profiles("") == []


def test_non_private_profiles_flags_public() -> None:
    firewall = {"profiles": env.parse_connection_profiles(_PROFILE_JSON)}
    assert env.non_private_profiles(firewall) == ["Wi-Fi"]


def test_non_private_profiles_accepts_domain() -> None:
    rows = [{"interface": "Ethernet", "category": "DomainAuthenticated"}]
    assert env.non_private_profiles({"profiles": rows}) == []


def test_firewall_profile_is_not_applicable_off_windows() -> None:
    result = env.firewall_profile()
    assert set(result) >= {"source", "supported", "profiles"}
    if not env.IS_WINDOWS:
        assert result["supported"] is False


def test_os_build_has_the_fields_the_report_needs() -> None:
    facts = env.os_build()
    assert facts["system"] and facts["python"]
    assert "sys_platform" in facts


def test_local_facts_include_the_wsl_verdict() -> None:
    facts = env.local_facts()
    assert "wsl" in facts
    assert isinstance(facts["wsl"]["is_wsl"], bool)


def test_interface_of_unknown_address_is_none() -> None:
    assert env.interface_of(None) is None
    assert env.interface_of("203.0.113.199") is None
