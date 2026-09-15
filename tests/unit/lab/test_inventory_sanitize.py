"""Privacy filters of lab/inventory.py (0.2.2, docs/LAB.md §6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from _inventory.sanitize import Sanitizer, is_shareable_ipv4, redaction_source


def test_posix_home_becomes_tilde() -> None:
    s = Sanitizer(Path("/home/alice"))
    assert s.text("/home/alice/Desktop/FINAL YEAR/.venv/bin/python") == (
        "~/Desktop/FINAL YEAR/.venv/bin/python"
    )


def test_windows_home_with_space_becomes_tilde() -> None:
    s = Sanitizer(Path("C:\\Users\\Aayush Kumar"))
    exe = "C:\\Users\\Aayush Kumar\\Desktop\\FINAL YEAR\\.venv\\Scripts\\python.exe"
    assert s.text(exe) == "~\\Desktop\\FINAL YEAR\\.venv\\Scripts\\python.exe"
    assert s.text("C:/Users/Aayush Kumar/x") == "~/x"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/home/bob/.cache/x", "/home/<user>/.cache/x"),
        ("/Users/carol/Library", "/Users/<user>/Library"),
        ("C:\\Users\\Dave Smith\\AppData", "C:\\Users\\<user>\\AppData"),
        ("C:\\Users\\Public\\Desktop", "C:\\Users\\Public\\Desktop"),
    ],
)
def test_other_user_directories_are_masked(text: str, expected: str) -> None:
    assert Sanitizer(None).text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("connect to 49.36.10.77:443 failed", "connect to <public-ip>:443 failed"),
        ("192.168.31.13/24", "192.168.31.13/24"),
        (
            "10.0.0.1 172.17.0.1 169.254.3.4 127.0.0.1",
            "10.0.0.1 172.17.0.1 169.254.3.4 127.0.0.1",
        ),
        (
            "driver 32.0.15.6636 build 10.0.22631.1",
            "driver 32.0.15.6636 build 10.0.22631.1",
        ),
        ("595.44.02", "595.44.02"),
    ],
)
def test_public_ipv4_masked_private_and_versions_kept(text: str, expected: str) -> None:
    assert Sanitizer(None).text(text) == expected


def test_secrets_are_scrubbed() -> None:
    assert redaction_source() == "meshforge.common.redaction"
    out = Sanitizer(None).text("login failed: token=abc123 password: hunter2")
    assert "abc123" not in out
    assert "hunter2" not in out


def test_tree_sanitises_keys_and_values_keeps_other_types() -> None:
    s = Sanitizer(Path("/home/alice"))
    tree = {"/home/alice/k": ["/home/alice/v", 1, 2.5, True, None, ("49.36.10.77",)]}
    assert s.tree(tree) == {"~/k": ["~/v", 1, 2.5, True, None, ["<public-ip>"]]}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/home/alice", "~"),
        ("/home/alice/x", "~/x"),
        ("PATH=/home/alice/bin:/usr/bin", "PATH=~/bin:/usr/bin"),
        ("'/home/alice' missing", "'~' missing"),
        ("/home/alicekumar/x", "/home/<user>/x"),
        ("/home/alice-admin/x", "/home/<user>/x"),
        ("/home/alice.old/x", "/home/<user>/x"),
        ("/mnt/home/alice/x", "/mnt/home/<user>/x"),
    ],
)
def test_posix_home_only_at_path_boundaries(text: str, expected: str) -> None:
    assert Sanitizer(Path("/home/alice")).text(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("C:\\Users\\Ayu\\x", "~\\x"),
        ("c:\\users\\AYU\\x", "~\\x"),  # Windows paths are case-insensitive
        ("C:/Users/Ayu/x", "~/x"),
        ("C:\\Users\\Ayu", "~"),
        ("C:\\Users\\Ayu Kumar\\x", "C:\\Users\\<user>\\x"),
        ("C:\\Users\\AyuBackup\\x", "C:\\Users\\<user>\\x"),
        ("C:/Users/Ayu Kumar/x", "C:/Users/<user>/x"),
    ],
)
def test_windows_home_prefix_collision(text: str, expected: str) -> None:
    out = Sanitizer(Path("C:\\Users\\Ayu")).text(text)
    assert out == expected
    assert "kumar" not in out.lower() and "backup" not in out.lower()


def test_short_home_is_ignored() -> None:
    assert Sanitizer(Path("/")).text("/etc/os-release") == "/etc/os-release"


@pytest.mark.parametrize(
    ("addr", "shareable"),
    [
        ("192.168.1.2", True),
        ("10.1.2.3", True),
        ("169.254.1.1", True),
        ("127.0.0.1", True),
        ("8.8.8.8", False),
        ("100.64.0.1", False),
        ("not-an-ip", False),
    ],
)
def test_is_shareable_ipv4(addr: str, shareable: bool) -> None:
    assert is_shareable_ipv4(addr) is shareable
