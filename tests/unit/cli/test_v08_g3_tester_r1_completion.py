"""V0.8 G3 tester r1: D191 d/e - ``install_bash`` on hostile homes: bytes in any
encoding, no trailing newline, CRLF, ``~/.bashrc`` a directory, a file where the
completion folder belongs; always exit 4 (never a traceback), always idempotent."""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.cli import completion
from meshforge.cli.exits import CliExit

pytestmark = pytest.mark.unit

KW = {"prog_name": "compute", "complete_var": "_COMPUTE_COMPLETE", "shell": "bash"}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Path.home() on Windows
    return tmp_path


@pytest.mark.parametrize(
    "seed",
    [
        b"",
        b"# caf\xe9 latin-1\n",
        b"\xff\xfe\x00bad",  # no final newline, invalid UTF-8
        b"export X=1\r\n",
        b"\x00\x01\x02",
    ],
)
def test_bashrc_in_any_bytes_gets_the_line_once_and_is_otherwise_untouched(
    home: Path, seed: bytes
) -> None:
    rc = home / ".bashrc"
    if seed:
        rc.write_bytes(seed)
    completion.install_bash(**KW)
    after_first = rc.read_bytes()
    assert after_first.startswith(seed)
    assert after_first.count(b".bash_completions/compute.sh") == 1
    completion.install_bash(**KW)
    completion.install_bash(**KW)
    assert rc.read_bytes() == after_first  # byte-identical on re-runs


def test_bashrc_that_is_a_directory_exits_4_not_a_traceback(home: Path) -> None:
    (home / ".bashrc").mkdir()
    with pytest.raises(SystemExit) as info:
        completion.install_bash(**KW)
    assert info.value.code == CliExit.CONFIG_ERROR == 4


def test_a_file_where_the_completion_folder_belongs_exits_4(home: Path) -> None:
    (home / ".bash_completions").write_text("not a folder")
    with pytest.raises(SystemExit) as info:
        completion.install_bash(**KW)
    assert info.value.code == 4


def test_a_home_with_a_single_quote_is_quoted_and_still_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    odd = tmp_path / "o'brien $HOME `x`"
    odd.mkdir()
    monkeypatch.setenv("HOME", str(odd))
    monkeypatch.setenv("USERPROFILE", str(odd))  # Path.home() on Windows
    completion.install_bash(**KW)
    first = (odd / ".bashrc").read_bytes()
    completion.install_bash(**KW)
    assert (odd / ".bashrc").read_bytes() == first
    import shlex
    import subprocess

    line = first.decode().strip()
    # the line must parse to exactly: source <path>
    assert shlex.split(line)[0] == "source"
    assert shlex.split(line)[1] == str(odd / ".bash_completions" / "compute.sh")
    ok = subprocess.run(["bash", "-n", "-c", line], capture_output=True, check=False)
    assert ok.returncode == 0
