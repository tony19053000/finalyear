"""V0.7 G5 (D142): ``meshforge-agent policy show|set|reset`` through Typer's
runner (local only: configuration + ``<data_dir>/policy.toml``)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meshforge.agent.main import app
from meshforge.agent.policy import LivePolicy, read_overlay
from meshforge.agent.settings import PolicySettings
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

runner = CliRunner()
PROG = "meshforge-agent"
GIB = 1024**3


@pytest.fixture(autouse=True)
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MESHFORGE_DATA_DIR", str(data_dir))
    return data_dir


def _show() -> dict[str, object]:
    result = runner.invoke(app, ["policy", "show", "--json"], prog_name=PROG)
    assert result.exit_code in (ExitCode.OK, ExitCode.CONFIG_ERROR), result.output
    return dict(json.loads(result.stdout))


def test_show_defaults_then_set_then_reset(data_dir: Path) -> None:
    view = _show()
    assert view["policy"]["accept_jobs"] is False  # type: ignore[index]
    assert view["live_keys"] == []

    result = runner.invoke(
        app,
        [
            "policy",
            "set",
            "accept_jobs=true",
            "max_ram_bytes=8GiB",
            "allowed_users=alice",
        ],
        prog_name=PROG,
    )
    assert result.exit_code == ExitCode.OK, result.output
    assert "restart it once" in result.stdout  # the config had accept_jobs off
    assert read_overlay(data_dir / "policy.toml") == {
        "accept_jobs": True,
        "max_ram_bytes": 8 * GIB,
        "allowed_users": ["alice"],
    }
    view = _show()
    policy = view["policy"]
    assert policy["accept_jobs"] is True  # type: ignore[index]
    assert policy["max_ram_bytes"] == 8 * GIB  # type: ignore[index]
    assert view["live_keys"] == ["accept_jobs", "allowed_users", "max_ram_bytes"]

    # A second set merges with the first.
    assert runner.invoke(app, ["policy", "set", "max_cpu_percent=50"]).exit_code == 0
    assert set(read_overlay(data_dir / "policy.toml") or {}) == {
        "accept_jobs",
        "allowed_users",
        "max_ram_bytes",
        "max_cpu_percent",
    }
    # The running agent's view of the same file.
    live = LivePolicy(
        PolicySettings(),
        data_dir / "policy.toml",
        gpu_count=0,
        max_concurrent_attempts=4,
    )
    assert live.policy.max_cpu_percent == 50 and live.policy.accept_jobs

    result = runner.invoke(app, ["policy", "reset"], prog_name=PROG)
    assert result.exit_code == ExitCode.OK and "removed" in result.stdout
    assert not (data_dir / "policy.toml").exists()


@pytest.mark.parametrize(
    "args",
    [
        ["max_cpu_percent=500"],
        ["allowed_users=Alice"],
        ["nonsense=1"],
        ["accept_jobs"],
        ["max_cpu_jobs=64"],  # over max_concurrent_attempts (4)
    ],
)
def test_set_refuses_an_invalid_policy_and_writes_nothing(
    data_dir: Path, args: list[str]
) -> None:
    result = runner.invoke(app, ["policy", "set", *args], prog_name=PROG)
    assert result.exit_code == ExitCode.USAGE_ERROR, result.output
    assert not (data_dir / "policy.toml").exists()


def test_show_reports_a_broken_live_file(data_dir: Path) -> None:
    data_dir.mkdir(parents=True)
    path = data_dir / "policy.toml"
    path.write_text("accept_jobs = maybe\n", "utf-8")
    os.chmod(path, 0o600)
    result = runner.invoke(app, ["policy", "show"], prog_name=PROG)
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert "INVALID" in result.stdout
