"""V0.3 G5 unit tests for the ``[cli]`` settings section (roadmap 0.3.12).

Precedence, bounds and the URL validator. Nothing here opens a socket; the
autouse fixture in ``conftest.py`` clears ``MESHFORGE_*`` and repoints the config
directory into ``tmp_path``, so a developer's own ``config.toml`` cannot change a
result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.cli.settings import (
    DEFAULT_CONTROLLER_URL,
    MAX_TIMEOUT_S,
    CliSection,
    CliSettings,
    normalise_controller_url,
)
from meshforge.common.errors import ConfigError

pytestmark = pytest.mark.unit


def test_the_defaults_are_the_documented_loopback_rest_port() -> None:
    settings = CliSettings.load()
    assert settings.cli.controller_url == DEFAULT_CONTROLLER_URL
    assert settings.cli.controller_url.endswith(":47110")
    assert settings.cli.timeout_s == 5.0
    assert settings.cli.watch_interval_s == 2.0


def test_the_environment_overrides_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESHFORGE_CLI__CONTROLLER_URL", "https://192.168.1.20:47110")
    monkeypatch.setenv("MESHFORGE_CLI__TIMEOUT_S", "1.5")
    monkeypatch.setenv("MESHFORGE_CLI__WATCH_INTERVAL_S", "10")
    settings = CliSettings.load()
    assert settings.cli.controller_url == "https://192.168.1.20:47110"
    assert settings.cli.timeout_s == 1.5
    assert settings.cli.watch_interval_s == 10.0


def test_a_toml_file_is_read_and_the_environment_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[cli]\ncontroller_url = "https://from-file:47110"\ntimeout_s = 9.0\n',
        encoding="utf-8",
    )
    assert CliSettings.load(config).cli.controller_url == "https://from-file:47110"
    assert CliSettings.load(config).cli.timeout_s == 9.0
    monkeypatch.setenv("MESHFORGE_CLI__CONTROLLER_URL", "https://from-env:47110")
    assert CliSettings.load(config).cli.controller_url == "https://from-env:47110"


def test_an_unknown_key_inside_the_section_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match=r"contorller_url|extra"):
        CliSettings(cli={"contorller_url": "https://127.0.0.1:47110"})  # type: ignore[arg-type]


# -------------------------------------------------------------- the URL rules


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://127.0.0.1:47110", "https://127.0.0.1:47110"),
        ("https://127.0.0.1:47110/", "https://127.0.0.1:47110"),
        ("HTTP://Host:47110", "http://Host:47110"),
        ("https://ctl.lan", "https://ctl.lan"),
        ("  http://ctl.lan:47110  ", "http://ctl.lan:47110"),
        ("http://[::1]:47110", "http://[::1]:47110"),
        ("http://ctl.lan/base/", "http://ctl.lan/base"),
    ],
)
def test_an_acceptable_url_is_normalised(raw: str, expected: str) -> None:
    assert normalise_controller_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "127.0.0.1:47110",  # no scheme: urlsplit would read the host as one
        "//127.0.0.1:47110",
        "ftp://ctl.lan",
        "grpc://ctl.lan:47100",
        "http://",
        "http:///v1/nodes",
        "http://ctl.lan?x=1",
        "http://ctl.lan#frag",
    ],
)
def test_a_url_without_a_scheme_or_host_is_a_config_error(raw: str) -> None:
    with pytest.raises(ConfigError):
        normalise_controller_url(raw)


def test_a_bad_url_given_as_an_argument_is_a_config_error_too() -> None:
    with pytest.raises(ConfigError, match="controller_url"):
        CliSettings(cli={"controller_url": "ftp://ctl.lan"})  # type: ignore[arg-type]


def test_a_bad_url_from_the_environment_surfaces_as_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MESHFORGE_CLI__CONTROLLER_URL", "127.0.0.1:47110")
    with pytest.raises(ConfigError) as info:
        CliSettings.load()
    assert "cli.controller_url" in str(info.value)


# ------------------------------------------------------------------- bounds


@pytest.mark.parametrize("timeout", [0.0, -1.0, MAX_TIMEOUT_S + 1])
def test_a_timeout_outside_the_range_is_a_config_error(timeout: float) -> None:
    """Through ``CliSettings``, which is what a command calls: the section itself
    raises Pydantic's ``ValidationError``, and ``MeshForgeSettings`` is the layer
    that turns it into a ``ConfigError`` with the sources named."""
    with pytest.raises(ConfigError, match="timeout_s"):
        CliSettings(cli={"timeout_s": timeout})  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [0.001, MAX_TIMEOUT_S])
def test_the_timeout_boundaries_are_accepted(timeout: float) -> None:
    assert CliSection(timeout_s=timeout).timeout_s == timeout


@pytest.mark.parametrize("interval", [0.0, 0.49, 61.0])
def test_a_watch_interval_outside_the_range_is_a_config_error(interval: float) -> None:
    with pytest.raises(ConfigError, match="watch_interval_s"):
        CliSettings(cli={"watch_interval_s": interval})  # type: ignore[arg-type]


def test_the_section_is_frozen_so_a_command_cannot_mutate_settings() -> None:
    section = CliSection()
    with pytest.raises(Exception, match=r"frozen|immutable"):
        section.timeout_s = 1.0  # type: ignore[misc]


def test_every_field_is_documented_where_it_is_defined() -> None:
    """CLAUDE.md §7 / the checklist: settings carry their own description."""
    for name, field in CliSection.model_fields.items():
        assert field.description, f"{name} has no description="
        assert "MESHFORGE_CLI__" in (field.description or ""), name
