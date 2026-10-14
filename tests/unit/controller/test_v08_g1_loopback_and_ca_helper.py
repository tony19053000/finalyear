"""V0.8 G1 deliverable 5 (brief Q1): ``[controller.http] loopback_plain`` binds
127.0.0.1 / ::1 only, and ``meshforge-controller ca trust-helper`` prints the
per-OS steps for the fabric CA - and changes nothing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from meshforge.common.errors import ExitCode
from meshforge.controller import app as controller_app
from meshforge.controller.ca_trust import trust_steps
from meshforge.controller.settings import HttpSettings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("bind", ["127.0.0.1:47111", "127.0.0.1:0", "[::1]:47111"])
def test_loopback_plain_accepts_only_the_two_loopback_literals(bind: str) -> None:
    assert HttpSettings(loopback_plain=bind).loopback_plain == bind


@pytest.mark.security
@pytest.mark.parametrize(
    "bind",
    [
        "0.0.0.0:47111",
        "[::]:47111",
        "192.168.1.5:47111",
        "10.0.0.1:47111",
        "localhost:47111",
        "127.0.0.2:47111",  # loopback, but not the documented address
        "127.0.0.1",
        "",
    ],
)
def test_loopback_plain_refuses_every_other_bind(bind: str) -> None:
    with pytest.raises(ValidationError):
        HttpSettings(loopback_plain=bind)


def test_a_v4_mapped_loopback_is_the_same_v4_loopback() -> None:
    """``parse_bind`` normalises ``::ffff:127.0.0.1`` to ``127.0.0.1`` - the
    socket that is bound is the IPv4 loopback, so it is accepted."""
    from meshforge.common.netaddr import parse_bind

    value = HttpSettings(loopback_plain="[::ffff:127.0.0.1]:47111").loopback_plain
    assert value is not None and parse_bind(value).host == "127.0.0.1"


def test_loopback_plain_is_off_by_default() -> None:
    assert HttpSettings().loopback_plain is None


def test_trust_steps_name_the_ca_its_fingerprint_and_each_os() -> None:
    text = trust_steps(ca_path="/srv/mf/ca.crt", fingerprint="AB:CD", fabric_id="fab_x")
    assert "/srv/mf/ca.crt" in text and "AB:CD" in text
    for marker in ("== Linux ==", "== Windows", "== macOS", "Remove:"):
        assert marker in text
    assert "loopback_plain" in text
    only = trust_steps(
        ca_path="c", fingerprint="f", fabric_id="fab_x", os_name="windows"
    )
    assert "== Windows" in only and "== Linux ==" not in only
    with pytest.raises(ValueError, match="os must be"):
        trust_steps(ca_path="c", fingerprint="f", fabric_id="x", os_name="beos")


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


def test_trust_helper_prints_steps_from_the_real_ca_and_touches_nothing(
    tmp_path: Path, config: Path
) -> None:
    code, _, err = _invoke("fabric", "create", "--config", str(config))
    assert code == ExitCode.OK, err
    ca_dir = tmp_path / "data" / "controller" / "ca"
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    code, out, err = _invoke("ca", "trust-helper", "--config", str(config))
    assert code == ExitCode.OK, err
    assert str(ca_dir / "ca.crt") in out
    _code, show, _ = _invoke("fabric", "show", "--json", "--config", str(config))
    import json

    full = str(json.loads(show)["ca_fingerprint_sha256"]).upper()
    assert ":".join(full[i : i + 2] for i in range(0, 64, 2)) in out
    assert "PRIVATE KEY" not in out
    code, pem, _ = _invoke("ca", "trust-helper", "--pem", "--config", str(config))
    assert code == ExitCode.OK
    assert pem == (ca_dir / "ca.crt").read_text()
    assert "PRIVATE" not in pem
    after = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    assert before == after  # read-only: nothing written or touched


def test_trust_helper_without_a_ca_or_with_a_bad_os_fails_typed(config: Path) -> None:
    code, _, err = _invoke("ca", "trust-helper", "--config", str(config))
    assert code != ExitCode.OK and "fabric" in err.lower()
    code, _, err = _invoke(
        "ca", "trust-helper", "--os", "beos", "--config", str(config)
    )
    assert code == ExitCode.USAGE_ERROR
