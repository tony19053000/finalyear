"""``meshforge-controller token show|rotate`` (D44): stdout is the token, nothing else.

Also pins that the bare ``meshforge-controller`` form is still the "run" action
(the service units and harnesses call it without a subcommand).
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meshforge.common.errors import ExitCode
from meshforge.common.fabric_token import parse_token
from meshforge.controller import app as controller_app


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"data_dir = '{(tmp_path / 'data').as_posix()}'\n")
    return cfg


def _invoke(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(controller_app.app, list(args))
    return result.exit_code, result.stdout, result.stderr


def test_show_creates_then_repeats_the_same_token(tmp_path: Path, config: Path) -> None:
    code, out, _ = _invoke("token", "show", "--config", str(config))
    assert code == ExitCode.OK
    token = out.strip()
    parse_token(token)
    assert out == token + "\n"  # only the token on stdout
    path = tmp_path / "data" / "controller" / "fabric.token"
    assert path.read_text().strip() == token
    if not sys.platform.startswith("win"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    code, again, _ = _invoke("token", "show", "--config", str(config))
    assert again.strip() == token


def test_rotate_prints_a_new_token(config: Path) -> None:
    _, first, _ = _invoke("token", "show", "--config", str(config))
    code, rotated, err = _invoke("token", "rotate", "--config", str(config))
    assert code == ExitCode.OK
    assert rotated.strip() != first.strip()
    assert rotated.strip() not in err
    _, shown, _ = _invoke("token", "show", "--config", str(config))
    assert shown == rotated


def test_corrupt_token_file_is_a_config_error(tmp_path: Path, config: Path) -> None:
    path = tmp_path / "data" / "controller" / "fabric.token"
    path.parent.mkdir(parents=True)
    path.write_text("garbage")
    if not sys.platform.startswith("win"):
        path.chmod(0o600)
    code, out, err = _invoke("token", "show", "--config", str(config))
    assert code == ExitCode.CONFIG_ERROR
    assert out == ""
    assert "URL-safe" in err


def test_token_without_subcommand_prints_help() -> None:
    code, out, _ = _invoke("token")
    assert code in (ExitCode.OK, ExitCode.USAGE_ERROR)
    assert "show" in out and "rotate" in out


def test_help_lists_token_and_bare_form_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, _ = _invoke("--help")
    assert code == ExitCode.OK
    assert "token" in out
    called: list[object] = []

    def fake_run(settings: object) -> int:
        called.append(settings)
        return 0

    import meshforge.controller.server as server

    monkeypatch.setattr(server, "run_controller", fake_run)
    code, _, _ = _invoke()
    assert code == ExitCode.OK
    assert len(called) == 1
