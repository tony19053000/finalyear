"""Task 0.1.6: settings loading (TOML + env), precedence, platform dirs."""

import os
from collections.abc import Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from pydantic import SecretStr, ValidationError

from meshforge.common.config import (
    LoggingSettings,
    MeshForgeSettings,
    SectionModel,
    default_config_file,
    default_data_dir,
    platform_dirs,
    resolve_config_file,
)
from meshforge.common.errors import ConfigError, FailureType
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit


class _AgentSection(SectionModel):
    port: int = 7000
    join_token: SecretStr | None = None


class _AgentSettings(MeshForgeSettings):
    agent: _AgentSection = _AgentSection()


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    yield


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_any_file() -> None:
    s = MeshForgeSettings.load()
    assert s.log == LoggingSettings(level="INFO", format="json")
    assert s.data_dir == default_data_dir()


def test_toml_overrides_defaults(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "c.toml", '[log]\nlevel = "debug"\nformat = "console"\n')
    s = MeshForgeSettings.load(cfg)
    assert (s.log.level, s.log.format) == ("DEBUG", "console")


def test_env_overrides_toml_and_nested_sections_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write(tmp_path / "c.toml", '[log]\nlevel = "ERROR"\nformat = "console"\n')
    monkeypatch.setenv("MESHFORGE_LOG__LEVEL", "WARNING")
    s = MeshForgeSettings.load(cfg)
    assert (s.log.level, s.log.format) == ("WARNING", "console")


def test_init_kwargs_override_env_and_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write(
        tmp_path / "c.toml", f'data_dir = "{(tmp_path / "toml").as_posix()}"\n'
    )
    monkeypatch.setenv("MESHFORGE_DATA_DIR", str(tmp_path / "env"))
    assert MeshForgeSettings.load(cfg).data_dir == tmp_path / "env"
    s = MeshForgeSettings.load(cfg, data_dir=tmp_path / "init")
    assert s.data_dir == tmp_path / "init"


def test_config_file_from_env_var_and_explicit_argument_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_cfg = _write(tmp_path / "env.toml", '[log]\nlevel = "ERROR"\n')
    arg_cfg = _write(tmp_path / "arg.toml", '[log]\nlevel = "DEBUG"\n')
    monkeypatch.setenv("MESHFORGE_CONFIG", str(env_cfg))
    assert MeshForgeSettings.load().log.level == "ERROR"
    assert (
        MeshForgeSettings().log.level == "ERROR"
    )  # direct construction uses same sources
    assert MeshForgeSettings.load(arg_cfg).log.level == "DEBUG"


def test_default_config_file_is_used_when_present() -> None:
    _write(default_config_file(), '[log]\nformat = "console"\n')
    assert resolve_config_file() == (default_config_file(), False)
    assert MeshForgeSettings.load().log.format == "console"


def test_missing_explicit_file_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ConfigError, match="not found"):
        MeshForgeSettings.load(tmp_path / "missing.toml")
    monkeypatch.setenv("MESHFORGE_CONFIG", str(tmp_path / "missing2.toml"))
    with pytest.raises(ConfigError, match="missing2.toml"):
        MeshForgeSettings.load()


def test_invalid_toml_is_a_config_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "bad.toml", "[log\nlevel = \n")
    with pytest.raises(ConfigError, match="not valid TOML") as info:
        MeshForgeSettings.load(cfg)
    assert info.value.details["config_file"] == str(cfg)


def test_validation_error_is_clear_typed_and_hides_input(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "c.toml", '[agent]\nport = "p@ss-not-a-port"\n')
    with pytest.raises(ConfigError) as info:
        _AgentSettings.load(cfg)
    err = info.value
    assert "agent.port" in str(err)
    assert "_AgentSettings" in str(err)
    assert str(cfg) in str(err)
    assert "p@ss-not-a-port" not in str(err)
    assert "p@ss-not-a-port" not in repr(err.details)
    assert err.__cause__ is None and err.__suppress_context__
    assert err.failure_type is FailureType.CONFIG_ERROR
    assert err.exit_code is ExitCode.CONFIG_ERROR
    assert err.details["errors"][0]["loc"] == "agent.port"


def test_invalid_env_value_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESHFORGE_LOG__LEVEL", "LOUD")
    with pytest.raises(ConfigError, match=r"log\.level"):
        MeshForgeSettings.load()


def test_unknown_section_keys_rejected_but_unknown_top_level_ignored(
    tmp_path: Path,
) -> None:
    shared = _write(
        tmp_path / "shared.toml", '[controller]\nport = 1\n[log]\nlevel = "INFO"\n'
    )
    assert MeshForgeSettings.load(shared).log.level == "INFO"
    typo = _write(tmp_path / "typo.toml", '[log]\nlevle = "DEBUG"\n')
    with pytest.raises(ConfigError, match=r"log\.levle"):
        MeshForgeSettings.load(typo)


def test_secrets_from_env_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESHFORGE_AGENT__JOIN_TOKEN", "tok-very-secret")
    monkeypatch.setenv("MESHFORGE_AGENT__PORT", "7443")
    s = _AgentSettings.load()
    assert s.agent.port == 7443
    assert s.agent.join_token is not None
    assert s.agent.join_token.get_secret_value() == "tok-very-secret"
    assert "tok-very-secret" not in repr(s)
    assert "tok-very-secret" not in str(s.model_dump())


def test_settings_are_immutable() -> None:
    s = MeshForgeSettings.load()
    with pytest.raises(ValidationError, match="frozen"):
        s.data_dir = Path("/elsewhere")


# ---------------------------------------------------------------- platform dirs


def test_linux_xdg_dirs() -> None:
    dirs = platform_dirs(
        "linux", {"XDG_CONFIG_HOME": "/cfg", "XDG_DATA_HOME": "/data"}, home="/home/u"
    )
    assert dirs.config_dir == PurePosixPath("/cfg/meshforge")
    assert dirs.data_dir == PurePosixPath("/data/meshforge")


def test_linux_defaults_and_relative_xdg_ignored() -> None:
    dirs = platform_dirs("linux", {"XDG_CONFIG_HOME": "relative/cfg"}, home="/home/u")
    assert dirs.config_dir == PurePosixPath("/home/u/.config/meshforge")
    assert dirs.data_dir == PurePosixPath("/home/u/.local/share/meshforge")


def test_windows_appdata_dirs() -> None:
    env = {
        "APPDATA": r"C:\Users\Asha\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\Asha\AppData\Local",
    }
    dirs = platform_dirs("win32", env)
    assert isinstance(dirs.config_dir, PureWindowsPath)
    assert dirs.config_dir == PureWindowsPath(
        r"C:\Users\Asha\AppData\Roaming\MeshForge"
    )
    assert dirs.data_dir == PureWindowsPath(r"C:\Users\Asha\AppData\Local\MeshForge")


def test_windows_fallback_to_userprofile() -> None:
    dirs = platform_dirs("win32", {"USERPROFILE": r"D:\Profiles\ravi"})
    assert dirs.config_dir == PureWindowsPath(
        r"D:\Profiles\ravi\AppData\Roaming\MeshForge"
    )
    assert dirs.data_dir == PureWindowsPath(r"D:\Profiles\ravi\AppData\Local\MeshForge")


def test_macos_dirs() -> None:
    dirs = platform_dirs("darwin", {}, home="/Users/k")
    assert dirs.config_dir == PurePosixPath(
        "/Users/k/Library/Application Support/MeshForge"
    )
    assert dirs.data_dir == dirs.config_dir
