"""Task 0.1.3: package skeleton and entry points."""

import importlib
import os
import shutil
import subprocess
import sys

import pytest
from typer.testing import CliRunner

import meshforge
from meshforge.agent.main import app as agent_app
from meshforge.cli.app import app as cli_app
from meshforge.common.exit_codes import ExitCode
from meshforge.controller.app import app as controller_app

pytestmark = pytest.mark.unit

SUBPACKAGES = ["common", "controller", "agent", "gpu_service", "sdk", "cli"]
SCRIPTS = {
    "compute": "meshforge.cli",
    "meshforge-controller": "meshforge.controller",
    "meshforge-agent": "meshforge.agent",
}
# Modules from optional extras that must not load when entry points are imported.
HEAVY_MODULES = [
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "asyncpg",
    "psutil",
    "pynvml",
    "torch",
]

runner = CliRunner()


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_importable_with_docstring(name: str) -> None:
    module = importlib.import_module(f"meshforge.{name}")
    assert module.__doc__ and "import" in module.__doc__


def test_version_is_real() -> None:
    assert meshforge.__version__ != "0.0.0+unknown"


def test_compute_version_via_runner() -> None:
    result = runner.invoke(cli_app, ["--version"], prog_name="compute")
    assert result.exit_code == ExitCode.OK
    assert result.stdout.strip() == f"compute {meshforge.__version__}"


def test_compute_no_args_shows_help() -> None:
    result = runner.invoke(cli_app, [], prog_name="compute")
    assert "status" in result.output


def test_compute_status_reports_an_unreachable_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``status`` stopped being a NOT_IMPLEMENTED stub in V0.3 G5 (roadmap 0.3.12).

    Port 1 is chosen so the connection is refused immediately on every platform
    and the assertion cannot depend on whether a controller happens to be running
    on this machine.
    """
    monkeypatch.setenv("MESHFORGE_CLI__CONTROLLER_URL", "http://127.0.0.1:1")
    result = runner.invoke(cli_app, ["status"], prog_name="compute")
    assert result.exit_code == ExitCode.UNAVAILABLE
    assert "reachable" in result.stdout
    assert "Traceback" not in result.output


def test_compute_unknown_command_is_usage_error() -> None:
    result = runner.invoke(cli_app, ["no-such-command"], prog_name="compute")
    assert result.exit_code == ExitCode.USAGE_ERROR


@pytest.mark.parametrize(
    ("app", "prog"),
    [(controller_app, "meshforge-controller"), (agent_app, "meshforge-agent")],
)
def test_process_entry_points_via_runner(app: object, prog: str) -> None:
    result = runner.invoke(app, ["--version"], prog_name=prog)  # type: ignore[arg-type]
    assert result.exit_code == ExitCode.OK
    assert result.stdout.strip() == f"{prog} {meshforge.__version__}"


def test_agent_entry_point_lists_its_commands() -> None:
    # `run` and `identity` are real since V0.3 G3 (`run` needs a controller, so it
    # is exercised in tests/component/agent); no arguments prints the help.
    result = runner.invoke(agent_app, [], prog_name="meshforge-agent")
    assert result.exit_code in (ExitCode.OK, ExitCode.USAGE_ERROR)
    assert "run" in result.output
    assert "identity" in result.output


@pytest.mark.parametrize("script", list(SCRIPTS))
def test_installed_script_version(script: str) -> None:
    path = shutil.which(script)
    assert path is not None, f"{script} not on PATH; run `uv sync`"
    result = _run(path, "--version")
    assert result.returncode == ExitCode.OK, result.stderr
    assert result.stdout.strip() == f"{script} {meshforge.__version__}"


@pytest.mark.parametrize(("script", "module"), list(SCRIPTS.items()))
def test_python_dash_m(script: str, module: str) -> None:
    result = _run(sys.executable, "-m", module, "--version")
    assert result.returncode == ExitCode.OK, result.stderr
    assert result.stdout.strip() == f"{script} {meshforge.__version__}"


def test_installed_compute_exits_unavailable_without_a_controller() -> None:
    """The installed console script, as an operator would run it: one typed line,
    exit 6, no traceback (roadmap 0.3.12, decision D18)."""
    path = shutil.which("compute")
    assert path is not None
    env = dict(os.environ, MESHFORGE_CLI__CONTROLLER_URL="http://127.0.0.1:1")
    result = subprocess.run(
        [path, "nodes"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )
    assert result.returncode == ExitCode.UNAVAILABLE
    assert "cannot reach the controller" in result.stderr
    assert "Traceback" not in result.stderr


def test_entry_points_do_not_import_extras() -> None:
    code = (
        "import sys\n"
        "import meshforge.cli.app, meshforge.controller.app, meshforge.agent.main\n"
        "import meshforge.common, meshforge.sdk, meshforge.gpu_service\n"
        f"heavy = [m for m in {HEAVY_MODULES!r} if m in sys.modules]\n"
        "print(','.join(heavy))\n"
    )
    result = _run(sys.executable, "-c", code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
