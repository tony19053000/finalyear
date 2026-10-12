# ruff: noqa: RUF001
# (adversarial literals / world-writable modes / seeded fuzz are the point of these tests)
"""V0.7 G5 tester round 1: what `meshforge-agent policy set` tells the owner."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meshforge.agent.main import app
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit
runner = CliRunner()


@pytest.fixture(autouse=True)
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MESHFORGE_DATA_DIR", str(data_dir))
    return data_dir


def _set(*items: str) -> str:
    result = runner.invoke(app, ["policy", "set", *items], prog_name="meshforge-agent")
    assert result.exit_code == ExitCode.OK, result.output
    return result.stdout


@pytest.mark.xfail(
    strict=True,
    reason="T-9 minor: turning allow_unsandboxed on live needs a restart (the "
    "subprocess/venv runtimes were never probed) but `policy set` only prints the "
    "restart note for accept_jobs",
)
def test_set_allow_unsandboxed_on_says_a_restart_is_needed() -> None:
    assert "restart" in _set("allow_unsandboxed=true")


def test_turning_things_off_never_asks_for_a_restart() -> None:
    assert "restart" not in _set("accept_jobs=false", "allow_unsandboxed=false")


def test_set_rejects_hostile_values_and_writes_nothing(data_dir: Path) -> None:
    for item in (
        "allowed_users=Alice",  # upper case is not a user name
        "allowed_users=a b",
        "allowed_users=аlice",  # Cyrillic a
        "max_ram_bytes=1",  # below the floor
        "max_ram_bytes=-5",
        "max_cpu_percent=0",
        "max_cpu_percent=101",
        "max_ram_bytes=99999999999999999999TiB",
        "accept_jobs=perhaps",
        "bogus=1",
        "accept_jobs",
    ):
        result = runner.invoke(
            app, ["policy", "set", item], prog_name="meshforge-agent"
        )
        assert result.exit_code != ExitCode.OK, item
    assert not (data_dir / "policy.toml").exists()
