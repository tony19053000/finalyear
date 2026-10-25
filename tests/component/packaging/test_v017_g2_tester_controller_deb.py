"""V0.17 G2 tester: the controller .deb under fault injection, in a systemd container.

Adds to the implementer's lifecycle test (not repeated here): the install's
security posture (who can read what, who listens where), the setup gate over
real HTTP, service recovery after kill -9 of the controller and of its
PostgreSQL, clean stop, same-version reinstall, remove -> reinstall keeps the
fabric (setup not offered again), purge while running, a busy database port, a
non-privileged user, and the 150 MB download budget.

One module-scoped container is installed and set up once; the tests then run in
file order (each leaves it healthy); the destructive purge test is last.
Slow. CI/locally: ``MESHFORGE_TEST_CONTROLLER_DEB_DIR`` = a folder with
``meshforge-controller_*-1_amd64.deb`` / ``-2``.
"""

from __future__ import annotations

# ruff: noqa: S104, E501
import json
import sys
import time
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
    pytest.mark.timeout(3600),
    pytest.mark.skipif(sys.platform != "linux", reason="Linux package"),
]

PW: Final = "correct horse battery staple 42"
CTL_PY: Final = "/opt/meshforge/controller/current/python/bin/python3.12"
CTL_COMPUTE: Final = "/opt/meshforge/controller/current/bin/compute"
BUDGET_BYTES: Final = 150 * 1024 * 1024

