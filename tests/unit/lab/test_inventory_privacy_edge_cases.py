"""Adversarial privacy + parsing edge cases for lab/inventory.py (0.2.2).

Independent of the implementer's own tests: derived from docs/LAB.md §6 ("no
usernames ... MAC and private IP addresses are fine") and success criterion 3
("no secrets or personal data"), not from reading ``sanitize.py``. Hermetic,
OS-independent (no subprocess, no real filesystem probing).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LAB_DIR = REPO_ROOT / "lab"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from _inventory import parse_windows as pw  # noqa: E402
from _inventory.sanitize import Sanitizer  # noqa: E402

# ---------------------------------------------------------------- home-prefix collision


@pytest.mark.parametrize(
    "other_user_path",
    [
        "/home/aayushkumar/secret.txt",
        "/home/aayush-admin/notes.md",
        "/home/aayush2/project/file.txt",
    ],
)
def test_home_dir_is_not_a_substring_prefix_of_another_user(
    other_user_path: str,
) -> None:
    """Another user's directory that merely starts with the same characters as the
    current user's home must be masked as ``<user>``, never leak the remaining
    characters of that other username.

    Reproduction: ``Sanitizer(Path("/home/aayush"))`` replaces the literal string
    ``"/home/aayush"`` wherever it occurs, including as a *prefix* of an unrelated
    directory name (``/home/aayushkumar``). This currently produces ``~kumar/...``
    instead of ``/home/<user>/...`` -- a privacy leak (part of another user's name
    survives) and also malformed output.
    """
    out = Sanitizer(Path("/home/aayush")).text(other_user_path)
    assert "aayush" not in out.lower(), out
    assert out.startswith("/home/<user>/") or out == "/home/<user>", out


@pytest.mark.parametrize(
    "other_user_path",
    [
        "C:\\Users\\Aayush2\\file.txt",
        "C:\\Users\\AayushBackup\\notes.md",
    ],
)
def test_windows_home_dir_is_not_a_substring_prefix_of_another_user(
    other_user_path: str,
) -> None:
    out = Sanitizer(Path("C:\\Users\\Aayush")).text(other_user_path)
    assert "aayush" not in out.lower(), out


# ---------------------------------------------------------------- env vars never collected


def test_env_var_secrets_never_appear_in_collected_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No probe reads process environment variables into the document (LAB.md §6:
    "no ... environment variables"). Setting a secret-looking env var and running
    the full (mocked-machine) collector must never leak it.
    """
    monkeypatch.setenv("MESHFORGE_TEST_SECRET_TOKEN", "sk-super-secret-value-123")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "another-fake-secret-abcxyz")

    sys.path.insert(0, str(LAB_DIR))
    from _inventory import probes  # local import: after sys.path insert, cached ok
    from _inventory.runner import CmdResult

    monkeypatch.setattr(probes, "IS_LINUX", True)
    monkeypatch.setattr(probes, "IS_WINDOWS", False)
    monkeypatch.setattr(probes, "FS_ROOT", Path("/nonexistent-fake-root"))
    monkeypatch.setattr(
        probes,
        "run",
        lambda name, args, **_: CmdResult(
            (name,), None, "", "", error=f"{name}: not found on PATH"
        ),
    )
    monkeypatch.setattr(probes, "which", lambda _n: None)
    monkeypatch.setattr(probes, "optional_module", lambda _n: None)
    monkeypatch.setattr(probes, "_windows_secure_boot_registry", lambda: None)

    doc = probes.collect(Path("/tmp"))
    text = json.dumps(doc)
    assert "sk-super-secret-value-123" not in text
    assert "another-fake-secret-abcxyz" not in text


# ---------------------------------------------------------------- unicode passthrough


