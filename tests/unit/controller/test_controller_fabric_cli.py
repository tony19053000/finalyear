"""``meshforge-controller fabric create|show`` (V0.7 G1, D134).

stdout carries the fabric facts only - never key material; refusals are typed exit
codes. THREAT_MODEL row PK-T2 points here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meshforge.common import pki
from meshforge.common.errors import ExitCode
from meshforge.common.ids import validate_fabric_id
from meshforge.controller import app as controller_app


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"data_dir = '{(tmp_path / 'data').as_posix()}'\n"
        "[controller]\n"
        "grpc_bind = '192.168.1.10:47100'\n"
        "http_bind = '192.168.1.10:47110'\n"
        "[controller.tls]\n"
        "extra_sans = ['controller.lan']\n"
    )
    return cfg


def _invoke(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(controller_app.app, list(args))
    return result.exit_code, result.stdout, result.stderr


def _show_json(config: Path) -> dict[str, object]:
    code, out, err = _invoke("fabric", "show", "--json", "--config", str(config))
    assert code == ExitCode.OK, err
    return dict(json.loads(out))


def _ca_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "controller" / "ca"


def test_create_prints_facts_and_no_secrets(tmp_path: Path, config: Path) -> None:
    code, out, err = _invoke("fabric", "create", "--config", str(config))
    assert code == ExitCode.OK, err
    facts = _show_json(config)
    validate_fabric_id(facts["fabric_id"])
    assert f"fabric id:         {facts['fabric_id']}" in out
    short = str(facts["join_fingerprint"])
    assert pki.format_fingerprint(short) in out
    assert len(short) == 32
    assert str(facts["ca_fingerprint_sha256"]).startswith(short)
    assert "not enforced yet" in out
    for text in (out, err):
        assert "PRIVATE KEY" not in text
        assert "BEGIN" not in text
    ca_key = (_ca_dir(tmp_path) / "ca.key").read_bytes()
    assert ca_key.decode() not in out
    server = facts["server_cert"]
    assert isinstance(server, dict)
    assert server["sans"] == [pki.SERVER_NAME, "192.168.1.10", "controller.lan"]
    assert server["renewal_due"] is False
    assert facts["tls_enforced"] is False


def test_create_refuses_overwrite_without_force(tmp_path: Path, config: Path) -> None:
    _invoke("fabric", "create", "--config", str(config))
    before = (_ca_dir(tmp_path) / "ca.key").read_bytes()
    first = _show_json(config)
    code, out, err = _invoke("fabric", "create", "--config", str(config))
    assert code == ExitCode.CONFLICT
    assert out == ""
    assert "--force" in err and "fabric show" in err
    assert (_ca_dir(tmp_path) / "ca.key").read_bytes() == before
    assert _show_json(config)["fabric_id"] == first["fabric_id"]


def test_force_replaces_and_warns(config: Path) -> None:
    _invoke("fabric", "create", "--config", str(config))
    old = _show_json(config)
    code, out, err = _invoke("fabric", "create", "--force", "--config", str(config))
    assert code == ExitCode.OK
    assert "re-join" not in out  # the warning goes to stderr only
    assert "re-join" in err and "WARNING" in err
    new = _show_json(config)
    assert new["fabric_id"] != old["fabric_id"]
    assert new["ca_fingerprint_sha256"] != old["ca_fingerprint_sha256"]
    server = new["server_cert"]
    assert (
        isinstance(server, dict) and "problem" not in server
    )  # re-issued from the new CA


def test_force_on_a_fresh_data_dir_does_not_warn(config: Path) -> None:
    code, _, err = _invoke("fabric", "create", "--force", "--config", str(config))
    assert code == ExitCode.OK
    assert "WARNING" not in err


def test_show_without_fabric_is_a_config_error(config: Path) -> None:
    code, out, err = _invoke("fabric", "show", "--config", str(config))
    assert code == ExitCode.CONFIG_ERROR
    assert out == ""
    assert "fabric create" in err


def test_show_reports_missing_and_foreign_server_cert(
    tmp_path: Path, config: Path
) -> None:
    _invoke("fabric", "create", "--config", str(config))
    tls_dir = tmp_path / "data" / "controller" / "tls"
    for path in tls_dir.iterdir():
        path.unlink()
    tls_dir.rmdir()
    code, out, _ = _invoke("fabric", "show", "--config", str(config))
    assert code == ExitCode.OK
    assert "not issued yet" in out
    # A server cert from another CA (e.g. copied from another controller).
    other = pki.create_ca(validate_fabric_id(_show_json(config)["fabric_id"]))
    issued = pki.issue_server_cert(other, hosts=[])
    tls_dir.mkdir(mode=0o700)
    pki.write_private_key(tls_dir / "server.key", issued.key)
    pki.write_cert(tls_dir / "server.crt", issued.cert)
    code, out, _ = _invoke("fabric", "show", "--config", str(config))
    assert code == ExitCode.OK
    assert "unusable" in out and "not issued by this fabric CA" in out


def test_broken_ca_is_reported_not_overwritten(tmp_path: Path, config: Path) -> None:
    _invoke("fabric", "create", "--config", str(config))
    (_ca_dir(tmp_path) / "ca.crt").write_bytes(b"garbage")
    code, out, err = _invoke("fabric", "show", "--config", str(config))
    assert code == ExitCode.CONFIG_ERROR
    assert out == ""
    assert "not a PEM certificate" in err
    code, _, err = _invoke("fabric", "create", "--config", str(config))
    assert code == ExitCode.CONFLICT  # still refuses: only --force replaces


def test_fabric_help_lists_commands() -> None:
    code, out, _ = _invoke("fabric")
    assert code in (ExitCode.OK, ExitCode.USAGE_ERROR)
    assert "create" in out and "show" in out
    code, out, _ = _invoke("--help")
    assert "fabric" in out and "token" in out
