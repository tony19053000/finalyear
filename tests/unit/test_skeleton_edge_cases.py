"""Independent tester coverage for task 0.1.3 (package skeleton + entry points).

Complements ``tests/unit/test_skeleton.py`` (implementer's tests) with checks
derived directly from the task's success criteria rather than from reading the
implementation: stdout/stderr separation under non-UTF-8 locales, usage-error
handling for *all three* entry points (not just ``compute``), exit-code
stability, ``--help`` behaviour, and wheel packaging (py.typed, subpackages,
entry_points.txt).

Never modifies ``src/``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from mf_fabric import ca_file
from typer.testing import CliRunner

import meshforge
from meshforge.agent.main import app as agent_app
from meshforge.cli.app import app as cli_app
from meshforge.common.exit_codes import ExitCode
from meshforge.controller.app import app as controller_app

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def _run_ascii_env(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a command forcing ASCII-only stdio, as a Windows console (cp437/cp1252
    codepage without UTF-8 opt-in) or a POSIX ``C`` locale would."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "ascii"
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    # Port 1 is never listening, so `compute status` fails the same way on every
    # machine. Without this the subprocess inherits the developer's
    # MESHFORGE_* environment and the exit code depends on whether a controller
    # happens to be running - the sibling tests in test_skeleton.py pin it too.
    env["MESHFORGE_CLI__CONTROLLER_URL"] = "https://127.0.0.1:1"
    env["MESHFORGE_CLI__CA_FILE"] = str(ca_file())  # V0.7 G3: trust to reach it
    return subprocess.run(
        args, capture_output=True, text=True, timeout=30, check=False, env=env
    )


# --- exit code contract -----------------------------------------------------


def test_exit_code_values_are_stable() -> None:
    """0/1/2/3 are part of the scripting contract; accidental renumbering would
    silently break callers relying on these codes."""
    assert int(ExitCode.OK) == 0
    assert int(ExitCode.FAILURE) == 1
    assert int(ExitCode.USAGE_ERROR) == 2
    assert int(ExitCode.NOT_IMPLEMENTED) == 3


# --- usage errors on ALL three entry points (not just `compute`) -----------


@pytest.mark.parametrize(
    ("app", "prog"),
    [
        (cli_app, "compute"),
        (controller_app, "meshforge-controller"),
        (agent_app, "meshforge-agent"),
    ],
)
def test_unknown_option_is_usage_error_all_entry_points(app: object, prog: str) -> None:
    result = runner.invoke(app, ["--this-flag-does-not-exist"], prog_name=prog)  # type: ignore[arg-type]
    assert result.exit_code == ExitCode.USAGE_ERROR


def test_compute_unknown_subcommand_stdout_empty() -> None:
    """Usage errors from Typer/Click go to stderr; stdout must stay clean so
    scripts can safely capture stdout as the "real" output channel."""
    result = runner.invoke(cli_app, ["no-such-command"], prog_name="compute")
    assert result.exit_code == ExitCode.USAGE_ERROR
    assert result.stdout == ""


# --- --help works and does not require any extras --------------------------


@pytest.mark.parametrize(
    ("app", "prog"),
    [
        (cli_app, "compute"),
        (controller_app, "meshforge-controller"),
        (agent_app, "meshforge-agent"),
    ],
)
def test_help_exits_zero_and_mentions_prog(app: object, prog: str) -> None:
    result = runner.invoke(app, ["--help"], prog_name=prog)  # type: ignore[arg-type]
    assert result.exit_code == ExitCode.OK
    assert prog in result.output


# --- ASCII-safety: Windows console / POSIX C-locale simulation -------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([sys.executable, "-m", "meshforge.cli", "--version"], ExitCode.OK),
        # `_run_ascii_env` points the CLI at a dead port, so this is exactly
        # "controller unreachable" (D18) on every machine, not "it depends".
        ([sys.executable, "-m", "meshforge.cli", "status"], ExitCode.UNAVAILABLE),
        ([sys.executable, "-m", "meshforge.controller", "--version"], ExitCode.OK),
        ([sys.executable, "-m", "meshforge.controller", "--help"], ExitCode.OK),
        ([sys.executable, "-m", "meshforge.agent", "--version"], ExitCode.OK),
        ([sys.executable, "-m", "meshforge.agent", "--help"], ExitCode.OK),
        ([sys.executable, "-m", "meshforge.agent", "identity", "--help"], ExitCode.OK),
    ],
)
def test_output_is_pure_ascii_under_c_locale(argv: list[str], expected: int) -> None:
    """Every message these commands can print must decode under a plain
    ASCII/cp437-style console, since Windows terminals are not guaranteed to be
    UTF-8 and the task requires ASCII stderr messages. ``compute status`` also
    renders a Rich table now (0.3.12), which is why it uses the ASCII box style."""
    result = _run_ascii_env(*argv)
    combined = result.stdout + result.stderr
    combined.encode("ascii")  # raises UnicodeEncodeError if not pure ASCII
    assert result.returncode == expected, combined


def test_ascii_env_does_not_crash_with_encode_error() -> None:
    """A stronger check: run with encoding actually pinned to ascii, subprocess
    must not itself raise UnicodeEncodeError while writing (returncode would be
    1/traceback, not 0 or the typed UNAVAILABLE of a missing controller)."""
    # `meshforge.controller` / `meshforge.agent run` are real processes since V0.3
    # (G2/G3) and are covered by tests/component instead.
    for argv in ([sys.executable, "-m", "meshforge.cli", "status"],):
        result = _run_ascii_env(*argv)
        assert result.returncode == ExitCode.UNAVAILABLE, result.stderr
        assert "Traceback" not in result.stderr


# --- wheel packaging ---------------------------------------------------------


def test_wheel_contains_py_typed_all_subpackages_and_entry_points(
    tmp_path: Path,
) -> None:
    """`uv build` output is what actually ships; verify py.typed marker, every
    subpackage's __init__.py, and the three console_scripts are present."""
    out_dir = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    wheels = list(out_dir.glob("meshforge-*.whl"))
    assert len(wheels) == 1, wheels
    wheel_path = wheels[0]

    with zipfile.ZipFile(wheel_path) as zf:
        names = zf.namelist()

        assert "meshforge/py.typed" in names

        for pkg in ("common", "controller", "agent", "gpu_service", "sdk", "cli"):
            assert f"meshforge/{pkg}/__init__.py" in names, f"missing {pkg}/__init__.py"

        entry_points_name = next(
            (n for n in names if n.endswith(".dist-info/entry_points.txt")), None
        )
        assert entry_points_name is not None
        entry_points_text = zf.read(entry_points_name).decode()

    for script, module_target in (
        ("compute", "meshforge.cli.app:main"),
        ("meshforge-controller", "meshforge.controller.app:main"),
        ("meshforge-agent", "meshforge.agent.main:main"),
    ):
        assert f"{script} = {module_target}" in entry_points_text


def test_version_string_matches_pyproject_scheme() -> None:
    """`--version` output must follow "<prog> <version>"; the version itself
    must be the dev0 scheme this task specifies, not a stray build tag."""
    assert meshforge.__version__ == "0.1.0.dev0"
