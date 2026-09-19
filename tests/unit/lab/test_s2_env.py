"""Unit tests for ``lab/spikes/_s2/env.py``: WSL detection and the refusal
gate (roadmap 0.2.6 / ADR-0009), and the Windows firewall-profile parser.

All evidence is fabricated. These tests must never depend on whether this
machine happens to be WSL, native Linux, or Windows -- ``wsl_status`` takes its
evidence as arguments precisely so it is a pure function of that evidence.
"""

from __future__ import annotations

import pytest

from spikes._s2 import env


# -------------------------------------------------------------- wsl_status
def test_windows_platform_is_always_native() -> None:
    status = env.wsl_status(platform_name="win32")
    assert status["is_wsl"] is False
    assert status["native"] is True


def test_macos_platform_is_always_native() -> None:
    status = env.wsl_status(platform_name="darwin")
    assert status["is_wsl"] is False


def test_native_linux_evidence_is_not_wsl() -> None:
    status = env.wsl_status(
        proc_version="Linux version 6.8.0-generic (buildd@lcy02)",
        osrelease="6.8.0-139-generic",
        env={},
        platform_name="linux",
    )
    assert status["is_wsl"] is False
    assert status["native"] is True


def test_wsl2_proc_version_marker_detected() -> None:
    status = env.wsl_status(
        proc_version="Linux version 5.15.90.1-microsoft-standard-WSL2",
        osrelease="5.15.90.1-microsoft-standard-WSL2",
        env={},
        platform_name="linux",
    )
    assert status["is_wsl"] is True
    assert status["wsl_version"] == 2
    assert status["native"] is False


def test_wsl1_osrelease_marker_detected() -> None:
    status = env.wsl_status(
        proc_version="Linux version 4.4.0-19041-Microsoft",
        osrelease="4.4.0-19041-Microsoft",
        env={},
        platform_name="linux",
    )
    assert status["is_wsl"] is True
    assert status["wsl_version"] == 1


def test_wsl_distro_env_var_alone_is_sufficient_evidence() -> None:
    """Even with clean kernel strings, WSL_DISTRO_NAME must be trusted (the
    detection rules are shared with lab/inventory.py)."""
    status = env.wsl_status(
        proc_version="Linux version 6.8.0-generic",
        osrelease="6.8.0-generic",
        env={"WSL_DISTRO_NAME": "Ubuntu"},
        platform_name="linux",
    )
    assert status["is_wsl"] is True
    assert status["distro"] == "Ubuntu"


def test_wsl_interop_env_var_alone_is_sufficient_evidence() -> None:
    """WSL_INTEROP can be set even when the kernel string was scrubbed/spoofed;
    the module docstring lists it as one of the four signals."""
    status = env.wsl_status(
        proc_version="Linux version 6.8.0-generic",
        osrelease="6.8.0-generic",
        env={"WSL_INTEROP": "/run/WSL/1_interop"},
        platform_name="linux",
    )
    assert status["is_wsl"] is True


def test_empty_env_var_values_are_not_evidence() -> None:
    status = env.wsl_status(
        proc_version="Linux version 6.8.0-generic",
        osrelease="6.8.0-generic",
        env={"WSL_DISTRO_NAME": "", "WSL_INTEROP": ""},
        platform_name="linux",
    )
    assert status["is_wsl"] is False


def test_evidence_is_truncated_and_never_raises_on_huge_input() -> None:
    status = env.wsl_status(
        proc_version="A" * 10_000,
        osrelease="B" * 10_000,
        env={},
        platform_name="linux",
    )
    assert len(status["evidence"]["proc_version"]) <= 200
    assert len(status["evidence"]["osrelease"]) <= 200


# ------------------------------------------------------------- refuse_if_wsl
def test_refuse_if_wsl_blocks_by_default() -> None:
    status = env.wsl_status(
        proc_version="microsoft-standard-WSL2",
        osrelease="",
        env={},
        platform_name="linux",
    )
    refusal = env.refuse_if_wsl(status, allow_wsl=False)
    assert refusal is not None
    assert "refusing to run inside WSL" in refusal
    assert "WSL2" in refusal


