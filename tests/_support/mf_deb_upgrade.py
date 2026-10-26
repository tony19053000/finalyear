"""Helpers of the V0.17 G5 upgrade/rollback package tests (real ``.deb`` in
systemd containers): a seeded per-user laptop, the pointer/binary invariant,
the uninstall verifier with its JSON kept as a CI artefact, the Q38
forced-failure trigger, and a controller + agent fabric with one finished job.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

from mf_deb import UNIT, Debs, SystemdBox

AGENT_OPT: Final = "/opt/meshforge"
CTL_OPT: Final = "/opt/meshforge/controller"
REPORTS_ENV: Final = "MESHFORGE_TEST_REPORT_DIR"
"""Where :func:`verify` keeps each verdict's JSON (CI uploads the folder)."""
LAPTOP_CONFIG: Final = """\
[agent]
controller_address = "127.0.0.1:47199"

[agent.discovery]
enabled = false

[agent.scores]
enabled = false
"""
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
print(json.dumps({"result": call("POST", "/v1/setup/complete",
                                 {"admin": admin, "password": password})}))
"""
PASSWORD: Final = "correct horse battery staple 42"
CTL_COMPUTE: Final = f"{CTL_OPT}/current/bin/compute"
CTL_PYTHON: Final = f"{CTL_OPT}/current/python/bin/python3.12"
MANIFEST: Final = 'name: g5-smoke\nentrypoint: [sh, -c, "echo hello-from-g5"]\n'


def seed_laptop(box: SystemdBox, identity: Path) -> None:
    """A user who ran the agent from a checkout: identity + config in HOME."""
    home = "/home/asha"
    target = f"{home}/.local/share/meshforge/identity"
    box.ok("useradd --create-home --shell /bin/sh asha")
    box.ok(f"install -d -o asha -g asha -m 0700 {home}/.local/share/meshforge")
    box.copy_in(identity, target)
    box.ok(f"chown -R asha:asha {target} && chmod 0700 {target}")
    box.ok(f"install -d -o asha -g asha -m 0700 {home}/.config/meshforge")
    box.ok(
        f"cat > {home}/.config/meshforge/config.toml && chown asha:asha "
        f"{home}/.config/meshforge/config.toml && chmod 0600 "
        f"{home}/.config/meshforge/config.toml",
        stdin=LAPTOP_CONFIG,
    )


def invariant(box: SystemdBox, unit: str, opt: str) -> str:
    """The service's interpreter lives under what ``current`` resolves to;
    returns that folder (relative to ``opt``)."""
    pid = box.show(unit, "MainPID")["MainPID"]
    assert pid not in ("", "0"), box.journal(unit)
    target = box.ok(f"readlink -f {opt}/current").strip()
    # the launcher execs python; give it a moment if the start is very fresh
    exe = box.ok(
        f"for i in $(seq 20); do e=$(readlink -f /proc/{pid}/exe); case $e in "
        f"*/python*) echo $e; exit 0;; esac; sleep 0.5; done; "
        f"readlink -f /proc/{pid}/exe"
    ).strip()
    assert exe.startswith(target + "/"), (exe, target)
    return target.removeprefix(opt + "/")


def save_report(name: str, text: str) -> None:
    folder = os.environ.get(REPORTS_ENV)
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        (Path(folder) / name).write_text(text, encoding="utf-8")


def verify(
    box: SystemdBox, name: str, *, data_removed: bool, package: str = "all"
) -> dict[str, object]:
    """``verify_uninstall --json`` must say clean; the JSON is kept."""
    done = box.verify_uninstall(
        data_removed=data_removed, package=package, as_json=True
    )
    save_report(f"verify-uninstall-{name}.json", done.stdout)
    assert done.returncode == 0, done.stdout + done.stderr
    verdict = dict(json.loads(done.stdout))
    assert verdict["clean"] is True, verdict
    return verdict


def trigger(box: SystemdBox, opt: str, version: str) -> None:
    """Q38: the next upgrade to ``version`` fails its health check."""
    box.ok(
        f"install -d -m 0755 {opt}/.upgrade && "
        f"echo {version} > {opt}/.upgrade/fail-upgrade-to"
    )


def connected(box: SystemdBox) -> None:
    box.ok(
        f"for i in $(seq 90); do {CTL_COMPUTE} nodes | grep -q ' yes ' && exit 0; "
        "sleep 1; done; exit 1",
        timeout=200,
    )


def fabric_with_a_job(box: SystemdBox, controller_debs: Debs, debs: Debs) -> str:
    """Controller (``controller_debs.first``) set up through its setup page,
    the agent package (``debs.first``) joined, one finished job. Returns the
    job id."""
    done = box.install(controller_debs.first)
    assert done.returncode == 0, done.stdout[-4000:]
    printed = box.ok("meshforge-controller setup url")
    url = next(
        line.split()[-1] for line in printed.splitlines() if "/setup#token=" in line
    )
    box.ok("cat > /tmp/setup_client.py", stdin=SETUP_CLIENT)
    out = box.ok(f"{CTL_PYTHON} /tmp/setup_client.py '{url}' admin '{PASSWORD}'")
    result = json.loads(out)["result"]
    invite = result["invite"]
    box.ok(
        "for i in $(seq 60); do test -e /var/lib/meshforge/controller/tls && "
        "journalctl -u meshforge-controller -o cat | grep -q controller.listening "
        "&& exit 0; sleep 1; done; exit 1"
    )
    address = str(invite["address"]).rsplit(":", 1)[0]
    box.ok(
        f"{CTL_COMPUTE} login --controller https://{address}:47110 "
        f"--fingerprint {result['ca_fingerprint']} -u admin --password-stdin",
        stdin=PASSWORD + "\n",
    )
    done = box.install(debs.first)
    assert done.returncode == 0, done.stdout[-4000:]
    box.ok(f"echo {invite['code']} | meshforge-agent join -")
    box.ok("meshforge-agent policy set accept_jobs=true allow_unsandboxed=true")
    box.ok(f"systemctl restart {UNIT}")
    connected(box)
    box.ok("mkdir -p /root/proj && cat > /root/proj/compute.yaml", stdin=MANIFEST)
    ran = box.ok(f"cd /root && {CTL_COMPUTE} run /root/proj -f", timeout=600)
    assert "hello-from-g5" in ran
    return "job_" + ran.split("job_", 1)[1].split()[0]
