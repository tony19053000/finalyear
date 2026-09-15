"""Adversarial tests for task 0.1.6 config: precedence, secrets, cross-platform edges."""

from __future__ import annotations

import codecs
import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from meshforge.common.config import MeshForgeSettings, SectionModel
from meshforge.common.errors import ConfigError

pytestmark = pytest.mark.unit


def _clear_meshforge_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("MESHFORGE_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_meshforge_env(monkeypatch)


# --------------------------------------------------------------------- secret errors


class _DbSection(SectionModel):
    password: SecretStr


class _SettingsWithSecret(MeshForgeSettings):
    db: _DbSection


class _PortSection(SectionModel):
    port: int


class _SettingsWithPort(MeshForgeSettings):
    db: _PortSection


def test_config_error_never_echoes_secretstr_input_value_from_sibling_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validation error on one field must not echo an unrelated SecretStr value
    that was also supplied on the same settings object.
    """
    monkeypatch.setenv("MESHFORGE_DB__PASSWORD", "hunter2-actual-secret")
    monkeypatch.setenv("MESHFORGE_LOG__LEVEL", "NOT_A_REAL_LEVEL")

    class _Both(_SettingsWithSecret):
        pass

    with pytest.raises(ConfigError) as exc_info:
        _Both.load()
    assert "hunter2-actual-secret" not in str(exc_info.value)
    assert "hunter2-actual-secret" not in repr(exc_info.value.details)


def test_config_error_message_hides_invalid_input_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_looking_value = "not-a-number-hunter2-secret"
    monkeypatch.setenv("MESHFORGE_DB__PORT", secret_looking_value)
    with pytest.raises(ConfigError) as exc_info:
        _SettingsWithPort.load()
    assert secret_looking_value not in str(exc_info.value)
    assert secret_looking_value not in repr(exc_info.value.details)


def test_toml_syntax_error_does_not_echo_file_contents(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(
        'password_leak = "hunter2-should-not-appear-in-error"\n[[[bad', encoding="utf-8"
    )
    with pytest.raises(ConfigError) as exc_info:
        MeshForgeSettings.load(config_file=bad)
    assert "hunter2-should-not-appear-in-error" not in str(exc_info.value)


# ------------------------------------------------------------------------ precedence


def test_kwargs_beat_env_beat_toml_beat_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    toml_file = tmp_path / "config.toml"
    toml_file.write_text(
        '[log]\nlevel = "WARNING"\nformat = "console"\n', encoding="utf-8"
    )
    monkeypatch.setenv("MESHFORGE_LOG__LEVEL", "ERROR")

    # TOML alone
    monkeypatch.delenv("MESHFORGE_LOG__LEVEL", raising=False)
    s_toml_only = MeshForgeSettings.load(config_file=toml_file)
    assert s_toml_only.log.level == "WARNING"
    assert s_toml_only.log.format == "console"

    # env beats TOML
    monkeypatch.setenv("MESHFORGE_LOG__LEVEL", "ERROR")
    s_env_beats_toml = MeshForgeSettings.load(config_file=toml_file)
    assert s_env_beats_toml.log.level == "ERROR"
    assert s_env_beats_toml.log.format == "console"  # untouched key still from TOML

    # kwargs beat env and TOML
    s_kwargs_win = MeshForgeSettings.load(config_file=toml_file, log={"level": "DEBUG"})
    assert s_kwargs_win.log.level == "DEBUG"


# --------------------------------------------------------------------- env edge cases


def test_empty_env_var_for_path_field_resolves_to_cwd_not_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEFECT (moderate): an accidentally-empty env var (e.g. a secret-injection
    failure in CI/Docker setting `MESHFORGE_DATA_DIR=""`) silently becomes the
    current working directory (`Path("") == Path(".")`) instead of falling back
    to the documented platform default or raising a ConfigError. This risks
    MeshForge writing state (blob cache, identity) into an arbitrary CWD.
    """
    cwd_marker = Path.cwd()
    monkeypatch.setenv("MESHFORGE_DATA_DIR", "")
    s = MeshForgeSettings.load()
    from meshforge.common.config import default_data_dir

    assert s.data_dir.resolve() != cwd_marker, "empty env var should not resolve to CWD"
    assert s.data_dir == default_data_dir()


def test_explicit_empty_data_dir_is_a_config_error(tmp_path: Path) -> None:
    """Empty ``data_dir`` given explicitly (TOML/kwarg) must not mean CWD."""
    toml_file = tmp_path / "config.toml"
    toml_file.write_text('data_dir = ""\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="data_dir"):
        MeshForgeSettings.load(config_file=toml_file)
    with pytest.raises(ConfigError, match="data_dir"):
        MeshForgeSettings.load(data_dir="  ")


def test_windows_backslash_toml_error_has_hint(tmp_path: Path) -> None:
    for body in (b'data_dir = "C:\\Users\\me"\n', b'data_dir = "C:\\data"\n'):
        naive = tmp_path / "naive.toml"
        naive.write_bytes(body)
        with pytest.raises(ConfigError, match="forward slashes"):
            MeshForgeSettings.load(config_file=naive)


def test_env_var_pointing_at_unknown_nested_section_key_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MESHFORGE_LOG__NOT_A_REAL_KEY", "x")
    with pytest.raises(ConfigError):
        MeshForgeSettings.load()


def test_unicode_env_var_value_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    unicode_path = "/tmp/héllo/wörld/目录"
    monkeypatch.setenv("MESHFORGE_DATA_DIR", unicode_path)
    s = MeshForgeSettings.load()
    assert str(s.data_dir) == unicode_path


# ------------------------------------------------------------------ TOML file quirks


def test_toml_file_with_utf8_bom_is_readable(tmp_path: Path) -> None:
    """DEFECT (minor/cross-platform): `tomllib` does not strip a UTF-8 BOM, and
    Windows editors (Notepad) commonly save UTF-8-with-BOM. A config.toml saved
    this way on a Windows laptop fails to load entirely with a confusing TOML
    syntax error rather than a clear "remove the BOM" message.
    """
    toml_file = tmp_path / "config.toml"
    toml_file.write_bytes(codecs.BOM_UTF8 + b'data_dir = "/tmp/from-toml"\n')
    s = MeshForgeSettings.load(config_file=toml_file)
    assert str(s.data_dir) == "/tmp/from-toml"


def test_toml_windows_path_with_naive_single_backslashes_needs_documentation(
    tmp_path: Path,
) -> None:
    """Not a code bug (TOML spec requires escaping `\\` in basic strings) but a
    real UX trap for Windows users hand-editing config.toml: pasting a raw
    Windows path (`C:\\Users\\foo`) into a double-quoted TOML string fails with a
    generic "Invalid hex value" TOML error. Literal strings (single quotes) work.
    This test documents the trap and pins the working form.
    """
    naive = tmp_path / "naive.toml"
    naive.write_bytes(b'data_dir = "C:\\Users\\foo\\meshforge"\n')
    with pytest.raises(ConfigError):
        MeshForgeSettings.load(config_file=naive)

    literal = tmp_path / "literal.toml"
    literal.write_bytes(b"data_dir = 'C:\\Users\\foo\\meshforge'\n")
    s = MeshForgeSettings.load(config_file=literal)
    assert str(s.data_dir) == "C:\\Users\\foo\\meshforge"


def test_windows_style_forward_slash_path_in_toml_works(tmp_path: Path) -> None:
    toml_file = tmp_path / "config.toml"
    toml_file.write_text('data_dir = "C:/Users/foo/meshforge"\n', encoding="utf-8")
    s = MeshForgeSettings.load(config_file=toml_file)
    assert str(s.data_dir) == "C:/Users/foo/meshforge"


def test_explicit_missing_config_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.toml"
    with pytest.raises(ConfigError):
        MeshForgeSettings.load(config_file=missing)


def test_missing_config_env_var_file_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "does-not-exist.toml"
    monkeypatch.setenv("MESHFORGE_CONFIG", str(missing))
    with pytest.raises(ConfigError):
        MeshForgeSettings.load()


def test_huge_toml_file_top_level_unknown_keys_are_ignored(tmp_path: Path) -> None:
    """Shared config.toml scenario: other components' huge sections must not
    break settings loading for a component that does not know about them.
    """
    toml_file = tmp_path / "config.toml"
    lines = [f"unrelated_key_{i} = {i}\n" for i in range(2000)]
    lines.append('[controller]\nsome_future_key = "x"\n')
    toml_file.write_text("".join(lines), encoding="utf-8")
    s = MeshForgeSettings.load(config_file=toml_file)
    assert isinstance(s, MeshForgeSettings)
