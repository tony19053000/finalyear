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
    assert set(facts) == {
        "node_id",
        "session_epoch",
        "key_fingerprint_sha256",
        "path",
        "enrolled",  # V0.7 G2: false until `meshforge-agent join`
    }
    assert facts["enrolled"] is False
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


# ------------------------------------------------------- hardware show (G5, D13)


@pytest.mark.extras("agent")
def test_hardware_show_renders_the_probe_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command is local only: the facts are injected, nothing is probed here."""
    import meshforge.agent.hardware as hardware_pkg

    facts = _fabricated_facts()
    monkeypatch.setattr(
        hardware_pkg.HardwareProvider, "get", lambda self: facts, raising=True
    )
    result = runner.invoke(app, ["hardware", "show"], prog_name=PROG)
    assert result.exit_code == ExitCode.OK, result.output
    assert "Fictional CPU 9000" in result.stdout
    assert "unavailable" in result.stdout
    assert "degraded:" in result.stdout


@pytest.mark.extras("agent")
def test_hardware_show_json_is_one_parseable_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meshforge.agent.hardware as hardware_pkg

    facts = _fabricated_facts()
    monkeypatch.setattr(
        hardware_pkg.HardwareProvider, "get", lambda self: facts, raising=True
    )
    result = runner.invoke(app, ["hardware", "show", "--json"], prog_name=PROG)
    assert result.exit_code == ExitCode.OK, result.output
    payload = json.loads(result.stdout)
    assert payload["cpu"]["model"] == "Fictional CPU 9000"
    assert payload["degraded"] == ["gpu: unavailable: not present"]


@pytest.mark.extras("agent")
def test_hardware_show_reports_a_wedged_probe_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that never returns is abandoned on its daemon thread (D10)."""
    import threading

    import meshforge.agent.hardware as hardware_pkg

    release = threading.Event()

    def wedged(_self: object) -> object:
        release.wait(30.0)
        return _fabricated_facts()

    monkeypatch.setattr(hardware_pkg.HardwareProvider, "get", wedged, raising=True)
    monkeypatch.setenv("MESHFORGE_AGENT__HARDWARE__TIMEOUT_S", "0.05")
    try:
        result = runner.invoke(app, ["hardware", "show"], prog_name=PROG)
    finally:
        release.set()
    assert result.exit_code == ExitCode.FAILURE, result.output
    assert "did not finish" in result.output
    assert "timeout_s" in result.output


def _fabricated_facts() -> object:
    from meshforge.agent.hardware.cpu import CpuFacts
    from meshforge.agent.hardware.probe import ProbeOutcome, ProbeStatus
    from meshforge.agent.hardware.summary import HardwareFacts

    return HardwareFacts(
        cpu=CpuFacts(
            model="Fictional CPU 9000",
            physical_cores=4,
            logical_threads=8,
            ram_bytes=1 << 33,
            source="test",
        ),
        capabilities=("docker",),
        probes=(
            ProbeOutcome("cpu", ProbeStatus.OK, value=1, duration_s=0.01),
            ProbeOutcome("gpu", ProbeStatus.UNAVAILABLE, reason="not present"),
        ),
        duration_s=0.5,
    )
