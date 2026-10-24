"""V0.17 G1 (D385, Q11): ``MESHFORGE_INSTALL_MODE=system`` -> machine-wide dirs.

The system variant is computed for both operating systems on any OS (pure paths),
so the Windows layout G4 reuses is pinned here too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from meshforge.common.config import (
    INSTALL_MODE_ENV_VAR,
    InstallMode,
    MeshForgeSettings,
    default_config_file,
    default_data_dir,
    install_mode,
    platform_dirs,
    resolve_config_file,
)
from meshforge.common.errors import ConfigError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


@pytest.mark.parametrize("value", ["", "  ", "user", "USER"])
def test_unset_empty_or_user_is_user_mode(value: str) -> None:
    assert install_mode({INSTALL_MODE_ENV_VAR: value}) is InstallMode.USER
    assert install_mode({}) is InstallMode.USER


@pytest.mark.parametrize("value", ["system", "System", " system "])
def test_system_mode_is_read_case_insensitively(value: str) -> None:
    assert install_mode({INSTALL_MODE_ENV_VAR: value}) is InstallMode.SYSTEM


def test_unknown_mode_is_a_config_error_without_the_value() -> None:
    with pytest.raises(ConfigError) as info:
        install_mode({INSTALL_MODE_ENV_VAR: "machine-wide-secretish"})
    assert INSTALL_MODE_ENV_VAR in str(info.value)
    assert "machine-wide-secretish" not in str(info.value)


def test_linux_system_dirs_ignore_xdg_and_home() -> None:
    env = {
        INSTALL_MODE_ENV_VAR: "system",
        "XDG_CONFIG_HOME": "/cfg",
        "XDG_DATA_HOME": "/data",
    }
    dirs = platform_dirs("linux", env, home="/home/u")
    assert dirs.config_dir == PurePosixPath("/etc/meshforge")
    assert dirs.data_dir == PurePosixPath("/var/lib/meshforge")


def test_windows_system_dirs_use_programdata() -> None:
    env = {
        INSTALL_MODE_ENV_VAR: "system",
        "PROGRAMDATA": r"D:\ProgramData",
        "LOCALAPPDATA": r"C:\Users\Asha\AppData\Local",
    }
    dirs = platform_dirs("win32", env)
    assert isinstance(dirs.data_dir, PureWindowsPath)
    assert dirs.data_dir == PureWindowsPath(r"D:\ProgramData\MeshForge")
    assert dirs.config_dir == PureWindowsPath(r"D:\ProgramData\MeshForge\config")


@pytest.mark.parametrize("program_data", [None, "", "relative\\pd"])
def test_windows_system_dirs_fall_back_to_c_programdata(
    program_data: str | None,
) -> None:
    env = {INSTALL_MODE_ENV_VAR: "system"}
    if program_data is not None:
        env["PROGRAMDATA"] = program_data
    dirs = platform_dirs("win32", env)
    assert dirs.data_dir == PureWindowsPath(r"C:\ProgramData\MeshForge")


def test_mode_argument_overrides_the_environment() -> None:
    env = {INSTALL_MODE_ENV_VAR: "system"}
    user = platform_dirs("linux", env, home="/home/u", mode=InstallMode.USER)
    assert user.data_dir == PurePosixPath("/home/u/.local/share/meshforge")
    system = platform_dirs("linux", {}, home="/home/u", mode=InstallMode.SYSTEM)
    assert system.data_dir == PurePosixPath("/var/lib/meshforge")


def test_macos_has_no_system_mode() -> None:
    with pytest.raises(ConfigError, match="macOS"):
        platform_dirs("darwin", {}, home="/Users/k", mode=InstallMode.SYSTEM)


def test_user_mode_is_unchanged_by_the_feature() -> None:
    dirs = platform_dirs("linux", {}, home="/home/u")
    assert dirs == platform_dirs("linux", {}, home="/home/u", mode=InstallMode.USER)
    assert dirs.data_dir == PurePosixPath("/home/u/.local/share/meshforge")


def _expected_system() -> tuple[Path, Path]:
    dirs = platform_dirs(sys.platform, mode=InstallMode.SYSTEM)
    return Path(dirs.config_dir), Path(dirs.data_dir)


@pytest.mark.skipif(sys.platform == "darwin", reason="no system mode on macOS")
def test_settings_default_to_system_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(INSTALL_MODE_ENV_VAR, "system")
    config_dir, data_dir = _expected_system()
    assert default_data_dir() == data_dir
    assert default_config_file() == config_dir / "config.toml"
    assert MeshForgeSettings.load().data_dir == data_dir


@pytest.mark.skipif(sys.platform == "darwin", reason="no system mode on macOS")
def test_explicit_settings_still_win_in_system_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """System mode only changes *defaults*: ``MESHFORGE_DATA_DIR`` and an explicit
    ``--config`` keep their precedence (tests and power users rely on it)."""
    monkeypatch.setenv(INSTALL_MODE_ENV_VAR, "system")
    monkeypatch.setenv("MESHFORGE_DATA_DIR", str(tmp_path / "d"))
    assert MeshForgeSettings.load().data_dir == tmp_path / "d"
    cfg = tmp_path / "c.toml"
    cfg.write_text("", encoding="utf-8")
    assert resolve_config_file(cfg) == (cfg, True)


def test_install_mode_is_not_a_settings_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MESHFORGE_INSTALL_MODE`` shares the env prefix but is not a field: the
    settings model must ignore it rather than refuse it."""
    monkeypatch.setenv(INSTALL_MODE_ENV_VAR, "user")
    MeshForgeSettings.load()
