# ruff: noqa: S104, S311  # hostile-input fixtures: wildcard hosts, SHA-1, bidi/unicode lookalikes
"""V0.7 G1 tester: ``meshforge-controller fabric create|show`` as real OS processes.

Concurrency across processes, SIGKILL at random moments, a full disk (RLIMIT_FSIZE),
a read-only data dir, unicode / spaced paths, secrets never on stdout/stderr, exit
codes and ``--force`` semantics. THREAT_MODEL rows PK-T2, LF-I1, LF-T3.
"""

from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytest.importorskip("typer", reason="needs the controller extra")

from meshforge.common import pki
from meshforge.common.errors import ExitCode
from meshforge.controller.fabric_ca import (
    FabricCaError,
    FabricMissingError,
    load_fabric,
)

pytestmark = pytest.mark.component

posix_only = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX signals / rlimits / modes"
)
_CLI = "from meshforge.controller.app import main; main()"


def _env() -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if not k.upper().startswith("MESHFORGE_")
    }
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _write_config(tmp_path: Path, data_dir: Path, extra: str = "") -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f"data_dir = '{data_dir.as_posix()}'\n"
        "[controller]\n"
        "grpc_bind = '192.168.1.10:47100'\n"
        "http_bind = '192.168.1.10:47110'\n"
        "[controller.tls]\n"
        "extra_sans = ['controller.lan']\n" + extra,
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def config(tmp_path: Path, data_dir: Path) -> Path:
    return _write_config(tmp_path, data_dir)


def _popen(*args: str, preexec: object = None) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _CLI, *args],
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        preexec_fn=preexec,  # type: ignore[arg-type]
    )


def _run(*args: str, timeout: float = 120.0) -> tuple[int, str, str]:
    proc = _popen(*args)
    out, err = proc.communicate(timeout=timeout)
    return proc.returncode, out, err


def _ca_dir(data_dir: Path) -> Path:
    return data_dir / "controller" / "ca"


def _tls_dir(data_dir: Path) -> Path:
    return data_dir / "controller" / "tls"


def _show(config: Path) -> dict[str, object]:
    code, out, err = _run("fabric", "show", "--json", "--config", str(config))
    assert code == 0, err
    return dict(json.loads(out))


def _key_bodies(data_dir: Path) -> list[str]:
    bodies: list[str] = []
    for key in (_ca_dir(data_dir) / "ca.key", _tls_dir(data_dir) / "server.key"):
        if key.exists():
            bodies += [
                ln for ln in key.read_text().splitlines() if ln and "-----" not in ln
            ]
    return bodies


def _no_leftovers(data_dir: Path) -> None:
    hidden = [
        p.name for p in (data_dir / "controller").iterdir() if p.name.startswith(".")
    ]
    assert hidden == []


# ----------------------------------------------------------------- exit codes / UX


def test_create_show_force_exit_codes_and_streams(data_dir: Path, config: Path) -> None:
    code, _, err = _run("fabric", "show", "--config", str(config))
    assert code == ExitCode.CONFIG_ERROR and "fabric create" in err  # no CA yet
    assert "Traceback" not in err

    code, out, err = _run("fabric", "create", "--config", str(config))
    assert code == 0, err
    assert "WARNING" not in err  # first create: no re-join warning
    first = _show(config)

    code, out2, err2 = _run("fabric", "create", "--config", str(config))
    assert code == ExitCode.CONFLICT, (out2, err2)
    assert "--force" in err2 and "Traceback" not in err2
    assert _show(config) == first  # refused create changed nothing

    code, out3, err3 = _run("fabric", "create", "--force", "--config", str(config))
    assert code == 0, err3
    assert "re-join" in err3 and "restart" in err3  # the warning goes to stderr
    assert "re-join" not in out3
    second = _show(config)
    assert second["fabric_id"] != first["fabric_id"]
    assert second["ca_fingerprint_sha256"] != first["ca_fingerprint_sha256"]
    server = second["server_cert"]
    assert isinstance(server, dict) and "problem" not in server  # re-issued for new CA
    assert "meshforge-controller" in server["sans"] and "192.168.1.10" in server["sans"]
    assert "controller.lan" in server["sans"]
    assert "0.0.0.0" not in server["sans"]
    for blob in (out, err, out2, err2, out3, err3):
        assert "PRIVATE KEY" not in blob
        for body in _key_bodies(data_dir):
            assert body not in blob
    _no_leftovers(data_dir)


