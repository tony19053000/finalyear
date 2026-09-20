"""V0.3 G3 (0.3.8 E, ADR-0013): ``meshforge-agent identity show|reset`` and the
``run`` command's configuration errors, through Typer's runner (no network)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meshforge.agent.identity import load_or_create
from meshforge.agent.main import app
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

runner = CliRunner()
PROG = "meshforge-agent"


@pytest.fixture(autouse=True)
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MESHFORGE_DATA_DIR", str(data_dir))
    return data_dir


def test_identity_show_creates_and_prints_without_key_material(
    data_dir: Path,
) -> None:
    result = runner.invoke(app, ["identity", "show"], prog_name=PROG)
    assert result.exit_code == ExitCode.OK, result.output
    identity = load_or_create(data_dir / "identity")
    assert f"node_id: {identity.node_id}" in result.stdout
    assert "session_epoch: 0" in result.stdout
    assert f"key_fingerprint_sha256: {identity.fingerprint}" in result.stdout
    assert "PRIVATE KEY" not in result.output


def test_identity_show_json(data_dir: Path) -> None:
    result = runner.invoke(app, ["identity", "show", "--json"], prog_name=PROG)
    assert result.exit_code == ExitCode.OK, result.output
    facts = json.loads(result.stdout)
    assert set(facts) == {"node_id", "session_epoch", "key_fingerprint_sha256", "path"}
    assert facts["node_id"].startswith("node_")
    assert facts["path"] == str(data_dir / "identity")


def test_identity_show_is_stable_across_calls(data_dir: Path) -> None:
    a = runner.invoke(app, ["identity", "show", "--json"], prog_name=PROG)
    b = runner.invoke(app, ["identity", "show", "--json"], prog_name=PROG)
    assert json.loads(a.stdout)["node_id"] == json.loads(b.stdout)["node_id"]


def test_identity_reset_requires_yes(data_dir: Path) -> None:
    runner.invoke(app, ["identity", "show"], prog_name=PROG)
    result = runner.invoke(app, ["identity", "reset"], prog_name=PROG)
    assert result.exit_code == ExitCode.USAGE_ERROR
    assert "--yes" in result.stderr
    assert (data_dir / "identity" / "node.key").exists()


def test_identity_reset_with_yes_mints_a_new_node_id(data_dir: Path) -> None:
    before = json.loads(
        runner.invoke(app, ["identity", "show", "--json"], prog_name=PROG).stdout
    )
    result = runner.invoke(app, ["identity", "reset", "--yes"], prog_name=PROG)
    assert result.exit_code == ExitCode.OK, result.output
    assert not (data_dir / "identity" / "node.key").exists()
    assert not (data_dir / "identity" / "node.json").exists()
    after = json.loads(
        runner.invoke(app, ["identity", "show", "--json"], prog_name=PROG).stdout
    )
    assert after["node_id"] != before["node_id"]


def test_identity_show_reports_partial_identity_as_config_error(
    data_dir: Path,
) -> None:
    runner.invoke(app, ["identity", "show"], prog_name=PROG)
    (data_dir / "identity" / "node.json").unlink()
    result = runner.invoke(app, ["identity", "show"], prog_name=PROG)
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert "partial identity" in result.stderr
    assert "identity reset" in result.stderr


def test_run_with_missing_config_file_is_config_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["run", "--config", str(tmp_path / "nope.toml")], prog_name=PROG
    )
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert "config file not found" in result.stderr


def test_run_with_bad_controller_option_is_config_error() -> None:
    result = runner.invoke(
        app, ["run", "--controller", "not-an-address"], prog_name=PROG
    )
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert "controller_address" in result.stderr


def test_no_arguments_prints_help() -> None:
    result = runner.invoke(app, [], prog_name=PROG)
    assert "run" in result.output and "identity" in result.output
    result = runner.invoke(app, ["identity"], prog_name=PROG)
    assert "show" in result.output and "reset" in result.output
