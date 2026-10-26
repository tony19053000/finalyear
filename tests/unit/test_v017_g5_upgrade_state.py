"""V0.17 G5 (D393, Q38/Q39): the readers of the packages' upgrade records -
``common/upgrade_state.py``, ``meshforge-agent doctor`` and the controller's
forced-failure hook - against files written the way
``packaging/linux/common/upgrade.sh`` writes them.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from meshforge.common.upgrade_state import (
    FORCED_FAILURE_NAME,
    forced_failure,
    program_dir,
    upgrade_dir,
    upgrade_notice,
)

pytestmark = pytest.mark.unit


def _upg(tmp_path: Path) -> Path:
    folder = upgrade_dir(tmp_path, windows=False)
    folder.mkdir()
    return folder


def test_program_dir_of_a_version_and_of_a_rolled_back_copy(tmp_path: Path) -> None:
    assert program_dir(Path("/opt/meshforge/0.17.1")) == Path("/opt/meshforge")
    assert program_dir(Path("/opt/meshforge/.previous/0.17.0")) == Path(
        "/opt/meshforge"
    )
    assert program_dir(Path("/opt/meshforge/controller/0.17.1")) == Path(
        "/opt/meshforge/controller"
    )
    assert upgrade_dir(tmp_path, windows=True).name == "upgrade"
    assert upgrade_dir(tmp_path, windows=False).name == ".upgrade"


@pytest.mark.parametrize(
    ("content", "version", "hit"),
    [
        ("0.17.1\n", "0.17.1", True),
        ("*", "0.17.1", True),
        ("0.17.1", "0.17.2", False),
        ("", "1", False),
    ],
)
def test_forced_failure_trigger(
    tmp_path: Path, content: str, version: str, hit: bool
) -> None:
    (_upg(tmp_path) / FORCED_FAILURE_NAME).write_text(content)
    reason = forced_failure(tmp_path, version, windows=False, require_root=False)
    assert (reason is not None) is hit
    if reason:
        assert "forced upgrade failure" in reason and FORCED_FAILURE_NAME in reason


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership")
def test_forced_failure_needs_a_root_owned_regular_file(tmp_path: Path) -> None:
    upg = _upg(tmp_path)
    (upg / FORCED_FAILURE_NAME).write_text("*")
    if os.geteuid() != 0:  # ours, not root's: ignored
        assert forced_failure(tmp_path, "1", windows=False) is None
    (upg / FORCED_FAILURE_NAME).unlink()
    (tmp_path / "x").write_text("*")
    (upg / FORCED_FAILURE_NAME).symlink_to(tmp_path / "x")
    assert forced_failure(tmp_path, "1", windows=False, require_root=False) is None
    assert forced_failure(tmp_path / "missing", "1", windows=False) is None


def _record(action: str) -> str:
    return json.dumps(
        {
            "package": "meshforge-agent",
            "action": action,
            "from": "0.17.0",
            "to": "0.17.1",
            "reason": "console did not answer",
            "at": "2026-10-26T10:00:00Z",
        }
    )


def test_notice_order_rolled_back_then_interrupted_then_in_progress(
    tmp_path: Path,
) -> None:
    upg = _upg(tmp_path)
    assert upgrade_notice(tmp_path, windows=False, require_root=False) is None
    (upg / "state").write_text("from=0.17.0\nto=0.17.1\nphase=configuring\n")
    notice = upgrade_notice(tmp_path, windows=False, require_root=False)
    assert notice is not None and notice.kind == "in_progress"
    assert "0.17.0 to 0.17.1" in notice.message
    (upg / "interrupted.json").write_text(_record("reverted"))
    notice = upgrade_notice(tmp_path, windows=False, require_root=False)
    assert notice is not None and notice.kind == "interrupted"
    assert "previous version 0.17.0 runs" in notice.message
    assert "dpkg --configure -a" in notice.message
    (upg / "interrupted.json").write_text(_record("finishing"))
    notice = upgrade_notice(tmp_path, windows=False, require_root=False)
    assert notice is not None and "version 0.17.1 runs" in notice.message
    (upg / "rolled-back.json").write_text(_record("rolled_back"))
    notice = upgrade_notice(tmp_path, windows=False, require_root=False)
    assert notice is not None and notice.kind == "rolled_back"
    assert "previous version 0.17.0" in notice.message
    assert "console did not answer" in notice.message


def test_garbage_records_are_ignored_not_fatal(tmp_path: Path) -> None:
    upg = _upg(tmp_path)
    (upg / "rolled-back.json").write_text("{not json")
    (upg / "interrupted.json").write_text("[1, 2]")
    assert upgrade_notice(tmp_path, windows=False, require_root=False) is None


# ------------------------------------------------------------------ doctor
def test_doctor_reports_a_rollback_as_a_failure(tmp_path: Path) -> None:
    pytest.importorskip("meshforge.agent.doctor", reason="needs the agent extra")
    from meshforge.agent.doctor import ItemStatus, gather, render
    from meshforge.agent.settings import AgentSettings

    bundle = tmp_path / "opt" / ".previous" / "0.17.0"
    bundle.mkdir(parents=True)
    (bundle / "BUILDINFO.json").write_text(
        json.dumps({"version": "0.17.0", "git_sha": "a" * 40, "role": "agent"})
    )
    settings = AgentSettings(data_dir=tmp_path / "data")
    report = gather(settings, root=bundle, with_checks=False, require_root=False)
    assert not report.failed
    ids = {i.id: i for i in report.items}
    assert ids["version"].text.startswith("meshforge-agent 0.17.0 (git aaaaaaaaaaaa)")
    assert ids["joined"].status is ItemStatus.WARN
    assert ids["upgrade"].status is ItemStatus.OK
    upg = upgrade_dir(tmp_path / "opt", windows=False)
    upg.mkdir()
    (upg / "rolled-back.json").write_text(_record("rolled_back"))
    report = gather(settings, root=bundle, with_checks=False, require_root=False)
    assert report.failed
    item = next(i for i in report.items if i.id == "upgrade")
    assert item.status is ItemStatus.FAIL
    assert "running the previous version 0.17.0" in item.text
    text = render(report)
    assert "FAIL  upgrade" in text and "fix: " in text
    assert text.endswith("doctor: something needs attention (FAIL above)")
    assert json.loads(json.dumps(report.as_dict()))["ok"] is False


def test_doctor_in_a_checkout(tmp_path: Path) -> None:
    pytest.importorskip("meshforge.agent.doctor", reason="needs the agent extra")
    from meshforge.agent.doctor import gather
    from meshforge.agent.settings import AgentSettings

    report = gather(AgentSettings(data_dir=tmp_path), root=None, with_checks=False)
    assert "developer checkout" in report.items[0].text
    assert all(i.id != "upgrade" for i in report.items)


def test_doctor_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("meshforge.agent.doctor", reason="needs the agent extra")
    from typer.testing import CliRunner

    from meshforge.agent import doctor as doctor_module
    from meshforge.agent.main import app

    monkeypatch.setenv("MESHFORGE_INSTALL_MODE", "user")
    monkeypatch.setenv("MESHFORGE_DATA_DIR", str(tmp_path))
    real = doctor_module.gather
    monkeypatch.setattr(
        doctor_module, "gather", lambda s: real(s, root=None, with_checks=False)
    )
    done = CliRunner().invoke(app, ["doctor", "--json"])
    assert done.exit_code == 0, done.output
    assert json.loads(done.output)["ok"] is True


# --------------------------------------------- controller forced failure
def test_controller_forced_failure_only_for_installed_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.common.errors import ConfigError
    from meshforge.controller import server

    monkeypatch.setattr(server, "bundle_root", lambda: None)
    server._raise_forced_upgrade_failure("0.17.1")  # a checkout: never
    bundle = tmp_path / "controller" / "0.17.1"
    bundle.mkdir(parents=True)
    monkeypatch.setattr(server, "bundle_root", lambda: bundle)
    server._raise_forced_upgrade_failure("0.17.1")  # no trigger
    calls: list[tuple[Path, str]] = []

    def fake(program: Path, version: str) -> str | None:
        calls.append((program, version))
        return "forced upgrade failure: test"

    monkeypatch.setattr(server, "forced_failure", fake)
    with pytest.raises(ConfigError, match="forced upgrade failure"):
        server._raise_forced_upgrade_failure("0.17.1")
    assert calls == [(tmp_path / "controller", "0.17.1")]
