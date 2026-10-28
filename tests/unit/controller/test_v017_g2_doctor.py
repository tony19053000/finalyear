"""V0.17 G2 (SP2 §7.6): ``meshforge-controller doctor`` checks and output.

Real files and sockets in tmp_path; the PostgreSQL binary check uses a fake
bin folder (missing programs) - the real bundle is component-tested.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meshforge.controller import doctor
from meshforge.controller.app import app
from meshforge.controller.doctor import CheckStatus, run_checks
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.unit


def _settings(tmp_path: Path, port: int = 47999) -> ControllerSettings:
    return ControllerSettings(
        data_dir=tmp_path / "state",
        controller={
            "db": {
                "mode": "managed",
                "bin_dir": tmp_path / "pg" / "bin",
                "port": port,
            }
        },
        database=DatabaseSettings(),
    )


def _by_name(report: doctor.DoctorReport) -> dict[str, doctor.Check]:
    return {c.name: c for c in report.checks}


def _dist(tmp_path: Path, built: bool = True) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    if built:
        (dist / "index.html").write_text("<html></html>")
    return dist


def test_fresh_install_before_first_start(tmp_path: Path) -> None:
    report = run_checks(_settings(tmp_path), dist=_dist(tmp_path))
    checks = _by_name(report)
    # missing binaries fail; not-yet-created things only warn
    assert checks["postgresql binaries"].status is CheckStatus.FAIL
    assert "missing" in checks["postgresql binaries"].detail
    assert checks["database passwords"].status is CheckStatus.WARN
    assert checks["database files"].status is CheckStatus.WARN
    assert checks["fabric CA"].status is CheckStatus.WARN
    assert checks["dashboard"].status is CheckStatus.OK
    assert not report.ok
    assert report.lines()[-1] == "some checks FAILED"


def test_port_in_use_by_another_program_fails(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        report = run_checks(_settings(tmp_path, port), dist=_dist(tmp_path))
        assert _by_name(report)["database port"].status is CheckStatus.FAIL
        # ...but not when the running controller's own cluster holds it
        report = run_checks(
            _settings(tmp_path, port),
            dist=_dist(tmp_path),
            cluster_running=lambda: True,
        )
        assert _by_name(report)["database port"].status is CheckStatus.OK


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX file modes (the controller is Linux-only, D398)"
)
def test_password_files_must_be_private(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = settings.db_root
    root.mkdir(parents=True, mode=0o700)
    for name in ("admin.password", "app.password"):
        (root / name).write_text("pw\n")
        (root / name).chmod(0o600)
    ok = _by_name(run_checks(settings, dist=_dist(tmp_path)))["database passwords"]
    assert ok.status is CheckStatus.OK
    (root / "app.password").chmod(0o644)
    bad = _by_name(run_checks(settings, dist=_dist(tmp_path)))["database passwords"]
    assert bad.status is CheckStatus.FAIL
    assert "chmod 600" in bad.hint
    assert "pw" not in bad.detail


def test_data_dir_major_and_foreign_folder(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    data = settings.db_root / "data"
    data.mkdir(parents=True)
    (data / "PG_VERSION").write_text("15\n")
    check = _by_name(run_checks(settings, dist=_dist(tmp_path)))["database files"]
    assert check.status is CheckStatus.FAIL and "PostgreSQL 15" in check.detail
    (data / "PG_VERSION").write_text("16\n")
    check = _by_name(run_checks(settings, dist=_dist(tmp_path)))["database files"]
    assert check.status is CheckStatus.OK
    (data / "PG_VERSION").unlink()
    (data / "x").write_text("y")
    check = _by_name(run_checks(settings, dist=_dist(tmp_path)))["database files"]
    assert check.status is CheckStatus.FAIL


def test_missing_dashboard_fails(tmp_path: Path) -> None:
    check = _by_name(run_checks(_settings(tmp_path), dist=_dist(tmp_path, False)))
    assert check["dashboard"].status is CheckStatus.FAIL


def test_disk_thresholds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import shutil
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    for free, status in (
        (10 * 1024**3, CheckStatus.OK),
        (1 * 1024**3, CheckStatus.WARN),
        (100 * 1024**2, CheckStatus.FAIL),
    ):
        monkeypatch.setattr(shutil, "disk_usage", lambda _p, f=free: usage(0, 0, f))
        assert doctor._disk(tmp_path / "not" / "yet").status is status


def test_glibc_check_on_this_machine() -> None:
    check = doctor._glibc()
    assert check.status in (CheckStatus.OK, CheckStatus.FAIL)
    assert check.detail


def test_cli_json_and_exit_code(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f"data_dir = {str(tmp_path / 'state')!r}\n"
        "[controller.db]\n"
        'mode = "managed"\n'
        f"bin_dir = {str(tmp_path / 'nope')!r}\n"
    )
    result = CliRunner().invoke(app, ["doctor", "--config", str(cfg), "--json"])
    assert result.exit_code == 1, result.output
    body = json.loads(result.output)
    assert body["ok"] is False
    names = [c["name"] for c in body["checks"]]
    assert names[:5] == [
        "glibc",
        "postgresql binaries",
        "database port",
        "database passwords",
        "database files",
    ]


def test_cli_db_restore_needs_yes(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["db", "restore", str(tmp_path / "x.dump")])
    assert result.exit_code == 2
    assert "--yes" in result.output


def test_cli_db_backup_refuses_external_mode(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        f'data_dir = {str(tmp_path)!r}\n[controller.db]\nmode = "external"\n'
    )
    result = CliRunner().invoke(app, ["db", "backup", "--config", str(cfg)])
    assert result.exit_code == 4
    assert "external" in result.output