def test_allow_wsl_overrides_the_refusal() -> None:
    status = env.wsl_status(
        proc_version="microsoft-standard-WSL2",
        osrelease="",
        env={},
        platform_name="linux",
    )
    assert env.refuse_if_wsl(status, allow_wsl=True) is None


def test_refuse_if_wsl_is_none_for_native() -> None:
    status = env.wsl_status(
        proc_version="Linux version 6.8.0-generic",
        osrelease="6.8.0-generic",
        env={},
        platform_name="linux",
    )
    assert env.refuse_if_wsl(status, allow_wsl=False) is None


def test_refuse_if_wsl_reports_unknown_distro_when_absent() -> None:
    status = env.wsl_status(
        proc_version="Linux version 4.4.0-19041-Microsoft",
        osrelease="",
        env={},
        platform_name="linux",
    )
    refusal = env.refuse_if_wsl(status, allow_wsl=False)
    assert refusal is not None and "unknown distro" in refusal


def test_wsl_status_here_never_touches_real_root_when_given_a_fake_one(
    tmp_path: object,
) -> None:
    """``root`` is documented as a test seam: this must read the *given* tree,
    never the real machine's /proc, so the test's own verdict cannot depend on
    whether this CI/dev machine happens to be WSL."""
    from pathlib import Path

    fake_root = Path(str(tmp_path))
    (fake_root / "proc" / "sys" / "kernel").mkdir(parents=True)
    (fake_root / "proc" / "version").write_text(
        "Linux version 5.15.90.1-microsoft-standard-WSL2\n", encoding="utf-8"
    )
    (fake_root / "proc" / "sys" / "kernel" / "osrelease").write_text(
        "5.15.90.1-microsoft-standard-WSL2\n", encoding="utf-8"
    )
    if not __import__("sys").platform.startswith("linux"):
        pytest.skip("wsl_status_here only reads /proc on Linux")
    status = env.wsl_status_here(root=fake_root)
    assert status["is_wsl"] is True


def test_wsl_status_here_missing_proc_files_reads_as_empty_not_crash(
    tmp_path: object,
) -> None:
    from pathlib import Path

    if not __import__("sys").platform.startswith("linux"):
        pytest.skip("wsl_status_here only reads /proc on Linux")
    fake_root = Path(str(tmp_path))  # no /proc at all under this empty tree
    status = env.wsl_status_here(root=fake_root)
    assert status["is_wsl"] is False


# -------------------------------------------------------- firewall profile
def test_parse_connection_profiles_single_object_becomes_list() -> None:
    rows = env.parse_connection_profiles(
        '{"InterfaceAlias": "Wi-Fi", "NetworkCategory": "Public"}'
    )
    assert rows == [
        {
            "interface": "Wi-Fi",
            "interface_index": None,
            "network": None,
            "category": "Public",
            "ipv4_connectivity": None,
        }
    ]


def test_parse_connection_profiles_empty_text_is_empty_list() -> None:
    assert env.parse_connection_profiles("") == []
    assert env.parse_connection_profiles("[]") == []


def test_parse_connection_profiles_ignores_non_dict_rows() -> None:
    rows = env.parse_connection_profiles('[1, "x", {"InterfaceAlias": "Eth"}]')
    assert len(rows) == 1
    assert rows[0]["interface"] == "Eth"


def test_parse_connection_profiles_garbage_json_raises() -> None:
    with pytest.raises((ValueError, TypeError)):
        env.parse_connection_profiles("not json")


def test_non_private_profiles_flags_public_only() -> None:
    firewall = {
        "profiles": [
            {"interface": "Wi-Fi", "category": "Public"},
            {"interface": "Ethernet", "category": "Private"},
            {"interface": "Domain", "category": "DomainAuthenticated"},
            {"interface": "weird-case", "category": "PRIVATE"},
        ]
    }
    assert env.non_private_profiles(firewall) == ["Wi-Fi"]


def test_non_private_profiles_empty_when_no_profiles_key() -> None:
    assert env.non_private_profiles({}) == []


def test_firewall_profile_on_non_windows_never_shells_out() -> None:
    if env.IS_WINDOWS:
        pytest.skip("this assertion is about the non-Windows branch")
    result = env.firewall_profile()
    assert result == {"source": "n/a", "supported": False, "profiles": []}
