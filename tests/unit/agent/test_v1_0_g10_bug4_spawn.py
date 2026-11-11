"""V1.0 G10 BUG-4 (D451), spawn half: one worker process, no error dialogs.

Lab L4 (Windows, venv runtime, job_01M73B13999W9AWDT89F7KDX2H chunk 68): the
venv's ``Scripts\\python.exe`` is a launcher that re-executes the base
interpreter; the real worker started 17 s after the launcher, then sat idle.

* :func:`direct_python` names the base interpreter of a venv (``pyvenv.cfg``
  ``home``) and the ``__PYVENV_LAUNCHER__`` variable that makes it *be* the venv
  (CPython ``getpath``: the variable stands in for ``sys.executable`` and the venv
  is found next to it) - what the launcher itself does, minus the extra process.
* :func:`quiet_error_mode` sets the inheritable Windows error mode so a critical
  error / missing-DLL / GP-fault dialog can never block a worker that no one can
  see (a service has no desktop).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.agent.execution.venv_runtime import (
    PYVENV_LAUNCHER_VAR,
    direct_python,
)
from meshforge.agent.win_error_mode import QUIET_FLAGS, quiet_error_mode

pytestmark = pytest.mark.unit


def _venv(tmp_path: Path, *, home: Path | None, base_names: tuple[str, ...]) -> Path:
    venv = tmp_path / "venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"launcher")
    base = tmp_path / "base"
    base.mkdir()
    for name in base_names:
        (base / name).write_bytes(b"interp")
    lines = ["implementation = CPython", "version_info = 3.12"]
    if home is not None:
        lines.insert(0, f"home = {home}")
    (venv / "pyvenv.cfg").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return venv


def test_windows_venv_runs_its_base_interpreter_directly(tmp_path: Path) -> None:
    venv = _venv(tmp_path, home=tmp_path / "base", base_names=("python.exe",))
    got = direct_python(venv, platform="win32")
    assert got is not None
    exe, env = got
    assert exe == tmp_path / "base" / "python.exe"
    assert env == {PYVENV_LAUNCHER_VAR: str(venv / "Scripts" / "python.exe")}


def test_home_key_is_case_and_space_tolerant(tmp_path: Path) -> None:
    venv = _venv(tmp_path, home=None, base_names=("python.exe",))
    cfg = venv / "pyvenv.cfg"
    cfg.write_text(f"  HOME   =   {tmp_path / 'base'}  \n", encoding="utf-8")
    got = direct_python(venv, platform="win32")
    assert got is not None and got[0] == tmp_path / "base" / "python.exe"


@pytest.mark.parametrize(
    "case", ["no_cfg", "no_home", "base_missing", "venv_python_missing"]
)
def test_anything_unusual_keeps_the_launcher(tmp_path: Path, case: str) -> None:
    venv = _venv(
        tmp_path,
        home=None if case == "no_home" else tmp_path / "base",
        base_names=() if case == "base_missing" else ("python.exe",),
    )
    if case == "no_cfg":
        (venv / "pyvenv.cfg").unlink()
    if case == "venv_python_missing":
        (venv / "Scripts" / "python.exe").unlink()
    assert direct_python(venv, platform="win32") is None


def test_posix_venvs_need_no_direct_start(tmp_path: Path) -> None:
    """There the venv's python is a symlink - no launcher process to skip."""
    venv = _venv(tmp_path, home=tmp_path / "base", base_names=("python.exe",))
    assert direct_python(venv, platform="linux") is None


# ---------------------------------------------------------------- error mode


class _Kernel32:
    def __init__(self, mode: int) -> None:
        self.mode = mode
        self.set_calls: list[int] = []

    def GetErrorMode(self) -> int:  # noqa: N802 - the Win32 name
        return self.mode

    def SetErrorMode(self, mode: int) -> int:  # noqa: N802 - the Win32 name
        self.set_calls.append(mode)
        old, self.mode = self.mode, mode
        return old


def test_error_mode_adds_the_quiet_flags_and_keeps_the_rest() -> None:
    k = _Kernel32(0x4)  # SEM_NOALIGNMENTFAULTEXCEPT already set
    assert quiet_error_mode(platform="win32", kernel32=k) == 0x4 | QUIET_FLAGS
    assert k.set_calls == [0x4 | QUIET_FLAGS]
    assert QUIET_FLAGS == 0x0001 | 0x0002 | 0x8000


def test_error_mode_is_a_no_op_off_windows() -> None:
    assert quiet_error_mode(platform="linux") is None


def test_error_mode_failure_is_never_fatal() -> None:
    class Broken:
        def GetErrorMode(self) -> int:  # noqa: N802
            raise OSError("no kernel32")

    assert quiet_error_mode(platform="win32", kernel32=Broken()) is None
