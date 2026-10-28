"""V1.0 G3 (D413, reviewer M5): open ports of an installed controller = D389.

The real ``meshforge-controller`` .deb in a clean Ubuntu 24.04 systemd
container with ``ufw`` **active** (as on a hardened server): install, create
the fabric through the setup page, then ``ss -ltnH`` must show exactly the
D389 ports {47100, 47101, 47110} on non-loopback addresses and nothing else
that a LAN host could reach (the managed PostgreSQL 47150 and anything else on
loopback only). ``ufw`` must hold the "MeshForge Controller" rule while
installed; after ``apt remove`` no MeshForge rule remains - also when ufw was
switched off in between (``ufw status`` then hides rules; the package checks
``ufw show added``).

Slow; CI's ``package-linux`` job runs it (``MESHFORGE_TEST_CONTROLLER_DEB_DIR``).
``iproute2`` (``ss``) and ``ufw`` are installed in the container by the test:
both are part of a stock Ubuntu, not of the minimal test image.
"""

from __future__ import annotations

import ipaddress
import sys
from collections.abc import Iterator
from typing import Final

import pytest
from mf_deb import (
    CONTROLLER_UNIT,
    Debs,
    SystemdBox,
    built_controller_debs,
    systemd_image,
)

pytestmark = [
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.security,
    pytest.mark.timeout(3600),
    pytest.mark.skipif(sys.platform != "linux", reason="Linux package"),
]

D389_PORTS: Final = frozenset({47100, 47101, 47110})
RULE: Final = "MeshForge Controller"
PASSWORD: Final = "open-ports check passphrase 2026"
CTL_PYTHON: Final = "/opt/meshforge/controller/current/python/bin/python3.12"
SETUP_CLIENT: Final = """
import http.cookiejar, json, sys, urllib.request
url, admin, password = sys.argv[1], sys.argv[2], sys.argv[3]
base, token = url.split("/setup#token=")
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
        headers={"content-type": "application/json", "origin": base})
    with opener.open(req, timeout=120) as resp:
        return json.loads(resp.read())
call("POST", "/v1/setup/session", {"token": token})
print(json.dumps(call("POST", "/v1/setup/complete",
                      {"admin": admin, "password": password})))
"""


@pytest.fixture(scope="module")
def debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    return built_controller_debs(tmp_path_factory.mktemp("g3-ports-debs"))


@pytest.fixture
def box() -> Iterator[SystemdBox]:
    made = SystemdBox.start(systemd_image("24.04"), name_hint="g3ports")
    try:
        tools = made.apt("update -qq && DEBIAN_FRONTEND=noninteractive apt-get "
                         "install -y -qq --no-install-recommends iproute2 ufw")  # fmt: skip
        assert tools.returncode == 0, f"cannot install ss/ufw: {tools.stdout[-2000:]}"
        enabled = made.run("ufw --force enable 2>&1")
        assert enabled.returncode == 0, f"ufw cannot run here: {enabled.stdout}"
        assert "Status: active" in made.ok("ufw status")
        yield made
    finally:
        made.remove()


def listeners(box: SystemdBox) -> list[tuple[str, int]]:
    """(address, port) of every TCP listener, from ``ss -ltnH``."""
    out: list[tuple[str, int]] = []
    for line in box.ok("ss -ltnH").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        host, _, port = local.rpartition(":")
        host = host.strip("[]").split("%", 1)[0]
        out.append((host, int(port)))
    return out


def _is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:  # "*" = every address
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def exposed_problems(found: list[tuple[str, int]]) -> list[str]:
    """Every listener a LAN host could reach that D389 does not name."""
    return [
        f"{host}:{port}"
        for host, port in found
        if not _is_loopback(host) and port not in D389_PORTS
    ]


def _rule_added(box: SystemdBox) -> bool:
    return RULE in box.ok("ufw show added 2>/dev/null || true")


