# ruff: noqa: UP031
"""V0.17 G3 tester: the agent console as installed by the .deb in a systemd
container (Ubuntu 22.04 and 24.04): token file permissions as other local users
see them, loopback-only listener, Host guard over the real socket, upgrade from
the G1 package (path unit gone), remove, purge, ``lab/verify_uninstall.py``.

Slow: ``--run-slow``. The G3 packages come from ``$MESHFORGE_TEST_DEB_DIR`` (a -1
and a -2); the G1 package for the upgrade test from ``$MESHFORGE_TEST_G1_DEB``
(that test is skipped when unset).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from mf_deb import UNIT, Debs, SystemdBox, built_debs, systemd_image

pytestmark = [
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.timeout(1500),
    pytest.mark.skipif(sys.platform != "linux", reason="Linux package"),
]

PORT = 47130
TOKEN = "/run/meshforge-console/token"


@pytest.fixture(scope="module")
def debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    return built_debs(tmp_path_factory.mktemp("debs"))


@pytest.fixture(params=["22.04", "24.04"])
def box(request: pytest.FixtureRequest) -> Iterator[SystemdBox]:
    made = SystemdBox.start(systemd_image(request.param), name_hint="g3con")
    try:
        yield made
    finally:
        made.remove()


def _curl(box: SystemdBox, args: str) -> str:
    """curl inside the container (the image has python3 only: use it)."""
    return box.run(f"curl -s -o /dev/null -w '%{{http_code}}' {args}").stdout.strip()


def _install(box: SystemdBox, deb: Path, **env: str) -> None:
    box.copy_in(deb, f"/tmp/{deb.name}")
    done = box.run(
        f"DEBIAN_FRONTEND=noninteractive apt-get install -y /tmp/{deb.name} 2>&1",
        env=env,
        timeout=600,
    )
    assert done.returncode == 0, done.stdout[-3000:]


def _py_http(box: SystemdBox, request: str) -> str:
    """One HTTP exchange from inside the box (python3, raw socket)."""
    code = (
        "import socket,sys;s=socket.create_connection(('127.0.0.1',%d),5);"
        "s.sendall(sys.stdin.buffer.read().replace(b'\\n',b'\\r\\n'));"
        "d=b''\n"
        "while True:\n"
        " c=s.recv(65536)\n"
        " if not c: break\n"
        " d+=c\n"
        "print(d.decode('latin-1'))"
    ) % PORT
    return box.run(
        f'/opt/meshforge/current/python/bin/python3 -c "{code}"', stdin=request
    ).stdout


def _status(blob: str) -> int:
    return int(blob.split(None, 2)[1]) if blob.startswith("HTTP/") else -1


def _listeners(box: SystemdBox) -> list[str]:
    """Local addresses LISTENing on the console port (from /proc/net/tcp{,6})."""
    hexport = f"{PORT:04X}"
    out = box.run(
        "cat /proc/net/tcp /proc/net/tcp6 | awk '$4==\"0A\" {print $2}'"
    ).stdout.split()
    return [a for a in out if a.endswith(":" + hexport)]


def _health(box: SystemdBox) -> int:
    return _status(
        _py_http(
            box,
            f"GET /console/v1/health HTTP/1.1\nHost: 127.0.0.1:{PORT}\nConnection: close\n\n",
        )
    )


def _launch(box: SystemdBox, token: str) -> int:
    return _status(
        _py_http(
            box,
            f"POST /console/v1/launch-code HTTP/1.1\nHost: 127.0.0.1:{PORT}\n"
            f"Authorization: Bearer {token}\nContent-Length: 0\nConnection: close\n\n",
        )
    )


def test_fresh_install_console_is_up_before_any_join_and_locked_down(
    box: SystemdBox, debs: Debs
) -> None:
    box.ok("useradd --create-home --shell /bin/sh owner")
    box.ok("useradd --create-home --shell /bin/sh stranger")
    _install(box, debs.first, SUDO_USER="owner")
    facts = box.wait_unit(UNIT, ["active"], within=90)
    assert int(facts["MainPID"]) > 0 and facts["NRestarts"] == "0", box.journal()
    units = box.run("systemctl list-unit-files 'meshforge*'").stdout
    assert "meshforge-agent.path" not in units, units
    failed = box.run("systemctl --failed --no-legend").stdout.strip()
    assert failed == "", failed

    assert (
        box.ok("stat -c '%a %U:%G' /run/meshforge-console").strip()
        == "2750 meshforge:meshforge-admin"
    )
    assert (
        box.ok(f"stat -c '%a %U:%G' {TOKEN}").strip() == "640 meshforge:meshforge-admin"
    )
    assert "owner" in box.ok("getent group meshforge-admin")
    assert "stranger" not in box.ok("getent group meshforge-admin")

    assert box.run(f"cat {TOKEN}", user="stranger").returncode != 0
    assert box.run("ls /run/meshforge-console", user="stranger").returncode != 0
    assert box.run(f"cat {TOKEN}", user="nobody").returncode != 0
    token = box.run(f"sg meshforge-admin -c 'cat {TOKEN}'", user="owner").stdout.strip()
    assert len(token) >= 40
    assert box.run(f"cat {TOKEN}", user="meshforge").stdout.strip() == token

    assert _listeners(box) == [f"0100007F:{PORT:04X}"], _listeners(box)

    assert _health(box) == 200
    rebinding = _py_http(
        box,
        f"GET /console/v1/health HTTP/1.1\nHost: evil.example:{PORT}\nConnection: close\n\n",
    )
    assert _status(rebinding) == 421, rebinding
    unsigned = _py_http(
        box,
        f"GET /console/v1/status HTTP/1.1\nHost: 127.0.0.1:{PORT}\nConnection: close\n\n",
    )
    assert _status(unsigned) == 401
    assert _launch(box, "nope") == 401
    assert _launch(box, token) == 200
    assert token not in box.journal(lines=500), "token leaked into the journal"
    events = [e.get("event") for e in box.journal_events()]
    assert "agent.console.listening" in events, events

    box.ok("systemctl restart meshforge-agent")
    box.wait_unit(UNIT, ["active"], within=60)
    new = token
    for _ in range(40):
        new = box.run(f"cat {TOKEN}", user="meshforge").stdout.strip()
        if new and new != token:
            break
        box.run("sleep 0.5")
    assert new != token
    assert _launch(box, token) == 401, "the old token still works after a restart"

    _install(box, debs.second)
    box.wait_unit(UNIT, ["active"], within=90)
    assert (
        box.ok(f"stat -c '%a %U:%G' {TOKEN}").strip() == "640 meshforge:meshforge-admin"
    )
    assert _health(box) == 200

    box.ok("DEBIAN_FRONTEND=noninteractive apt-get remove -y meshforge-agent 2>&1")
    assert box.run(f"test -e {TOKEN}").returncode != 0
    assert _listeners(box) == []
    done = box.verify_uninstall(data_removed=False)
    assert done.returncode == 0, done.stdout + done.stderr
    _install(box, debs.second)
    box.wait_unit(UNIT, ["active"], within=90)
    box.ok("DEBIAN_FRONTEND=noninteractive apt-get purge -y meshforge-agent 2>&1")
    assert box.run("test -e /run/meshforge-console").returncode != 0
    assert box.run("getent group meshforge-admin").returncode != 0
    done = box.verify_uninstall(data_removed=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_upgrade_from_the_g1_package_drops_the_path_unit_and_starts_the_console(
    box: SystemdBox, debs: Debs
) -> None:
    g1 = os.environ.get("MESHFORGE_TEST_G1_DEB")
    if not g1:
        pytest.skip("MESHFORGE_TEST_G1_DEB (a G1 build of the .deb) is not set")
    box.ok("useradd --create-home --shell /bin/sh owner")
    _install(box, Path(g1), SUDO_USER="owner")
    assert (
        box.run("systemctl is-enabled meshforge-agent.path").stdout.strip() == "enabled"
    )
    assert (
        box.run("systemctl is-active meshforge-agent.service").stdout.strip()
        != "active"
    )
    _install(box, debs.second)
    facts = box.wait_unit(UNIT, ["active"], within=90)
    assert facts["NRestarts"] == "0", box.journal()
    assert (
        box.run("test -e /usr/lib/systemd/system/meshforge-agent.path").returncode != 0
    )
    assert (
        box.run("systemctl is-enabled meshforge-agent.path").stdout.strip() != "enabled"
    )
    assert (
        box.run("systemctl is-active meshforge-agent.path").stdout.strip() != "active"
    )
    assert (
        box.run("systemctl is-enabled meshforge-agent.service").stdout.strip()
        == "enabled"
    )
    assert _health(box) == 200
    assert box.run("systemctl --failed --no-legend").stdout.strip() == ""
    box.ok("DEBIAN_FRONTEND=noninteractive apt-get remove -y meshforge-agent 2>&1")
    done = box.verify_uninstall(data_removed=False)
    assert done.returncode == 0, done.stdout + done.stderr
    box.ok("DEBIAN_FRONTEND=noninteractive apt-get purge -y meshforge-agent 2>&1")
    done = box.verify_uninstall(data_removed=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_package_files_for_the_console(box: SystemdBox, debs: Debs) -> None:
    _install(box, debs.first)
    box.wait_unit(UNIT, ["active"], within=90)
    desktop = box.ok("cat /usr/share/applications/meshforge.desktop")
    assert "Exec=" in desktop and "meshforge-console-open" in desktop
    assert box.run("test -x /usr/bin/meshforge-console-open").returncode == 0
    policy = box.ok(
        "cat /usr/share/polkit-1/actions/org.meshforge.docker-access.policy"
    )
    assert "org.meshforge.docker-access" in policy and "auth_admin" in policy
    bad = box.ok(
        "find /opt/meshforge /usr/lib/meshforge /usr/share/polkit-1 "
        "/usr/share/applications -perm -0002 ! -type l 2>/dev/null | head"
    )
    assert bad.strip() == "", bad
    assert (
        box.ok("stat -c '%U %a' /usr/lib/meshforge/docker-access").split()[0] == "root"
    )