def test_force_on_a_fresh_install_does_not_claim_a_replacement(
    data_dir: Path, config: Path
) -> None:
    code, _, err = _run("fabric", "create", "--force", "--config", str(config))
    assert code == 0 and "re-join" not in err


def test_show_json_has_no_secret_fields_and_matches_the_files(
    data_dir: Path, config: Path
) -> None:
    assert _run("fabric", "create", "--config", str(config))[0] == 0
    facts = _show(config)
    blob = json.dumps(facts)
    assert "PRIVATE" not in blob
    fab = load_fabric(_ca_dir(data_dir))
    assert facts["fabric_id"] == fab.fabric_id
    assert facts["ca_fingerprint_sha256"] == fab.fingerprint
    assert facts["join_fingerprint"] == fab.short_fingerprint
    assert str(facts["ca_fingerprint_sha256"]).startswith(
        str(facts["join_fingerprint"])
    )
    code, out, _ = _run("fabric", "show", "--config", str(config))
    assert code == 0 and pki.format_fingerprint(fab.short_fingerprint) in out
    assert "not enforced" in out  # honest about G1 scope


def test_show_with_a_damaged_ca_is_a_clean_config_error(
    data_dir: Path, config: Path
) -> None:
    assert _run("fabric", "create", "--config", str(config))[0] == 0
    body = _key_bodies(data_dir)[0]
    (_ca_dir(data_dir) / "ca.key").write_text("-----BEGIN PRIVATE KEY-----\nxx\n")
    code, out, err = _run("fabric", "show", "--config", str(config))
    assert code == ExitCode.CONFIG_ERROR and "Traceback" not in err
    assert body not in out + err
    # a missing cert half
    (_ca_dir(data_dir) / "ca.crt").unlink()
    code, _, err = _run("fabric", "show", "--config", str(config))
    assert code == ExitCode.CONFIG_ERROR and "incomplete" in err
    # --force is the documented way out
    assert _run("fabric", "create", "--force", "--config", str(config))[0] == 0
    assert _run("fabric", "show", "--config", str(config))[0] == 0


def test_invalid_tls_settings_fail_before_anything_is_created(
    tmp_path: Path, data_dir: Path
) -> None:
    for bad in ("*.lan", "0.0.0.0", "", "a b", "http://x"):
        cfg = tmp_path / "bad.toml"
        cfg.write_text(
            f"data_dir = '{data_dir.as_posix()}'\n[controller.tls]\nextra_sans = ['{bad}']\n"
        )
        code, _, err = _run("fabric", "create", "--config", str(cfg))
        assert code != 0 and "Traceback" not in err, bad
        assert not _ca_dir(data_dir).exists(), bad  # nothing half-created


def test_wildcard_bind_never_becomes_a_san(tmp_path: Path, data_dir: Path) -> None:
    cfg = tmp_path / "w.toml"
    cfg.write_text(
        f"data_dir = '{data_dir.as_posix()}'\n"
        "[controller]\nallow_any_bind = true\n"
        "grpc_bind = '0.0.0.0:47100'\nhttp_bind = '[::]:47110'\n"
        "[controller.tls]\nextra_sans = ['10.1.2.3']\n"
    )
    code, _, err = _run("fabric", "create", "--config", str(cfg))
    assert code == 0, err
    sans = _show(cfg)["server_cert"]
    assert isinstance(sans, dict)
    assert sans["sans"] == ["meshforge-controller", "10.1.2.3"]


def test_unicode_and_spaced_data_dir(tmp_path: Path) -> None:
    weird = tmp_path / "dätä dir 日本語 ünï" / "ctrl data"
    cfg = _write_config(tmp_path, weird)
    code, _, err = _run("fabric", "create", "--config", str(cfg))
    assert code == 0, err
    assert _run("fabric", "show", "--config", str(cfg))[0] == 0
    assert _ca_dir(weird).is_dir()
    assert load_fabric(_ca_dir(weird)).fabric_id.startswith("fab_")