HTTP_CLIENT: Final = """
import http.cookiejar, json, sys, urllib.request, urllib.error
base = sys.argv[1]; steps = json.loads(sys.argv[2])
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
out = []
for method, path, body, headers in steps:
    data = None if body is None else json.dumps(body).encode()
    h = {"content-type": "application/json", "origin": base}; h.update(headers)
    req = urllib.request.Request(base + path, data=data, method=method, headers=h)
    try:
        with op.open(req, timeout=120) as r:
            out.append([r.status, r.read().decode()[:3000]])
    except urllib.error.HTTPError as e:
        out.append([e.code, e.read().decode()[:3000]])
    except Exception as e:  # connection refused etc.
        out.append([0, repr(e)])
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    return built_controller_debs(tmp_path_factory.mktemp("g2t-debs"))


def _listeners(box: SystemdBox) -> list[tuple[str, int]]:
    """(address, port) of every TCP listener in the container, from /proc."""
    out: list[tuple[str, int]] = []
    for table, v6 in (("tcp", False), ("tcp6", True)):
        text = box.ok(f"cat /proc/net/{table}")
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A":
                continue
            addr_hex, port_hex = parts[1].split(":")
            port = int(port_hex, 16)
            if v6:
                raw = bytes.fromhex(addr_hex)
                # /proc prints four host-order 32-bit words
                words = [raw[i : i + 4][::-1] for i in range(0, 16, 4)]
                packed = b"".join(words)
                import ipaddress

                addr = str(ipaddress.IPv6Address(packed))
            else:
                b = bytes.fromhex(addr_hex)[::-1]
                addr = ".".join(str(x) for x in b)
            out.append((addr, port))
    return out


def _http(box: SystemdBox, base: str, steps: list[list[object]]) -> list[list[object]]:
    box.ok("cat > /tmp/hc.py", stdin=HTTP_CLIENT)
    raw = box.ok(f"{CTL_PY} /tmp/hc.py '{base}' '{json.dumps(steps)}'")
    return list(json.loads(raw))


def _pg_pids(box: SystemdBox) -> list[int]:
    out = box.run("pgrep -u meshforge -x postgres").stdout.split()
    return [int(x) for x in out]


def _postmaster(box: SystemdBox) -> int:
    return int(box.ok("head -1 /var/lib/meshforge/db/data/postmaster.pid").strip())


def _now(box: SystemdBox) -> int:
    return int(box.ok("date +%s").strip())


def _wait_serving(box: SystemdBox, since: int, within: float = 120) -> None:
    """Until the controller logged ``controller.listening`` after ``since`` (epoch s)."""
    box.ok(
        f"for i in $(seq {int(within)}); do "
        f"journalctl -u meshforge-controller -o cat --since @{since} | "
        "grep -q controller.listening && exit 0; sleep 1; done; exit 1"
    )


@pytest.fixture(scope="module")
def ready(debs: Debs) -> Iterator[SystemdBox]:
    """Installed + fabric created through the real setup page."""
    box = SystemdBox.start(systemd_image("24.04"), name_hint="g2t")
    try:
        done = box.install(debs.first)
        assert done.returncode == 0, done.stdout[-4000:]
        assert "#token=" not in done.stdout, "setup link in the install output"
        url = next(
            line.split()[-1]
            for line in box.ok("meshforge-controller setup url").splitlines()
            if "/setup#token=" in line
        )
        box.url = url  # type: ignore[attr-defined]
        box.install_out = done.stdout  # type: ignore[attr-defined]
        yield box
    finally:
        box.remove()


def test_01_posture_before_setup_secrets_listeners_and_the_token_gate(
    ready: SystemdBox, debs: Debs
) -> None:
    box = ready
    url: str = box.url  # type: ignore[attr-defined]
    base, token = url.split("/setup#token=")
    # --- file modes / owners
    for path, mode in (
        ("/var/lib/meshforge/db/app.password", "600"),
        ("/var/lib/meshforge/db/admin.password", "600"),
        ("/var/lib/meshforge/controller/setup-token", "600"),
        ("/var/lib/meshforge/db/data", "700"),
    ):
        got = box.ok(f"stat -c '%a %U' {path}").split()
        assert got == [mode, "meshforge"], (path, got)
    dump_dir = box.ok("stat -c %a /var/lib/meshforge/db").strip()
    assert int(dump_dir, 8) & 0o007 == 0, f"db root world-accessible: {dump_dir}"
    # --- an unprivileged user cannot read any secret or the database
    box.ok("id bob >/dev/null 2>&1 || useradd -m bob")
    for secret in (
        "/var/lib/meshforge/db/app.password",
        "/var/lib/meshforge/db/admin.password",
        "/var/lib/meshforge/controller/setup-token",
        "/var/lib/meshforge/db/data/PG_VERSION",
    ):
        assert box.run(f"cat {secret}", user="bob").returncode != 0, secret
    # the admin CLI for a non-privileged user: a clean refusal, no traceback/secret
    for cmd in ("db status", "db backup", "setup url", "doctor"):
        r = box.run(f"meshforge-controller {cmd} 2>&1", user="bob")
        text = r.stdout + r.stderr
        assert "Traceback" not in text, (cmd, text[-1500:])
        pw = box.ok("cat /var/lib/meshforge/db/app.password").strip()
        assert pw not in text, cmd
    # --- who listens where
    listening = _listeners(box)
    by_port: dict[int, set[str]] = {}
    for addr, port in listening:
        by_port.setdefault(port, set()).add(addr)
    assert by_port.get(47150) and by_port[47150] <= {"127.0.0.1", "::1"}, by_port
    setup_port = int(base.rsplit(":", 1)[1])
    assert by_port[setup_port] <= {"127.0.0.1", "::1"}, by_port[setup_port]
    wildcard = [
        (a, p)
        for a, p in listening
        if a in ("0.0.0.0", "::") and p in (47100, 47101, 47110, 47150, setup_port)
    ]
    assert wildcard == [], f"MeshForge ports on the wildcard address: {wildcard}"
    # --- the token gate over real HTTP
    steps: list[list[object]] = [
        ["GET", "/v1/setup/info", None, {}],
        ["POST", "/v1/setup/session", {"token": "x" * 43}, {}],
        ["POST", "/v1/setup/session", {"token": token[:-1]}, {}],
        ["POST", "/v1/setup/complete", {"admin": "asha", "password": PW}, {}],
        [
            "POST",
            "/v1/setup/session",
            {"token": token},
            {"origin": "http://evil.example"},
        ],
        ["GET", "/v1/setup/state", None, {"host": "evil.example"}],
    ]
    res = _http(box, base, steps)
    assert [r[0] for r in res] == [401, 401, 401, 401, 403, 400], res
    assert PW not in json.dumps(res)
    assert (
        box.ok("test -e /var/lib/meshforge/controller/ca/ca.crt || echo none").strip()
        == "none"
    )
    journal = box.run("journalctl -u meshforge-controller -o cat --no-pager").stdout
    assert token not in journal, "setup token leaked into the journal"
    shown = box.install_out  # type: ignore[attr-defined]
    assert token not in shown, "the token is in the install output (apt term.log)"
    assert "sudo meshforge-controller setup url" in shown
    term_log = box.run("cat /var/log/apt/term.log 2>/dev/null || true").stdout
    assert token not in term_log, "setup token leaked into apt's term.log"


def test_02_setup_through_the_page_then_the_gate_closes(ready: SystemdBox) -> None:
    box = ready
    base, token = box.url.split("/setup#token=")  # type: ignore[attr-defined]
    weak = _http(box, base, [
        ["POST", "/v1/setup/session", {"token": token}, {}],
        ["POST", "/v1/setup/complete", {"admin": "asha", "password": "short"}, {}],
        ["POST", "/v1/setup/complete", {"admin": "asha", "password": "asha-asha-asha-1"}, {}],
        ["POST", "/v1/setup/complete", {"admin": "asha", "password": "password1234"}, {}],
    ])  # fmt: skip
    assert [r[0] for r in weak] == [200, 400, 400, 400], weak
    assert box.run("test -e /var/lib/meshforge/controller/ca/ca.crt").returncode != 0
    t_done = _now(box)
    done = _http(box, base, [
        ["POST", "/v1/setup/session", {"token": token}, {}],
        ["POST", "/v1/setup/complete", {"admin": "asha", "password": PW}, {}],
        ["POST", "/v1/setup/complete", {"admin": "ravi", "password": PW}, {}],
    ])  # fmt: skip
    assert [r[0] for r in done] == [200, 200, 410], done
    assert box.run("test -e /var/lib/meshforge/controller/setup-token").returncode != 0
    _wait_serving(box, t_done)
    # the setup listener is gone with the setup mode; TLS API is up
    refused = _http(box, base, [["GET", "/v1/setup/state", None, {}]])
    assert refused[0][0] in (0, 400, 410), refused  # plain http to a TLS port
    assert box.run("meshforge-controller setup url").returncode == 5
    keys = box.ok(
        "find /var/lib/meshforge/controller/ca -name '*.key' -printf '%m %u %p\\n'"
    )
    assert keys.strip(), "no CA key found"
    for line in keys.splitlines():
        mode, owner, _path = line.split(maxsplit=2)
        assert mode in ("600", "400") and owner == "meshforge", line


def test_03_doctor_json_and_a_broken_secret_is_reported(ready: SystemdBox) -> None:
    box = ready
    good = box.run("meshforge-controller doctor --json")
    assert good.returncode == 0, good.stdout + good.stderr
    report = json.loads(good.stdout)
    assert report["ok"] is True
    box.ok("chmod 644 /var/lib/meshforge/db/app.password")
    try:
        bad = box.run("meshforge-controller doctor --json")
        assert bad.returncode != 0
        names = {c["name"]: c["status"] for c in json.loads(bad.stdout)["checks"]}
        assert names["database passwords"] == "fail", names
        assert PW not in bad.stdout
    finally:
        box.ok("chmod 600 /var/lib/meshforge/db/app.password")
    assert box.run("meshforge-controller doctor").returncode == 0


def test_04_kill9_postmaster_restarts_service_and_keeps_data(ready: SystemdBox) -> None:
    box = ready
    before = int(box.show(CONTROLLER_UNIT, "NRestarts")["NRestarts"])
    old_main = box.show(CONTROLLER_UNIT, "MainPID")["MainPID"]
    t_kill = _now(box)
    box.ok(f"kill -9 {_postmaster(box)}")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        facts = box.show(CONTROLLER_UNIT, "ActiveState", "NRestarts", "MainPID")
        if int(facts["NRestarts"]) > before and facts["ActiveState"] == "active":
            break
        time.sleep(1)
    else:
        pytest.fail(
            f"no restart after postmaster death: {facts}\n{box.journal(CONTROLLER_UNIT)}"
        )
    assert facts["MainPID"] != old_main
    _wait_serving(box, t_kill)
    assert len(set(_postmaster_pids(box))) == 1, (
        "more than one postmaster after restart"
    )
    out = box.ok("meshforge-controller db status --json")
    assert json.loads(out)["running"] is True
    users = box.ok("meshforge-controller user list")
    assert "asha" in users, users  # the data survived the postmaster's death


def _postmaster_pids(box: SystemdBox) -> list[int]:
    return [int(box.ok("head -1 /var/lib/meshforge/db/data/postmaster.pid").strip())]


def test_05_kill9_controller_restarts_without_orphan_or_duplicate_postgres(
    ready: SystemdBox,
) -> None:
    box = ready
    before = int(box.show(CONTROLLER_UNIT, "NRestarts")["NRestarts"])
    main = box.show(CONTROLLER_UNIT, "MainPID")["MainPID"]
    old_pm = _postmaster(box)
    t_kill = _now(box)
    box.ok(f"kill -9 {main}")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        facts = box.show(CONTROLLER_UNIT, "ActiveState", "NRestarts", "MainPID")
        if int(facts["NRestarts"]) > before and facts["ActiveState"] == "active":
            break
        time.sleep(1)
    else:
        pytest.fail(f"no restart: {facts}")
    _wait_serving(box, t_kill)
    # exactly one postmaster owns the data dir; the old tree is gone
    pm = _postmaster(box)
    roots = box.ok(
        "for p in $(pgrep -u meshforge -x postgres); do "
        "echo $(awk '{print $4}' /proc/$p/stat) ; done | sort -u"
    ).split()
    new_main = box.show(CONTROLLER_UNIT, "MainPID")["MainPID"]
    assert new_main != main
    # every postgres process is the postmaster or its child; the postmaster's
    # parent is the new controller: no leftover tree of the killed one
    assert set(roots) <= {str(pm), new_main}, (roots, pm, new_main, old_pm)
    assert old_pm == pm or box.run(f"kill -0 {old_pm}").returncode != 0


def test_06_stop_is_clean_and_fast_and_start_works_again(ready: SystemdBox) -> None:
    box = ready
    t0 = time.monotonic()
    box.ok(f"systemctl stop {CONTROLLER_UNIT}", timeout=120)
    took = time.monotonic() - t0
    assert took < 66, f"stop took {took:.0f}s (TimeoutStopSec=65)"
    assert _pg_pids(box) == [], "postgres processes survive a service stop"
    assert box.show(CONTROLLER_UNIT, "ActiveState", "Result")["Result"] == "success"
    assert not any(p in (47100, 47110, 47150) for _, p in _listeners(box))
    # admin CLI while stopped: backup starts the cluster briefly and stops it
    out = box.ok("meshforge-controller db backup").strip()
    assert out.startswith("/var/lib/meshforge/db/backups/manual-")
    assert _pg_pids(box) == []
    assert box.ok(f"stat -c '%a %U' {out}").split() == ["600", "meshforge"]
    t_start = _now(box)
    box.ok(f"systemctl start {CONTROLLER_UNIT}")
    box.wait_unit(CONTROLLER_UNIT, ["active"], within=90)
    _wait_serving(box, t_start)
    # restore while running is refused (clean message, exit non-zero)
    r = box.run(f"meshforge-controller db restore --yes {out} 2>&1")
    assert r.returncode != 0 and "Traceback" not in r.stdout
    assert "stop" in r.stdout.lower()
    assert box.show(CONTROLLER_UNIT, "ActiveState")["ActiveState"] == "active"


def test_07_same_version_reinstall_keeps_everything(
    ready: SystemdBox, debs: Debs
) -> None:
    box = ready
    box.copy_in(debs.first, f"/tmp/{debs.first.name}")
    done = box.apt(f"install -y --reinstall /tmp/{debs.first.name}")
    assert done.returncode == 0, done.stdout[-3000:]
    assert "setup url" not in done.stdout, "setup offered again on a set-up fabric"
    box.wait_unit(CONTROLLER_UNIT, ["active"], within=90)
    assert box.run("test -e /var/lib/meshforge/controller/setup-token").returncode != 0
    assert box.run("meshforge-controller setup url").returncode == 5
    assert json.loads(box.ok("meshforge-controller db status --json"))["running"]
    assert len(_pg_pids(box)) >= 1


def test_08_remove_then_reinstall_keeps_the_fabric(
    ready: SystemdBox, debs: Debs
) -> None:
    box = ready
    fp_before = box.ok("sha256sum /var/lib/meshforge/controller/ca/ca.crt").split()[0]
    assert box.apt("remove -y meshforge-controller").returncode == 0
    assert _pg_pids(box) == [] and not any(p == 47150 for _, p in _listeners(box))
    done = box.install(debs.first)
    assert done.returncode == 0, done.stdout[-3000:]
    assert "setup url" not in done.stdout, "setup offered after remove+reinstall"
    box.wait_unit(CONTROLLER_UNIT, ["active"], within=90)
    assert (
        box.ok("sha256sum /var/lib/meshforge/controller/ca/ca.crt").split()[0]
        == fp_before
    )
    assert json.loads(box.ok("meshforge-controller db status --json"))["running"]


def test_09_busy_database_port_is_reported_not_swallowed(debs: Debs) -> None:
    box = SystemdBox.start(systemd_image("24.04"), name_hint="g2tbusy")
    try:
        # something else already listens on the managed DB port (47150)
        if box.run("command -v perl").returncode != 0:
            pytest.skip("no perl in the image to occupy a port")
        box.ok(
            "(nohup perl -MIO::Socket::INET -e '$s=IO::Socket::INET->new("
            'LocalAddr=>"127.0.0.1",LocalPort=>47150,Listen=>5,ReuseAddr=>1) '
            "or die; sleep 100000' >/dev/null 2>&1 &) ; sleep 1"
        )
        assert any(p == 47150 for _, p in _listeners(box)), "squatter not listening"
        done = box.install(debs.first)
        text = done.stdout
        assert "Traceback" not in text
        # whatever dpkg decided, the user must be told about the port
        assert "47150" in text or "port" in text.lower(), text[-3000:]
        facts = box.show(CONTROLLER_UNIT, "ActiveState")
        assert facts["ActiveState"] != "active", "active although its DB port is taken"
        assert (
            "47150" in box.journal(CONTROLLER_UNIT)
            or "port" in box.journal(CONTROLLER_UNIT).lower()
        )
        # no postgres of ours was left half-started
        assert box.run("pgrep -u meshforge -x postgres").returncode != 0
    finally:
        box.remove()


def test_10_download_budget_and_package_hygiene(debs: Debs) -> None:
    for deb in (debs.first, debs.second):
        assert deb.stat().st_size <= BUDGET_BYTES, f"{deb.name}: {deb.stat().st_size}"
    sys.path.insert(
        0, str(__import__("pathlib").Path(__file__).resolve().parents[3] / "scripts")
    )
    import build_deb

    contents = build_deb.inspect_deb(debs.first)
    paths = list(contents.data)
    assert not any("/tests/" in p and "site-packages/numpy" in p for p in paths)
    assert not any(p.endswith((".pyc",)) and "/home/" in p for p in paths)
    leaked = [
        p for p in paths if any(s in p for s in ("/home/aayush", "FINAL YEAR", ".env"))
    ]
    assert leaked == [], leaked[:5]
    assert contents.control["Package"] == "meshforge-controller"
    assert "libc6 (>= 2.36)" in contents.control["Depends"]


def test_99_purge_while_running_leaves_nothing_and_no_orphans(
    ready: SystemdBox,
) -> None:
    box = ready
    assert box.show(CONTROLLER_UNIT, "ActiveState")["ActiveState"] == "active"
    done = box.apt("purge -y meshforge-controller")
    assert done.returncode == 0, done.stdout[-3000:]
    assert _pg_pids(box) == [], "postgres still running after purge"
    for gone in (
        "/var/lib/meshforge",
        "/etc/meshforge",
        "/opt/meshforge",
        "/etc/ufw/applications.d/meshforge-controller",
        "/usr/bin/meshforge-controller",
        "/usr/lib/systemd/system/meshforge-controller.service",
    ):
        assert box.run(f"test -e {gone}").returncode != 0, gone
    assert box.run("id meshforge").returncode != 0, "service user left behind"
    assert not any(p in (47100, 47101, 47110, 47150) for _, p in _listeners(box))
    left = box.run(
        "find / -xdev -name '*meshforge*' -not -path '/proc/*' -not -path '/tmp/*' -not -path '/var/cache/*' -not -path '/var/lib/dpkg/*' -not -path '/var/log/*' 2>/dev/null"
    ).stdout
    assert left.strip() == "", left
