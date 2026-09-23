"""Component test for task 0.1.3: base install (`uv sync --locked --no-dev`, no
extras) must give three fully working console scripts without pulling in any
optional-dependency package (fastapi/psutil/torch/sqlalchemy/...).

This copies the repository into an isolated temp directory (its own
UV_CACHE_DIR too) so it never touches the developer's `.venv`, and is a true
end-to-end reproduction of what a fresh clone + `uv sync` (no extras) gives a
user. Real dependency, real subprocesses, real `uv` — no mocks.

Never modifies `src/`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.component

REPO_ROOT = Path(__file__).resolve().parents[2]

HEAVY_PACKAGES = [
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "asyncpg",
    "alembic",
    "zeroconf",
    "psutil",
    "nvidia-ml-py",
    "torch",
]

COPY_EXCLUDE = {
    ".venv",
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
}


def _copy_repo(dest: Path) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n in COPY_EXCLUDE}

    shutil.copytree(REPO_ROOT, dest, ignore=ignore)


@pytest.fixture(scope="module")
def base_install(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("meshforge-base-install")
    project_dir = root / "clean"
    cache_dir = root / "uv-cache"
    _copy_repo(project_dir)

    env = dict(os.environ)
    env["UV_CACHE_DIR"] = str(cache_dir)

    result = subprocess.run(
        ["uv", "sync", "--locked", "--no-dev"],
        cwd=project_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, (
        f"`uv sync --locked --no-dev` failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    return project_dir


def _venv_bin(project_dir: Path, name: str) -> Path:
    return project_dir / ".venv" / "bin" / name


def test_base_install_has_no_heavy_extra_packages(base_install: Path) -> None:
    env = dict(os.environ)
    env["VIRTUAL_ENV"] = str(base_install / ".venv")
    result = subprocess.run(
        ["uv", "pip", "list", "--format=freeze"],
        cwd=base_install,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    installed = {
        line.split("==")[0].lower() for line in result.stdout.splitlines() if line
    }
    leaked = installed & {p.lower() for p in HEAVY_PACKAGES}
    assert not leaked, f"controller/agent/gpu extras leaked into base install: {leaked}"


@pytest.mark.parametrize(
    "script", ["compute", "meshforge-controller", "meshforge-agent"]
)
def test_base_install_script_version_works(base_install: Path, script: str) -> None:
    exe = _venv_bin(base_install, script)
    assert exe.exists(), f"{script} console script missing from base install"
    result = subprocess.run(
        [str(exe), "--version"], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{script} 0.1.0.dev0"
    assert result.stderr == ""


def test_base_install_compute_talks_rest_with_no_extra(
    base_install: Path, tmp_path: Path
) -> None:
    """Since V0.3 G5 `compute` is real and needs **no** extra: it speaks REST, and
    httpx is a core dependency (decision D15). With nothing listening it reports
    the typed UNAVAILABLE (6), never a traceback and never an ImportError.

    Port 1 is refused immediately on every platform, so the result does not depend
    on whether a controller happens to run on this machine.
    """
    exe = _venv_bin(base_install, "compute")
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env["XDG_CONFIG_HOME"] = str(tmp_path)
    env["APPDATA"] = str(tmp_path)
    env["MESHFORGE_CLI__CONTROLLER_URL"] = "http://127.0.0.1:1"
    result = subprocess.run(
        [str(exe), "nodes"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    assert result.returncode == 6, result.stderr
    assert result.stdout == ""
    assert "cannot reach the controller" in result.stderr
    assert "Traceback" not in result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_base_install_controller_run_needs_extra(
    base_install: Path, tmp_path: Path
) -> None:
    """Since V0.3 G2 `meshforge-controller` is a real server; on a base install it
    must fail fast with a typed CONFIG_ERROR naming the missing extra, not a
    traceback (settings load first, so an unreadable user config would win)."""
    exe = _venv_bin(base_install, "meshforge-controller")
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env["XDG_CONFIG_HOME"] = str(tmp_path)
    env["APPDATA"] = str(tmp_path)
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30, check=False, env=env
    )
    assert result.returncode == 4, result.stderr
    assert result.stdout == ""
    assert "controller extra" in result.stderr
    assert "Traceback" not in result.stderr


def test_base_install_python_dash_m_works(base_install: Path) -> None:
    python = _venv_bin(base_install, "python")
    for module, prog in (
        ("meshforge.cli", "compute"),
        ("meshforge.controller", "meshforge-controller"),
        ("meshforge.agent", "meshforge-agent"),
    ):
        result = subprocess.run(
            [str(python), "-m", module, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"{prog} 0.1.0.dev0"