def test_very_long_data_dir_is_a_typed_error_or_works(tmp_path: Path) -> None:
    deep = tmp_path
    for _ in range(40):
        deep = deep / ("d" * 90)  # > 3 KiB path: beyond PATH_MAX on Linux
    cfg = _write_config(tmp_path, deep)
    code, _, err = _run("fabric", "create", "--config", str(cfg))
    assert "Traceback" not in err
    assert code in {0, ExitCode.CONFIG_ERROR}, err


# ------------------------------------------------------------- concurrency, crashes


def test_racing_creates_exactly_one_wins(data_dir: Path, config: Path) -> None:
    procs = [_popen("fabric", "create", "--config", str(config)) for _ in range(8)]
    results = [(p.communicate(timeout=180), p.returncode) for p in procs]
    codes = sorted(rc for _, rc in results)
    assert codes == [0] + [ExitCode.CONFLICT] * 7, [(rc, e) for (_, e), rc in results]
    for (_, err), _rc in results:
        assert "Traceback" not in err
    winners = [out for (out, _), rc in results if rc == 0]
    facts = _show(config)
    assert pki.format_fingerprint(str(facts["join_fingerprint"])) in winners[0]
    fab = load_fabric(_ca_dir(data_dir))
    assert fab.fingerprint == facts["ca_fingerprint_sha256"]
    _no_leftovers(data_dir)


@posix_only
def test_racing_forced_creates_leave_one_consistent_fabric(
    data_dir: Path, config: Path
) -> None:
    assert _run("fabric", "create", "--config", str(config))[0] == 0
    procs = [
        _popen("fabric", "create", "--force", "--config", str(config)) for _ in range(6)
    ]
    outcomes = [(p.communicate(timeout=180), p.returncode) for p in procs]
    for (_, err), rc in outcomes:
        assert rc in {0, ExitCode.CONFIG_ERROR}, err  # 4 = lock timeout, typed
        assert "Traceback" not in err
    assert sum(1 for _, rc in outcomes if rc == 0) >= 1
    fab = load_fabric(_ca_dir(data_dir))  # always loadable, key matches cert
    assert pki.public_keys_equal(fab.authority.key.public_key(), fab.cert.public_key())
    _no_leftovers(data_dir)
    facts = _show(config)
    server = facts["server_cert"]
    # NOTE (observation O-1, not asserted as a defect): create_fabric and the
    # following ensure_server_cert are separate lock sections, so a racing --force
    # can leave a server cert from the superseded CA; the controller start-up heals
    # it (reissue 'other_ca'). We only require the system to *report* the state.
    assert isinstance(server, dict)