def test_windows_unicode_adapter_names_survive_and_are_not_mangled() -> None:
    """Localised / unicode adapter names and descriptions (e.g. a German or CJK
    Windows install) must parse and JSON-serialise cleanly, and must not be
    accidentally rewritten by the privacy filters (they are not paths, IPs, or
    home directories).
    """
    adapters = [
        {
            "Name": "WLAN",
            "InterfaceDescription": "Intel(R) Wi-Fi 6 AX201 160MHz — 中文適配器",
            "InterfaceIndex": 12,
            "MacAddress": "AC-19-8E-67-92-06",
            "Status": "Up",
            "LinkSpeed": "866.7 Mbps",
            "ReceiveLinkSpeed": 0,
            "MediaType": "Native 802.11",
            "PhysicalMediaType": "Native 802.11",
            "NdisPhysicalMedium": 9,
            "Virtual": False,
            "HardwareInterface": True,
        },
        {
            "Name": "Ethernet Ω",
            "InterfaceDescription": "Réaltek PCIe GbE Family Controller",
            "InterfaceIndex": 7,
            "MacAddress": "E0-73-E7-D2-5A-54",
            "Status": "Disconnected",
            "LinkSpeed": None,
            "ReceiveLinkSpeed": 0,
            "MediaType": "802.3",
            "PhysicalMediaType": "802.3",
            "NdisPhysicalMedium": 14,
            "Virtual": False,
            "HardwareInterface": True,
        },
    ]
    result = pw.parse_net_adapters(adapters, [])
    names = {a["name"] for a in result}
    assert names == {"WLAN", "Ethernet Ω"}
    wifi = next(a for a in result if a["name"] == "WLAN")
    assert wifi["kind"] == "wifi"
    assert "中文適配器" in str(wifi["description"])

    sanitized = Sanitizer(None).tree(result)
    assert sanitized == result  # unicode text must pass through unchanged
    json.dumps(sanitized, ensure_ascii=False)  # must not raise


def test_unicode_home_directory_still_masked() -> None:
    """A home directory containing non-ASCII characters (a real case on some lab
    laptops, e.g. a user profile named with accented letters) must still be
    replaced with ``~``.
    """
    home = Path("/home/józef")
    s = Sanitizer(home)
    assert (
        s.text("/home/józef/Desktop/project/file.txt") == "~/Desktop/project/file.txt"
    )


# ---------------------------------------------------------------- non-UTF8 / garbage tool output


def test_garbage_nvidia_smi_csv_output_is_recorded_not_crashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken/mocking ``nvidia-smi`` that prints unrelated garbage (not valid
    CSV) on a zero exit code must be recorded as a failed probe, never raise.
    """
    sys.path.insert(0, str(LAB_DIR))
    from _inventory import probes
    from _inventory.runner import CmdResult

    def fake_which(name: str) -> str | None:
        return "/fake/bin/nvidia-smi" if name == "nvidia-smi" else None

    def fake_run(name: str, args: list[str], **_: Any) -> CmdResult:
        return CmdResult((name, *args), 0, "not,csv,at,all\ngarbage\n", "")

    monkeypatch.setattr(probes, "which", fake_which)
    monkeypatch.setattr(probes, "run", fake_run)
    probes.nvidia_status.cache_clear()
    status = probes._nvidia_smi()
    probes.nvidia_status.cache_clear()
    assert status["available"] is True
    assert status["ok"] is False
    assert "error" in status


def test_nonutf8_bytes_from_a_tool_never_raise() -> None:
    """The subprocess decoder must never raise on arbitrary/invalid byte sequences
    (a hostile or corrupted tool output), regardless of the declared encoding.
    """
    sys.path.insert(0, str(LAB_DIR))
    from _inventory import runner

    weird = bytes(range(256))
    text = runner._decode(weird, "utf-8")
    assert isinstance(text, str)
    json.dumps({"x": text})  # must be JSON-serialisable after decode

    text16 = runner._decode(weird, "utf-16-le")
    assert isinstance(text16, str)


# ---------------------------------------------------------------- disk full / unwritable parent


def test_dumps_output_is_valid_utf8_json_with_surrogate_free_text() -> None:
    """dumps() must produce valid, re-parseable JSON even with unicode content and
    must not use ``ensure_ascii`` (LAB README promises deterministic, sorted-key
    UTF-8 output).
    """
    sys.path.insert(0, str(LAB_DIR))
    import inventory

    doc = {"schema_version": 1, "b": "中文", "a": "café"}
    text = inventory.dumps(doc, pretty=False)
    assert text.endswith("\n")
    assert "café" in text  # not escaped to \u00e9 (ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed == doc
    # keys are sorted
    assert text.index('"a"') < text.index('"b"')
