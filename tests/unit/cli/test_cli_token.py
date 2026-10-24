"""``compute`` fabric-token plumbing (D44): settings, env, --token-file, header only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_fabric import ca_file
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli import client as client_module
from meshforge.cli.client import ControllerClient, ControllerError
from meshforge.cli.settings import CliSettings, resolve_token
from meshforge.common.config import default_data_dir
from meshforge.common.errors import ConfigError, ExitCode, FailureType

TOKEN = "T" * 43
OTHER = "O" * 43


def _settings(**cli: Any) -> CliSettings:
    return CliSettings.load(cli=cli) if cli else CliSettings.load()


def test_nothing_configured_means_no_token() -> None:
    assert resolve_token(_settings()) is None


def test_env_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESHFORGE_CLI__TOKEN", TOKEN)
    token = resolve_token(_settings())
    assert token is not None and token.get_secret_value() == TOKEN


def test_malformed_configured_token_is_a_config_error_without_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MESHFORGE_CLI__TOKEN", "short-secret")
    with pytest.raises(ConfigError) as info:
        _settings()
    assert "short-secret" not in info.value.message


def test_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flag_file = tmp_path / "flag.token"
    flag_file.write_text(OTHER + "\n")
    cfg_file = tmp_path / "cfg.token"
    cfg_file.write_text("C" * 43)
    # XDG data dir on Linux, %LOCALAPPDATA%\\MeshForge on Windows (cli conftest)
    local = default_data_dir() / "controller" / "fabric.token"
    local.parent.mkdir(parents=True)
    local.write_text("L" * 43)

    def got(settings: CliSettings, flag: Path | None = None) -> str | None:
        token = resolve_token(settings, flag)
        return token.get_secret_value() if token is not None else None

    both = _settings(token=TOKEN, token_file=str(cfg_file))
    assert got(both, flag_file) == OTHER  # --token-file wins
    assert got(both) == TOKEN  # then [cli] token
    assert got(_settings(token_file=str(cfg_file))) == "C" * 43
    assert got(_settings()) == "L" * 43  # the local controller's own file


def test_named_file_that_is_missing_is_an_error_not_no_token(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        resolve_token(_settings(), tmp_path / "typo.token")
    with pytest.raises(ConfigError, match="not found"):
        resolve_token(_settings(token_file=str(tmp_path / "typo")))


def test_token_is_never_in_settings_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESHFORGE_CLI__TOKEN", TOKEN)
    settings = _settings()
    assert TOKEN not in repr(settings)
    assert TOKEN not in settings.model_dump_json()


def _capturing(seen: list[httpx.Request], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status == 401:
            return httpx.Response(
                401, json={"error": {"code": "unauthorized", "message": "no"}}
            )
        return httpx.Response(200, json={"nodes": [], "count": 0})

    return httpx.MockTransport(handler)


def test_client_sends_the_token_only_as_a_header() -> None:
    seen: list[httpx.Request] = []
    settings = _settings(token=TOKEN)
    with ControllerClient(
        "http://c:1",
        timeout_s=1,
        transport=_capturing(seen),
        token=settings.cli.token,
    ) as client:
        client.nodes()
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(seen[0].url)


def test_client_without_token_sends_no_header() -> None:
    seen: list[httpx.Request] = []
    with ControllerClient("http://c:1", timeout_s=1, transport=_capturing(seen)) as c:
        c.nodes()
    assert "authorization" not in seen[0].headers


def test_401_becomes_a_typed_error_with_a_hint() -> None:
    seen: list[httpx.Request] = []
    with (
        ControllerClient(
            "http://c:1", timeout_s=1, transport=_capturing(seen, 401)
        ) as c,
        pytest.raises(ControllerError) as info,
    ):
        c.nodes()
    assert info.value.failure_type is FailureType.UNAUTHORIZED
    # V0.7 G4 (D141): the hint is now the user login (was `token show`).
    assert "compute login" in info.value.message


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, seen: list[httpx.Request]
) -> None:
    real = client_module.ControllerClient

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = _capturing(seen)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(client_module, "ControllerClient", Recording)


def test_token_file_flag_reaches_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []
    _install_transport(monkeypatch, seen)
    token_file = tmp_path / "t"
    token_file.write_text(TOKEN + "\n")
    result = CliRunner().invoke(
        cli_app.app,
        ["--ca", str(ca_file()), "--token-file", str(token_file), "nodes", "--json"],
    )
    assert result.exit_code == ExitCode.OK, result.output
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in result.output


def test_bad_token_file_flag_exits_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []
    _install_transport(monkeypatch, seen)
    bad = tmp_path / "bad"
    bad.write_text("nope")
    result = CliRunner().invoke(cli_app.app, ["--token-file", str(bad), "nodes"])
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert seen == []