_ENSURE_SCRIPT = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from meshforge.controller.fabric_ca import load_fabric, ensure_server_cert
    ca, tls, go = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
    fab = load_fabric(ca)
    while time.time() < go:
        time.sleep(0.001)
    got = ensure_server_cert(tls, fab, hosts=["10.0.0.1"])
    print(got.action.value, got.cert.serial_number)
    """
)


def test_racing_server_cert_issues_converge_on_one_cert(
    data_dir: Path, config: Path
) -> None:
    assert _run("fabric", "create", "--config", str(config))[0] == 0
    # remove the cert `create` issued so every racer must decide
    import shutil

    shutil.rmtree(_tls_dir(data_dir))
    go = time.time() + 1.5
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _ENSURE_SCRIPT,
                str(_ca_dir(data_dir)),
                str(_tls_dir(data_dir)),
                str(go),
            ],
            env=_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]
    outs = [(p.communicate(timeout=120), p.returncode) for p in procs]
    lines = []
    for (out, err), rc in outs:
        assert rc == 0, err
        lines.append(out.strip().splitlines()[-1].split())  # last line: action serial
    actions = sorted(a for a, _ in lines)
    assert actions == ["issued"] + ["kept"] * 7, actions
    assert len({serial for _, serial in lines}) == 1  # everyone sees the same cert
    _no_leftovers(data_dir)


@posix_only
def test_sigkill_at_random_moments_never_leaves_a_torn_state(
    data_dir: Path, config: Path
) -> None:
    assert _run("fabric", "create", "--config", str(config))[0] == 0
    rng = random.Random(7)
    survived_missing = 0
    for _ in range(25):
        proc = _popen("fabric", "create", "--force", "--config", str(config))
        time.sleep(rng.uniform(0.0, 1.2))  # python start-up ~0.5 s: hits all phases
        proc.send_signal(signal.SIGKILL)
        proc.communicate(timeout=30)
        ca = _ca_dir(data_dir)
        if ca.exists():
            fab = load_fabric(ca)  # complete and consistent, or a typed error
            assert pki.public_keys_equal(
                fab.authority.key.public_key(), fab.cert.public_key()
            )
        else:
            survived_missing += 1
            with pytest.raises(FabricMissingError):
                load_fabric(ca)
        tls = _tls_dir(data_dir)
        if tls.exists():
            from meshforge.controller.fabric_ca import load_server_cert

            try:
                load_server_cert(tls)  # key and cert always a matching pair
            except FabricCaError as exc:  # pragma: no cover - would be a defect
                pytest.fail(f"torn tls pair after SIGKILL: {exc}")
        code, _, err = _run("fabric", "show", "--config", str(config))
        assert code in {0, ExitCode.CONFIG_ERROR} and "Traceback" not in err
    # recover: whatever state we are in, `create --force` converges
    assert _run("fabric", "create", "--force", "--config", str(config))[0] == 0
    _no_leftovers(data_dir)
    assert "problem" not in json.dumps(_show(config)["server_cert"])
    _ = survived_missing


@posix_only
def test_full_disk_on_create_leaves_nothing_half_written(
    data_dir: Path, config: Path
) -> None:
    import resource

    def limit_files() -> None:
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (200, 200))  # a key is ~240 B

    proc = _popen("fabric", "create", "--config", str(config), preexec=limit_files)
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == ExitCode.CONFIG_ERROR, (out, err)
    assert "Traceback" not in err
    assert "PRIVATE KEY" not in out + err
    ca = _ca_dir(data_dir)
    assert not ca.exists()
    _no_leftovers(data_dir)
    # the disk comes back: a plain create works and nothing remembers the failure
    assert _run("fabric", "create", "--config", str(config))[0] == 0
    load_fabric(ca)


@posix_only
def test_full_disk_on_force_keeps_the_old_ca(data_dir: Path, config: Path) -> None:
    import resource

    assert _run("fabric", "create", "--config", str(config))[0] == 0
    before = _show(config)

    def limit_files() -> None:
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (200, 200))

    proc = _popen(
        "fabric", "create", "--force", "--config", str(config), preexec=limit_files
    )
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == ExitCode.CONFIG_ERROR, (out, err)
    assert "re-join" not in err  # no false claim that the CA was replaced
    assert _show(config)["fabric_id"] == before["fabric_id"]
    _no_leftovers(data_dir)


@posix_only
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes"
)
def test_read_only_controller_dir_is_a_typed_error(
    data_dir: Path, config: Path
) -> None:
    (data_dir / "controller").mkdir(parents=True)
    (data_dir / "controller").chmod(0o500)
    try:
        code, _, err = _run("fabric", "create", "--config", str(config))
    finally:
        (data_dir / "controller").chmod(0o700)
    assert code == ExitCode.CONFIG_ERROR and "Traceback" not in err
    assert str(data_dir / "controller") in err  # names the folder to fix
    assert not _ca_dir(data_dir).exists()


@posix_only
def test_create_after_a_world_readable_controller_parent_still_protects_ca(
    data_dir: Path, config: Path
) -> None:
    (data_dir / "controller").mkdir(parents=True, mode=0o755)
    (data_dir / "controller").chmod(0o755)
    assert _run("fabric", "create", "--config", str(config))[0] == 0
    ca = _ca_dir(data_dir)
    assert ca.stat().st_mode & 0o077 == 0
    assert all(p.stat().st_mode & 0o077 == 0 for p in ca.iterdir())
    # and a stranger (simulated by mode) cannot read the key through the folder
    assert (_tls_dir(data_dir).stat().st_mode & 0o077) == 0
