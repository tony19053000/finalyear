"""Task 0.1.7: database settings, URL building and secret handling (no SQLAlchemy)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from meshforge.common.errors import ConfigError
from meshforge.controller.store.settings import DatabaseSettings, StoreSettings

pytestmark = pytest.mark.unit

PASSWORD = "s3cr3t-p@ss:/word"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def test_defaults_match_dev_compose() -> None:
    db = StoreSettings.load().database
    assert (db.host, db.port, db.name, db.user) == (
        "127.0.0.1",
        5432,
        "meshforge",
        "meshforge",
    )
    assert db.url_secret().get_secret_value() == (
        "postgresql+asyncpg://meshforge:meshforge-dev@127.0.0.1:5432/meshforge"
    )
    assert db.display_url() == (
        "postgresql+asyncpg://meshforge:***@127.0.0.1:5432/meshforge"
    )


def test_env_vars_use_meshforge_database_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MESHFORGE_DATABASE__HOST", "db.lan")
    monkeypatch.setenv("MESHFORGE_DATABASE__PORT", "6543")
    monkeypatch.setenv("MESHFORGE_DATABASE__PASSWORD", PASSWORD)
    db = StoreSettings.load().database
    assert db.host == "db.lan"
    assert db.port == 6543
    assert db.password.get_secret_value() == PASSWORD


def test_toml_database_section(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('[database]\nname = "mf_test"\npool_size = 2\n', encoding="utf-8")
    db = StoreSettings.load(cfg).database
    assert db.name == "mf_test"
    assert db.pool_size == 2


def test_password_special_chars_are_url_encoded() -> None:
    db = DatabaseSettings(password=SecretStr(PASSWORD), user="u@x")
    raw = db.url_secret().get_secret_value()
    assert "s3cr3t-p%40ss%3A%2Fword" in raw
    assert "u%40x:" in raw
    assert PASSWORD not in db.display_url()
    assert "%40ss" not in db.display_url()


def test_ipv6_host_is_bracketed() -> None:
    db = DatabaseSettings(host="::1")
    assert "@[::1]:5432/" in db.url_secret().get_secret_value()


@pytest.mark.parametrize(
    "given",
    [
        "postgresql://u:pw@h:5432/db",
        "postgres://u:pw@h:5432/db",
        "postgresql+asyncpg://u:pw@h:5432/db",
        "  POSTGRESQL://u:pw@h:5432/db  ",
    ],
)
def test_url_is_normalised_to_asyncpg(given: str) -> None:
    db = DatabaseSettings(url=SecretStr(given))
    assert db.url_secret().get_secret_value() == "postgresql+asyncpg://u:pw@h:5432/db"
    assert db.display_url() == "postgresql+asyncpg://u:***@h:5432/db"


def test_url_overrides_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESHFORGE_DATABASE__URL", "postgresql://a:b@other:1/x")
    monkeypatch.setenv("MESHFORGE_DATABASE__HOST", "ignored")
    db = StoreSettings.load().database
    assert db.display_url() == "postgresql+asyncpg://a:***@other:1/x"


def test_url_without_password_displays_unchanged() -> None:
    db = DatabaseSettings(url=SecretStr("postgresql://u@h/db"))
    assert db.display_url() == "postgresql+asyncpg://u@h/db"


# Passwords inside a URL must be percent-encoded, so these use a URL-safe secret.
URL_SECRET = "s3cr3tUrlPw"


@pytest.mark.parametrize(
    "bad",
    [
        f"mysql://u:{URL_SECRET}@h/db",
        f"postgresql+psycopg://u:{URL_SECRET}@h/db",
        f"u:{URL_SECRET}@h/db",
        f"postgresql://u:{URL_SECRET}@/db",
        f"postgresql://u:{URL_SECRET}@h:5432/",
    ],
)
def test_invalid_url_rejected_without_leaking_it(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("MESHFORGE_DATABASE__URL", bad)
    with pytest.raises(ConfigError) as info:
        StoreSettings.load()
    rendered = f"{info.value} {info.value.details!r} {info.value!r}"
    assert "database.url" in rendered
    assert URL_SECRET not in rendered
    assert "s3cr3t" not in rendered


def test_unknown_key_in_database_section_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text('[database]\nhots = "typo"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"database\.hots"):
        StoreSettings.load(cfg)


@pytest.mark.parametrize("port", ["0", "70000", "abc"])
def test_bad_port_rejected(monkeypatch: pytest.MonkeyPatch, port: str) -> None:
    monkeypatch.setenv("MESHFORGE_DATABASE__PORT", port)
    with pytest.raises(ConfigError, match=r"database\.port"):
        StoreSettings.load()


def test_password_and_url_masked_in_repr() -> None:
    db = DatabaseSettings(
        password=SecretStr(PASSWORD), url=SecretStr(f"postgresql://u:{PASSWORD}@h/db")
    )
    settings = StoreSettings(database=db)
    for text in (repr(db), str(db), repr(settings), str(settings.model_dump())):
        assert PASSWORD not in text
        assert "s3cr3t" not in text


def test_settings_module_does_not_import_sqlalchemy() -> None:
    """settings.py must stay importable on a base install (no controller extra)."""
    import subprocess
    import sys

    code = (
        "import sys, meshforge.controller.store.settings;"
        "assert 'sqlalchemy' not in sys.modules, 'sqlalchemy imported'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
