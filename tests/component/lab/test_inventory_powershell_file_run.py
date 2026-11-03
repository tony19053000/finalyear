"""``runner.powershell_script`` + ``runner.run`` against real processes.

Off Windows a fake ``powershell`` executable on PATH stands in (a Python script
that checks it was given ``-File <existing .ps1 with BOM>``); it proves the
temporary script outlives the call and is removed afterwards, also when the
command times out and its process tree is killed. On Windows the real Windows
PowerShell 5.1 runs a script with non-ASCII text (lab L3 R1-3: ``-File`` instead
of the ``-EncodedCommand`` that hung there).
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

LAB_DIR: Final = Path(__file__).resolve().parents[3] / "lab"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from _inventory import runner  # noqa: E402

pytestmark = pytest.mark.timeout(60)

#: Stand-in for powershell: validates argv and the script file, then either
#: prints the script (minus BOM) or sleeps (``Start-Sleep`` in the script).
FAKE_POWERSHELL: Final = """import codecs, sys, time
args = sys.argv[1:]
assert args[:4] == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"], args
assert args[4] == "-File" and len(args) == 6, args
raw = open(args[5], "rb").read()
assert raw.startswith(codecs.BOM_UTF8), raw[:8]
text = raw[3:].decode("utf-8")
if "Start-Sleep" in text:
    time.sleep(120)
sys.stdout.buffer.write(text.encode("utf-8"))
"""


@pytest.fixture(autouse=True)
def _fresh_runner() -> Iterator[None]:
    runner.reset()
    yield
    runner.reset()


@pytest.fixture
def temp_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "temp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


@pytest.fixture
def fake_powershell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    if sys.platform == "win32":
        pytest.skip("POSIX fake executable; Windows uses the real PowerShell test")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = tmp_path / "fake_powershell.py"
    fake.write_text(FAKE_POWERSHELL, encoding="utf-8")
    exe = bin_dir / "powershell"
    # A /bin/sh wrapper, not a shebang: the interpreter path may contain spaces.
    exe.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8"
    )
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return exe


def test_script_is_read_from_the_file_then_removed(
    fake_powershell: Path, temp_root: Path
) -> None:
    script = "Write-Output 'Grüße'\n"
    with runner.powershell_script(script) as args:
        result = runner.run("powershell", args, timeout=20)
    assert result.ok, result.describe_failure()
    assert result.stdout == script
    assert list(temp_root.iterdir()) == []


def test_timeout_kills_powershell_and_removes_the_script(
    fake_powershell: Path, temp_root: Path
) -> None:
    with runner.powershell_script("Start-Sleep 120") as args:
        result = runner.run("powershell", args, timeout=1)
    assert result.error == "powershell: timed out after 1 s"
    assert list(temp_root.iterdir()) == []
    # the typed "hung once" behaviour is unchanged
    with runner.powershell_script("Write-Output 1") as args:
        again = runner.run("powershell", args, timeout=20)
    assert again.error == f"powershell: {runner.SKIPPED_HUNG}"
    assert list(temp_root.iterdir()) == []


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell") is None,
    reason="real Windows PowerShell only",
)
def test_real_windows_powershell_runs_a_non_ascii_script(temp_root: Path) -> None:
    script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "Write-Output ('Grüße ' + $PSVersionTable.PSVersion.Major)\n"
    )
    with runner.powershell_script(script) as args:
        result = runner.run("powershell", args, timeout=45)
    assert result.ok, result.describe_failure()
    assert result.stdout.strip().startswith("Grüße ")
    assert list(temp_root.iterdir()) == []
