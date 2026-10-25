"""V0.17 G2 (D390/D392/D393/D398): the real meshforge-controller .deb in clean
Ubuntu systemd containers - the brief's whole package scenario.

24.04, in the order a controller machine lives through it:

1. ``apt install ./meshforge-controller_<v>-1_amd64.deb`` with no Docker, no
   PostgreSQL and no Python on the machine: ``controller.toml`` written,
   ``doctor`` passes, the service runs its own PostgreSQL as ``meshforge``, and
   the install output carries the one-time setup link;
2. the setup page over HTTP (the link's token): fabric CA, admin, first invite;
   the controller restarts itself into TLS; ``compute login`` works;
3. ``db status`` / ``db backup`` by a human (re-executed as the service user);
4. the **agent package next to it**: joins with the setup page's code, runs a
   job submitted with ``compute run`` (the job succeeds, its output comes back);
5. upgrade to revision 2 from an "older" version (the marker says 0.0.9): a
   ``pre-<v>.dump`` backup appears, the data (the job) is kept, the agent stays up;
6. ``apt remove`` keeps the fabric and leaves the agent running; ``apt purge``
   removes the controller's state only (the agent's state and the shared user
   stay); purging the agent then leaves nothing.

22.04: the install fails cleanly on ``libc6 (>= 2.36)`` (D398) - dpkg leaves
nothing configured, no service, no user state.

Slow: builds both bundles and packages (CI passes them in with
``MESHFORGE_TEST_DEB_DIR`` / ``MESHFORGE_TEST_CONTROLLER_DEB_DIR``).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from mf_deb import (
    CONTROLLER_UNIT,
    UNIT,
    Debs,
    SystemdBox,
    built_controller_debs,
    built_debs,
    systemd_image,
)

pytestmark = [
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.timeout(3600),
    pytest.mark.skipif(sys.platform != "linux", reason="Linux package"),
]

PASSWORD: Final = "correct horse battery staple 42"
CTL_COMPUTE: Final = "/opt/meshforge/controller/current/bin/compute"
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
info = call("GET", "/v1/setup/info")
print(json.dumps({"info": info,
    "result": call("POST", "/v1/setup/complete",
                   {"admin": admin, "password": password})}))
"""
MANIFEST: Final = (
    "name: g2-smoke\n"
    'entrypoint: [sh, -c, "mkdir -p results && echo made-by-g2 > results/r.txt '
    '&& echo hello-from-g2"]\n'
    "outputs: [results/]\n"
)


@pytest.fixture(scope="module")
def controller_debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    return built_controller_debs(tmp_path_factory.mktemp("controller-debs"))


@pytest.fixture(scope="module")
def agent_debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    return built_debs(tmp_path_factory.mktemp("agent-debs"))


def _box(ubuntu: str) -> Iterator[SystemdBox]:
    made = SystemdBox.start(systemd_image(ubuntu), name_hint=f"ctl{ubuntu}")
    try:
        yield made
    finally:
        made.remove()


@pytest.fixture
def box2404() -> Iterator[SystemdBox]:
    yield from _box("24.04")


@pytest.fixture
def box2204() -> Iterator[SystemdBox]:
    yield from _box("22.04")


def _setup(box: SystemdBox, url: str) -> dict[str, dict[str, object]]:
    box.ok("cat > /tmp/setup_client.py", stdin=SETUP_CLIENT)
    out = box.ok(f"{CTL_PYTHON} /tmp/setup_client.py '{url}' admin '{PASSWORD}'")
    return dict(json.loads(out))


