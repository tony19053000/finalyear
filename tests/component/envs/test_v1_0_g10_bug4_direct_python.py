"""V1.0 G10 BUG-4 (D451): the base interpreter + ``__PYVENV_LAUNCHER__`` *is*
the venv - a real uv venv, a real child process.

Windows (CI runner, lab): :func:`direct_python` on the venv the way the venv
runtime starts a worker; the worker's ``sys.prefix`` is the venv, it does not
see the base interpreter's site-packages, and only ONE process ran (no launcher).
Everywhere: the same CPython mechanism with the POSIX base interpreter (the
runtime itself uses it on Windows only - a POSIX venv's python is a symlink).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from meshforge.agent.execution.venv_runtime import PYVENV_LAUNCHER_VAR, direct_python

pytestmark = pytest.mark.component

_PROBE = (
    "import json, os, sys; print(json.dumps({'prefix': sys.prefix, "
    "'base': sys.base_prefix, 'exe': sys.executable, 'pid': os.getpid(), "
    "'ppid': os.getppid()}))"
)


def _uv_venv(tmp_path: Path) -> Path:
    uv = pytest.importorskip("uv")
    venv = tmp_path / "venv"
    subprocess.run(
        [uv.find_uv_bin(), "venv", "--quiet", "--python", sys.executable, str(venv)],
        check=True,
        timeout=120,
    )
    return venv


def _probe(argv: list[str], extra_env: dict[str, str]) -> dict[str, object]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", PYVENV_LAUNCHER_VAR)
    }
    env.update(extra_env)
    proc = subprocess.Popen(
        [*argv, "-c", _PROBE],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
    )
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0
    data: dict[str, object] = json.loads(out)
    data["spawned_pid"] = proc.pid
    return data


def _same(a: object, b: Path) -> bool:
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(
        os.path.realpath(str(b))
    )


@pytest.mark.skipif(sys.platform != "win32", reason="the venv launcher is Windows-only")
def test_windows_worker_is_the_venv_in_one_process(tmp_path: Path) -> None:
    venv = _uv_venv(tmp_path)
    got = direct_python(venv)
    assert got is not None, (venv / "pyvenv.cfg").read_text()
    exe, env = got
    data = _probe([str(exe)], env)
    assert _same(data["prefix"], venv)
    assert not _same(data["base"], venv)
    # one process: the pid we started is the interpreter (no launcher hop)
    assert data["pid"] == data["spawned_pid"]
    # the launcher, for contrast, starts a second process
    via_launcher = _probe([str(venv / "Scripts" / "python.exe")], {})
    assert _same(via_launcher["prefix"], venv)


def test_the_mechanism_works_with_the_posix_interpreter_too(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("covered by the Windows test")
    venv = _uv_venv(tmp_path)
    home = next(
        line.partition("=")[2].strip()
        for line in (venv / "pyvenv.cfg").read_text().splitlines()
        if line.partition("=")[0].strip().lower() == "home"
    )
    base = next(
        Path(home) / n for n in ("python3", "python") if (Path(home) / n).exists()
    )
    data = _probe([str(base)], {PYVENV_LAUNCHER_VAR: str(venv / "bin" / "python")})
    assert _same(data["prefix"], venv)
    assert data["pid"] == data["spawned_pid"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows error mode")
def test_windows_children_inherit_the_quiet_error_mode() -> None:
    from meshforge.agent.win_error_mode import QUIET_FLAGS, quiet_error_mode

    assert quiet_error_mode() is not None
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ctypes; print(ctypes.windll.kernel32.GetErrorMode())",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    assert int(out) & QUIET_FLAGS == QUIET_FLAGS
