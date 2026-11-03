"""``runner.powershell_script``: PowerShell runs from a temporary ``.ps1`` file.

Lab L3 (Windows 11, R1-3/R1-5): ``powershell -EncodedCommand <~6000 chars>`` hung
for more than 40 s while the same script run with ``-File`` answered in ~3 s. These
tests pin the replacement: the file holds the exact script as UTF-8 with BOM, sits
in a fresh private directory, and is deleted afterwards - also when the command
times out or the caller raises. The callers (inventory CIM bundle, S2 profile
query) use it. No real PowerShell is needed (fake ``run``).
"""

from __future__ import annotations

import codecs
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from _inventory import parse_windows as pw
from _inventory import probes, runner
from _inventory.runner import CmdResult
from spikes._s2 import env


@pytest.fixture
def temp_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``tempfile`` at an empty directory so leftovers are visible."""
    root = tmp_path / "temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def _script_path(args: list[str]) -> Path:
    assert args[-2] == "-File"
    return Path(args[-1])


def test_args_are_the_fixed_options_then_file(temp_root: Path) -> None:
    with runner.powershell_script("Write-Output 1") as args:
        assert args[:-1] == [
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
        ]
        assert "-EncodedCommand" not in args
        assert "-Command" not in args
        path = _script_path(args)
        assert path.suffix == ".ps1"
        assert path.parent.parent == temp_root


def test_file_is_utf8_with_bom_and_holds_the_exact_script(temp_root: Path) -> None:
    script = "Write-Output 'Grüße \u2013 日本'\r\nWrite-Output 2\n"
    with runner.powershell_script(script) as args:
        raw = _script_path(args).read_bytes()
    assert raw.startswith(codecs.BOM_UTF8)
    assert raw[len(codecs.BOM_UTF8) :].decode("utf-8") == script  # no newline rewrite


def test_cim_script_round_trips_through_the_file(temp_root: Path) -> None:
    with runner.powershell_script(pw.CIM_SCRIPT) as args:
        text = _script_path(args).read_text(encoding="utf-8-sig")
    assert text == pw.CIM_SCRIPT


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_directory_is_private(temp_root: Path) -> None:
    with runner.powershell_script("1") as args:
        mode = stat.S_IMODE(_script_path(args).parent.stat().st_mode)
    assert mode == 0o700


def test_each_call_gets_its_own_directory(temp_root: Path) -> None:
    with (
        runner.powershell_script("1") as first,
        runner.powershell_script("2") as second,
    ):
        assert _script_path(first).parent != _script_path(second).parent
    assert list(temp_root.iterdir()) == []


def test_deleted_after_a_normal_exit(temp_root: Path) -> None:
    with runner.powershell_script("1") as args:
        path = _script_path(args)
        assert path.is_file()
    assert not path.exists()
    assert not path.parent.exists()
    assert list(temp_root.iterdir()) == []


@pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyboardInterrupt()])
def test_deleted_when_the_caller_raises(temp_root: Path, exc: BaseException) -> None:
    seen: list[Path] = []

    def body() -> None:
        with runner.powershell_script("1") as args:
            seen.append(_script_path(args))
            raise exc

    with pytest.raises(type(exc)):
        body()
    assert not seen[0].parent.exists()
    assert list(temp_root.iterdir()) == []


def test_deleted_when_writing_the_script_fails(temp_root: Path) -> None:
    with pytest.raises(UnicodeEncodeError), runner.powershell_script("\ud800"):
        pass  # pragma: no cover - never entered
    assert list(temp_root.iterdir()) == []


def test_cleanup_retries_a_briefly_locked_file(
    temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows: a just-killed PowerShell may hold the file for a moment."""
    real_rmtree = shutil.rmtree
    tries: list[int] = []

    def flaky_rmtree(path: Any, ignore_errors: bool = False) -> None:
        tries.append(1)
        if len(tries) < 3:
            return  # "sharing violation", ignored: the tree is still there
        real_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(runner, "_CLEANUP_PAUSE_S", 0.0)
    with runner.powershell_script("1"):
        pass
    assert len(tries) == 3
    assert list(temp_root.iterdir()) == []


def test_cleanup_gives_up_without_raising(
    temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(shutil, "rmtree", lambda *_a, **_k: calls.append(1))
    monkeypatch.setattr(runner, "_CLEANUP_PAUSE_S", 0.0)
    with runner.powershell_script("1"):
        pass
    assert len(calls) == runner._CLEANUP_TRIES


# ----------------------------------------------------------------- callers


def _fake_powershell(
    seen: list[tuple[str, ...]], result: CmdResult | BaseException
) -> Any:
    def fake_run(name: str, args: list[str], **_: Any) -> CmdResult:
        path = _script_path(args)
        seen.append((name, *args, path.read_text(encoding="utf-8-sig")))
        if isinstance(result, BaseException):
            raise result
        return result

    return fake_run


TIMED_OUT = CmdResult(
    ("powershell",), None, "", "", error="powershell: timed out after 60 s"
)


def test_cim_bundle_runs_the_script_from_a_file(
    temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, ...]] = []
    ok = CmdResult(("powershell",), 0, '{"processor": {"ok": true, "data": []}}', "")
    monkeypatch.setattr(probes, "run", _fake_powershell(seen, ok))
    assert probes.windows_cim() == {"processor": {"ok": True, "data": []}}
    (call,) = seen
    assert call[0] == "powershell"
    assert call[-1] == pw.CIM_SCRIPT  # the file held the script during the call
    assert list(temp_root.iterdir()) == []


def test_cim_bundle_timeout_keeps_the_typed_error_and_cleans_up(
    temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(probes, "run", _fake_powershell(seen, TIMED_OUT))
    with pytest.raises(RuntimeError, match=r"powershell CIM query failed: .*timed out"):
        probes.windows_cim()
    assert len(seen) == 1
    assert list(temp_root.iterdir()) == []


def test_cim_bundle_interrupt_cleans_up(
    temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(probes, "run", _fake_powershell(seen, KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        probes.windows_cim()
    assert list(temp_root.iterdir()) == []


def test_s2_profile_query_runs_from_a_file(
    temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, ...]] = []
    out = '[{"InterfaceAlias": "Wi-Fi", "NetworkCategory": 1}]'
    ok = CmdResult(("powershell",), 0, out, "")
    monkeypatch.setattr(env, "IS_WINDOWS", True)
    monkeypatch.setattr(runner, "run", _fake_powershell(seen, ok))
    result = env.firewall_profile()
    assert result["source"] == "powershell"
    assert "error" not in result
    (call,) = seen
    assert call[-1] == env.PROFILE_SCRIPT
    assert list(temp_root.iterdir()) == []


def test_s2_profile_timeout_is_recorded_and_cleans_up(
    temp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(env, "IS_WINDOWS", True)
    monkeypatch.setattr(runner, "run", _fake_powershell(seen, TIMED_OUT))
    result = env.firewall_profile()
    assert result["profiles"] == []
    assert "timed out" in result["error"]
    assert list(temp_root.iterdir()) == []