def test_controller_package_lifecycle_with_an_agent_next_to_it(
    box2404: SystemdBox, controller_debs: Debs, agent_debs: Debs
) -> None:
    box = box2404
    assert box.run("command -v python3 psql postgres docker || true").stdout == ""

    # 1. install
    done = box.install(controller_debs.first)
    assert done.returncode == 0, done.stdout[-4000:]
    out = done.stdout
    assert "wrote /etc/meshforge/controller.toml" in out
    assert "all checks passed" in out
    assert "is running" in out, out[-3000:]
    assert "Create your fabric" in out
    url = next(line.split()[-1] for line in out.splitlines() if "/setup#token=" in line)
    facts = box.show(CONTROLLER_UNIT, "ActiveState", "User", "MainPID")
    assert facts["ActiveState"] == "active" and facts["User"] == "meshforge"
    owner = box.ok("stat -c %U /var/lib/meshforge/db/data").strip()
    assert owner == "meshforge"
    assert box.ok("stat -c %a /var/lib/meshforge/db/app.password").strip() == "600"
    pg = box.ok("pgrep -u meshforge -c postgres || true").strip()
    assert int(pg or 0) >= 1

    # 2. setup page, restart into TLS, compute login
    setup = _setup(box, url)
    result = setup["result"]
    invite = result["invite"]
    assert isinstance(invite, dict) and str(invite["code"]).startswith("mf1-")
    box.ok(
        "for i in $(seq 60); do test -e /var/lib/meshforge/controller/tls && "
        "journalctl -u meshforge-controller -o cat | grep -q controller.listening "
        "&& exit 0; sleep 1; done; exit 1"
    )
    address = str(invite["address"]).rsplit(":", 1)[0]
    login = box.ok(
        f"{CTL_COMPUTE} login --controller https://{address}:47110 "
        f"--fingerprint {result['ca_fingerprint']} -u admin --password-stdin",
        stdin=PASSWORD + "\n",
    )
    assert "logged in" in login
    assert box.run("meshforge-controller setup url").returncode == 5  # set up

    # 3. db commands by a human (re-executed as meshforge via sudo)
    status = box.ok("meshforge-controller db status --json")
    assert json.loads(status)["running"] is True
    backup = box.ok("meshforge-controller db backup").strip()
    assert backup.startswith("/var/lib/meshforge/db/backups/manual-")

    # 4. the agent package on the same machine: join, run a job
    done = box.install(agent_debs.first)
    assert done.returncode == 0, done.stdout[-4000:]
    joined = box.ok(f"echo {invite['code']} | meshforge-agent join -")
    assert "joined fabric" in joined
    box.ok("meshforge-agent policy set accept_jobs=true allow_unsandboxed=true")
    box.ok(f"systemctl restart {UNIT}")
    box.wait_unit(UNIT, ["active"], within=60)
    box.ok(
        f"for i in $(seq 60); do {CTL_COMPUTE} nodes | grep -q ' yes ' && exit 0; "
        "sleep 1; done; exit 1"
    )
    box.ok("mkdir -p /root/proj && cat > /root/proj/compute.yaml", stdin=MANIFEST)
    ran = box.ok(f"cd /root && {CTL_COMPUTE} run /root/proj -f", timeout=600)
    assert "hello-from-g2" in ran
    job_id = ran.split("job_", 1)[1].split()[0]
    box.ok(f"cd /root && {CTL_COMPUTE} get job_{job_id} --dest /root/out")
    assert box.ok("cat /root/out/results/r.txt").strip() == "made-by-g2"

    # 5. upgrade from an "older" version: backup first, data kept
    box.ok(
        'sed -i \'s/"version": "[^"]*"/"version": "0.0.9"/\' '
        "/var/lib/meshforge/db/schema-version.json"
    )
    done = box.install(controller_debs.second)
    assert done.returncode == 0, done.stdout[-4000:]
    assert "is running" in done.stdout, done.stdout[-3000:]
    dumps = box.ok("ls /var/lib/meshforge/db/backups").split()
    assert f"pre-{controller_debs.bundle_version}.dump" in dumps, dumps
    jobs = box.ok(f"{CTL_COMPUTE} jobs --json")
    assert f"job_{job_id}" in jobs
    assert box.show(UNIT, "ActiveState")["ActiveState"] == "active"

    # 6. remove keeps the fabric; purge removes only the controller's state
    assert box.apt("remove -y meshforge-controller").returncode == 0
    assert box.show(CONTROLLER_UNIT, "ActiveState")["ActiveState"] == "inactive"
    assert box.run("test -d /var/lib/meshforge/db/data").returncode == 0
    assert box.run("test -e /opt/meshforge/controller").returncode != 0
    assert box.run("pgrep -u meshforge postgres").returncode != 0
    assert box.show(UNIT, "ActiveState")["ActiveState"] == "active"
    assert box.apt("purge -y meshforge-controller").returncode == 0
    for gone in (
        "/var/lib/meshforge/db",
        "/var/lib/meshforge/controller",
        "/etc/meshforge/controller.toml",
        "/etc/ufw/applications.d/meshforge-controller",
    ):
        assert box.run(f"test -e {gone}").returncode != 0, gone
    assert box.run("test -d /var/lib/meshforge/identity").returncode == 0
    assert box.run("id meshforge").returncode == 0
    assert box.show(UNIT, "ActiveState")["ActiveState"] == "active"
    assert box.apt("purge -y meshforge-agent").returncode == 0
    for gone in ("/var/lib/meshforge", "/etc/meshforge", "/opt/meshforge"):
        assert box.run(f"test -e {gone}").returncode != 0, gone
    assert box.run("id meshforge").returncode != 0


def test_agent_purge_keeps_the_controller(
    box2404: SystemdBox, controller_debs: Debs, agent_debs: Debs
) -> None:
    box = box2404
    assert box.install(controller_debs.first).returncode == 0
    assert box.install(agent_debs.first).returncode == 0
    assert box.apt("purge -y meshforge-agent").returncode == 0
    assert box.run("test -d /var/lib/meshforge/db/data").returncode == 0
    assert box.run("test -e /etc/meshforge/controller.toml").returncode == 0
    assert box.run("test -e /var/lib/meshforge/identity").returncode != 0
    assert box.run("id meshforge").returncode == 0
    facts = box.wait_unit(CONTROLLER_UNIT, ["active"], within=60)
    assert facts["ActiveState"] == "active"


def test_ubuntu_2204_install_fails_cleanly_on_glibc(
    box2204: SystemdBox, controller_debs: Debs
) -> None:
    done = box2204.install(controller_debs.first)
    assert done.returncode != 0
    assert "libc6 (>= 2.36)" in done.stdout, done.stdout[-3000:]
    status = box2204.run(
        "dpkg-query -W -f='${db:Status-Status}' meshforge-controller"
    ).stdout
    assert status in ("", "not-installed"), status
    assert box2204.run("test -e /var/lib/meshforge/db").returncode != 0
    assert box2204.run(f"systemctl cat {CONTROLLER_UNIT}").returncode != 0


def test_package_files_are_shipped(controller_debs: Debs) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    import build_deb

    contents = build_deb.inspect_deb(controller_debs.first)
    assert contents.control["Package"] == "meshforge-controller"
    assert "libc6 (>= 2.36)" in contents.control["Depends"]
    assert "./usr/bin/compute" not in contents.data  # the agent package owns it
    assert not any(p.startswith("./opt/meshforge/0") for p in contents.data)