def _set_up(box: SystemdBox, debs: Debs) -> None:
    done = box.install(debs.first)
    assert done.returncode == 0, done.stdout[-4000:]
    url = next(
        line.split()[-1]
        for line in box.ok("meshforge-controller setup url").splitlines()
        if "/setup#token=" in line
    )
    box.ok("cat > /tmp/setup_client.py", stdin=SETUP_CLIENT)
    box.ok(f"{CTL_PYTHON} /tmp/setup_client.py '{url}' admin '{PASSWORD}'")
    box.ok(
        "for i in $(seq 90); do "
        "journalctl -u meshforge-controller -o cat | grep -q controller.listening "
        "&& ss -ltnH | grep -q ':47101 ' && exit 0; sleep 1; done; exit 1",
        timeout=200,
    )


def test_listeners_match_d389_and_ufw_rule_follows_the_package(
    box: SystemdBox, debs: Debs
) -> None:
    # before anything: the clean image exposes nothing (a baseline for the diff)
    assert exposed_problems(listeners(box)) == [], listeners(box)
    _set_up(box, debs)
    assert box.show(CONTROLLER_UNIT, "ActiveState")["ActiveState"] == "active"
    found = listeners(box)
    ports = {port for _, port in found}
    assert ports >= D389_PORTS, f"a D389 port is not listening: {sorted(found)}"
    assert exposed_problems(found) == [], (
        f"listeners beyond D389 reachable from the LAN: {exposed_problems(found)}"
    )
    pg = [host for host, port in found if port == 47150]
    assert pg and all(_is_loopback(h) for h in pg), f"PostgreSQL bound to {pg}"
    # ufw was active at install: the named profile rule is in, for D389 only
    assert _rule_added(box), box.ok("ufw show added")
    status = box.ok("ufw status verbose")
    assert RULE in status, status
    profile = box.ok(f"ufw app info '{RULE}'")
    assert "47100,47101,47110/tcp" in profile.replace(" ", ""), profile

    # remove while ufw is active: the rule goes, the ports close
    assert box.apt("remove -y meshforge-controller").returncode == 0
    assert not _rule_added(box), box.ok("ufw show added")
    assert RULE not in box.ok("ufw status")
    assert not ({port for _, port in listeners(box)} & (D389_PORTS | {47150}))


def test_no_rule_survives_a_remove_while_ufw_is_off(
    box: SystemdBox, debs: Debs
) -> None:
    """``ufw status`` lists no rules while inactive; the rule must still go."""
    _set_up(box, debs)
    assert _rule_added(box)
    box.ok("ufw disable")
    assert RULE not in box.ok("ufw status")  # hidden, yet still configured...
    assert _rule_added(box)
    assert box.apt("remove -y meshforge-controller").returncode == 0
    assert not _rule_added(box), box.ok("ufw show added")  # ...and now gone
    # and switching ufw back on does not resurrect it
    box.ok("ufw --force enable")
    assert RULE not in box.ok("ufw status")


def test_listener_parser_and_rule() -> None:
    """The ``ss`` parsing and the exposure rule on fixed text (no container)."""

    class _Fake:
        def ok(self, command: str, **_: object) -> str:
            return (
                "LISTEN 0 4096 192.168.1.20:47100 0.0.0.0:*\n"
                "LISTEN 0 4096 [::ffff:127.0.0.1]:47130 [::]:*\n"
                "LISTEN 0 4096 127.0.0.1:47150 0.0.0.0:*\n"
                "LISTEN 0 4096 [::1]:47150 [::]:*\n"
                "LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\n"
                "LISTEN 0 4096 *:9999 *:*\n"
                "LISTEN 0 4096 [fe80::1%eth0]:47110 [::]:*\n"
            )

    found = listeners(_Fake())  # type: ignore[arg-type]
    assert ("::1", 47150) in found and ("fe80::1", 47110) in found
    assert exposed_problems(found) == ["0.0.0.0:22", "*:9999"]
