"""V0.17 G1: the real meshforge-agent .deb in clean Ubuntu 22.04 / 24.04 systemd containers.

One scenario per Ubuntu release, in the order a laptop lives through it (brief
§5a "Tests", Q16). Before the install, the laptop already has a per-user agent
(user ``alice``: an enrolled identity + ``config.toml``, as a V0.16 laptop that
ran ``meshforge-agent`` from a checkout), and no Python at all.

1. ``apt install ./meshforge-agent_<v>-1_amd64.deb``: the identity and config are
   migrated (same ``node_id``; source renamed ``identity.migrated-<date>``), the
   service is active as ``meshforge`` with no system Python and no uv on PATH,
   ``--version`` / ``hardware show`` work (a human's command re-executes as the
   service user, Q15), the hardened unit holds (``ProtectSystem=strict``: the
   service cannot write ``/opt`` or ``/etc``), the CPU cgroup is delegated
   (``cpu_cgroup_ready`` on systemd >= 254, i.e. 24.04; 22.04 says why not);
2. ``identity show`` is stable across ``systemctl restart``; ``kill -9`` of the
   agent -> systemd restarts it (same ``node_id``);
3. exit 5 (CONFLICT) and exit 4 (CONFIG_ERROR) are not restarted;
4. upgrade to revision 2 keeps ``node_id``, config, and the running service;
5. ``apt remove`` keeps identity/config and leaves no unit, process or program
   file (``lab/verify_uninstall.py``); re-install -> same ``node_id``;
6. ``apt purge`` leaves nothing (``verify_uninstall.py --data-removed``);
7. a **fresh** install (no per-user agent) is "not joined yet": the service is
   skipped (inactive, not failed), the path unit waits, and a node
   certificate appearing starts the service (here a broken one -> exit 4, not
   restarted, no loop).

Slow (bundle + .deb build, two containers); CI runs it in the package-linux job
with the packages it built (``MESHFORGE_TEST_DEB_DIR``).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

pytest.importorskip("cryptography", reason="the test makes a real enrolled identity")

from mf_deb import (
    PATH_UNIT,
    UBUNTU_VERSIONS,
    UNIT,
    Debs,
    SystemdBox,
    built_debs,
    systemd_image,
)

pytestmark = [
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.timeout(2400),
    pytest.mark.skipif(sys.platform != "linux", reason="Linux package"),
]

ALICE_CONFIG: Final = """\
# alice's per-user agent config (V0.16 style)
[agent]
controller_address = "127.0.0.1:47199"

[agent.discovery]
enabled = false

[agent.scores]
enabled = false

[agent.execution]
enabled = true

[agent.policy]
allow_unsandboxed = true
"""
SYSTEM_IDENTITY: Final = "/var/lib/meshforge/identity"
ALICE_IDENTITY: Final = "/home/alice/.local/share/meshforge/identity"


@pytest.fixture(scope="module")
def debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    return built_debs(tmp_path_factory.mktemp("debs"))


@pytest.fixture
def alice_identity(tmp_path: Path) -> tuple[Path, str]:
    """An enrolled per-user identity folder (made by the real code) + its node_id."""
    from mf_fabric import enroll_identity

    from meshforge.agent.identity import load_or_create

    folder = tmp_path / "identity"
    identity = load_or_create(folder)
    enroll_identity(identity)
    return folder, identity.node_id


@pytest.fixture(params=UBUNTU_VERSIONS)
def box(request: pytest.FixtureRequest) -> Iterator[tuple[SystemdBox, str]]:
    ubuntu = str(request.param)
    made = SystemdBox.start(systemd_image(ubuntu), name_hint=f"deb{ubuntu}")
    try:
        yield made, ubuntu
    finally:
        made.remove()


def _no_python_on_path(box: SystemdBox) -> None:
    out = box.run("command -v python3 python uv || true").stdout.strip()
    assert out == "", f"the test image must have no python/uv on PATH: {out}"


def _seed_alice(box: SystemdBox, identity: Path, user: str = "alice") -> None:
    """``user`` with an agent identity (and config) in their home, as a V0.16
    laptop that ran the agent from a checkout."""
    home = f"/home/{user}"
    target = f"{home}/.local/share/meshforge/identity"
    box.ok(f"useradd --create-home --shell /bin/sh {user}")
    box.ok(f"install -d -o {user} -g {user} -m 0700 {home}/.local/share/meshforge")
    box.copy_in(identity, target)
    box.ok(f"install -d -o {user} -g {user} -m 0700 {home}/.config/meshforge")
    box.ok(
        f"cat > {home}/.config/meshforge/config.toml && "
        f"chown {user}:{user} {home}/.config/meshforge/config.toml && "
        f"chmod 0600 {home}/.config/meshforge/config.toml",
        stdin=ALICE_CONFIG,
    )
    box.ok(f"chown -R {user}:{user} {target} && chmod 0700 {target}")


def _mount_options(mountinfo: str) -> dict[str, set[str]]:
    """mount point -> its per-mount options (the last mount of a point wins)."""
    options: dict[str, set[str]] = {}
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) > 5:
            options[fields[4]] = set(fields[5].split(","))
    return options


def _main_pid(box: SystemdBox) -> int:
    return int(box.show(UNIT, "MainPID")["MainPID"])


def _wait_running(box: SystemdBox) -> dict[str, str]:
    facts = box.wait_unit(UNIT, ["active"], within=60)
    assert int(facts["MainPID"]) > 0, facts
    return facts


def test_deb_lifecycle_on_a_laptop_that_already_joined(
    debs: Debs, box: tuple[SystemdBox, str], alice_identity: tuple[Path, str]
) -> None:
    sb, ubuntu = box
    identity_dir, node_id = alice_identity
    _no_python_on_path(sb)
    _seed_alice(sb, identity_dir)

    # 1. install: migration, active service, no system Python
    done = sb.install(debs.first)
    assert done.returncode == 0, done.stdout[-4000:]
    assert "migrated" in done.stdout, done.stdout[-3000:]
    assert f"service {UNIT} is running" in done.stdout, done.stdout[-3000:]
    _wait_running(sb)
    assert sb.identity()["node_id"] == node_id
    assert sb.identity()["enrolled"] is True
    moved = sb.ok(
        "ls -d /home/alice/.local/share/meshforge/identity.migrated-*"
    ).split()
    assert len(moved) == 1
    assert sb.run(f"test -e {ALICE_IDENTITY}").returncode != 0
    assert "controller_address" in sb.ok("cat /etc/meshforge/config.toml")
    assert sb.ok("stat -c '%U %a' /etc/meshforge/config.toml").split() == [
        "root",
        "640",
    ]
    assert sb.ok(f"stat -c '%U %a' {SYSTEM_IDENTITY}").split() == ["meshforge", "700"]
    assert sb.ok(f"stat -c %U /proc/{_main_pid(sb)}").strip() == "meshforge"
    _no_python_on_path(sb)
    version = sb.ok("meshforge-agent --version")
    assert version.startswith(f"meshforge-agent {debs.bundle_version} (git "), version
    assert "bundle" in version
    shown = sb.run("meshforge-agent hardware show")
    assert shown.returncode == 0, shown.stderr
    assert "cpu:" in shown.stdout
    assert "via sudo" in shown.stderr  # Q15: root's command ran as meshforge
    compute = sb.run("compute --version")
    assert compute.returncode == 0 and debs.bundle_version in compute.stdout

    # hardening: the service's own view of the file system
    probe = (
        "for p in /opt/meshforge/current/x /etc/meshforge/x /home/alice/x "
        "/var/lib/meshforge/x; do "
        'if (echo t > $p) 2>/dev/null; then echo "$p writable"; rm -f $p; '
        'else echo "$p denied"; fi; done'
    )
    sandboxed = sb.ok(
        "systemd-run --wait --pipe --quiet -p User=meshforge "
        "-p ProtectSystem=strict -p ReadWritePaths=/var/lib/meshforge "
        f"-p ProtectHome=yes sh -c '{probe}'"
    )
    # the unit's real settings, read back from systemd
    unit = sb.show(UNIT, "ProtectSystem", "ProtectHome", "NoNewPrivileges",
                   "PrivateTmp", "ReadWritePaths", "User", "Environment",
                   "RestartPreventExitStatus", "KillMode", "Delegate")  # fmt: skip
    assert unit["ProtectSystem"] == "strict" and unit["ProtectHome"] == "yes", unit
    assert unit["NoNewPrivileges"] == "yes" and unit["PrivateTmp"] == "yes", unit
    assert unit["ReadWritePaths"] == "/var/lib/meshforge", unit
    assert unit["User"] == "meshforge" and unit["KillMode"] == "mixed", unit
    assert "MESHFORGE_INSTALL_MODE=system" in unit["Environment"], unit
    assert set(unit["RestartPreventExitStatus"].split()) == {"4", "5"}, unit
    assert unit["Delegate"] == "yes", unit
    assert sandboxed.split("\n")[:4] == [
        "/opt/meshforge/current/x denied",
        "/etc/meshforge/x denied",
        "/home/alice/x denied",
        "/var/lib/meshforge/x writable",
    ], sandboxed
    # the running agent itself: in its mount namespace / is read-only and only
    # /var/lib/meshforge is writable
    options = _mount_options(sb.ok(f"cat /proc/{_main_pid(sb)}/mountinfo"))
    assert "ro" in options["/"], options["/"]
    assert "rw" in options["/var/lib/meshforge"], options.get("/var/lib/meshforge")

    events = sb.journal_events()
    names = [str(e.get("event")) for e in events]
    if ubuntu >= "24.04":  # DelegateSubgroup needs systemd >= 254
        assert "agent.execution.cpu_cgroup_ready" in names, names
    else:
        unavailable = [
            e
            for e in events
            if e.get("event") == "agent.execution.cpu_cgroup_unavailable"
        ]
        assert unavailable and unavailable[0]["reason"] == "not_delegated", names
    assert "identity.created" not in names  # migrated, never minted

    # 2. restart keeps the identity; kill -9 -> restarted by systemd
    sb.ok(f"systemctl restart {UNIT}")
    _wait_running(sb)
    assert sb.identity()["node_id"] == node_id
    before = int(sb.show(UNIT, "NRestarts")["NRestarts"])
    old_pid = _main_pid(sb)
    sb.ok(f"kill -9 {old_pid}")
    facts = sb.wait_unit(UNIT, ["active"], within=30)
    for _ in range(40):
        facts = sb.show(UNIT, "MainPID", "NRestarts", "ActiveState")
        if int(facts["NRestarts"]) > before and facts["ActiveState"] == "active":
            break
        sb.run("sleep 0.5")
    assert int(facts["NRestarts"]) == before + 1, facts
    assert int(facts["MainPID"]) not in (0, old_pid), facts
    assert sb.identity()["node_id"] == node_id

    # 3. exit 5 / exit 4 are not restarted
    sb.ok("mkdir -p /etc/systemd/system/meshforge-agent.service.d")
    sb.ok(
        "printf '[Service]\\nExecStart=\\nExecStart=/bin/sh -c \"exit 5\"\\n' "
        "> /etc/systemd/system/meshforge-agent.service.d/exit5.conf && systemctl daemon-reload"
    )
    sb.run(f"systemctl restart {UNIT}")
    facts = sb.wait_unit(UNIT, ["failed", "inactive"], within=30)
    sb.run("sleep 7")  # longer than RestartSec=5
    facts = sb.show(UNIT, "ActiveState", "ExecMainStatus", "NRestarts")
    assert facts["ActiveState"] == "failed" and facts["ExecMainStatus"] == "5", facts
    sb.ok(
        "rm -rf /etc/systemd/system/meshforge-agent.service.d && systemctl daemon-reload"
    )
    sb.ok("cp /etc/meshforge/config.toml /tmp/config.good")
    sb.ok("printf '[agent]\\nno_such_key = 1\\n' >> /etc/meshforge/config.toml")
    sb.run(f"systemctl reset-failed {UNIT}; systemctl restart {UNIT}")
    facts = sb.wait_unit(UNIT, ["failed"], within=30)
    sb.run("sleep 7")
    facts = sb.show(UNIT, "ActiveState", "ExecMainStatus")
    assert facts == {"ActiveState": "failed", "ExecMainStatus": "4"}, (
        facts,
        sb.journal(),
    )
    sb.ok("cp /tmp/config.good /etc/meshforge/config.toml")
    sb.run(f"systemctl reset-failed {UNIT}; systemctl start {UNIT}")
    _wait_running(sb)

    # 4. upgrade (revision 2): same node, still running
    done = sb.install(debs.second)
    assert done.returncode == 0, done.stdout[-4000:]
    assert f"service {UNIT} is running" in done.stdout, done.stdout[-3000:]
    _wait_running(sb)
    assert sb.identity()["node_id"] == node_id
    assert "controller_address" in sb.ok("cat /etc/meshforge/config.toml")

    # 5. remove keeps data, leaves no service/process/program; re-install
    done = sb.apt("remove -y meshforge-agent")
    assert done.returncode == 0, done.stdout[-3000:]
    check = sb.verify_uninstall(data_removed=False)
    assert check.returncode == 0, check.stdout + check.stderr
    assert "data kept" in check.stdout
    assert sb.run(f"test -f {SYSTEM_IDENTITY}/node.key").returncode == 0
    done = sb.install(debs.second)
    assert done.returncode == 0, done.stdout[-4000:]
    _wait_running(sb)
    assert sb.identity()["node_id"] == node_id

    # 6. purge leaves nothing
    done = sb.apt("purge -y meshforge-agent")
    assert done.returncode == 0, done.stdout[-3000:]
    check = sb.verify_uninstall(data_removed=True)
    assert check.returncode == 0, check.stdout + check.stderr
    assert "CLEAN" in check.stdout


def test_fresh_install_waits_for_the_join(
    debs: Debs, box: tuple[SystemdBox, str]
) -> None:
    sb, _ = box
    done = sb.install(debs.first)
    assert done.returncode == 0, done.stdout[-4000:]
    assert "has not joined a fabric yet" in done.stdout, done.stdout[-3000:]
    assert "is running" not in done.stdout
    assert sb.ok(f"systemctl is-enabled {UNIT} {PATH_UNIT}").split() == ["enabled"] * 2
    assert sb.run(f"systemctl is-active {PATH_UNIT}").stdout.strip() == "active"
    facts = sb.show(UNIT, "ActiveState", "Result", "ConditionResult")
    assert facts["ActiveState"] == "inactive", facts
    assert facts["ConditionResult"] == "no", facts
    # the admin CLI mints the identity in the SERVICE's place (Q15), never in ~
    shown = sb.identity()
    assert shown["enrolled"] is False
    assert str(shown["path"]) == SYSTEM_IDENTITY
    assert sb.run("test -e /root/.local/share/meshforge").returncode != 0

    # a node certificate appears (as `join` writes it) -> the path unit starts
    # the service; this one is garbage -> exit 4, not restarted, no loop
    sb.ok(
        "sudo -u meshforge sh -c 'umask 077; echo junk > "
        f"{SYSTEM_IDENTITY}/node.crt.tmp && mv {SYSTEM_IDENTITY}/node.crt.tmp "
        f"{SYSTEM_IDENTITY}/node.crt'"
    )
    facts = sb.wait_unit(UNIT, ["failed"], within=30)
    assert facts["ExecMainStatus"] == "4", facts
    sb.run("sleep 7")
    facts = sb.show(UNIT, "ActiveState", "NRestarts")
    assert facts == {"ActiveState": "failed", "NRestarts": "0"}, facts
    assert "invalid_enrollment" in sb.journal() or "unusable" in sb.journal()

    done = sb.apt("purge -y meshforge-agent")
    assert done.returncode == 0, done.stdout[-3000:]
    check = sb.verify_uninstall(data_removed=True)
    assert check.returncode == 0, check.stdout + check.stderr
    events = json.dumps(sb.journal_events())
    assert "BEGIN" not in events  # no key material in the journal


def test_two_per_user_agents_block_the_migration_not_the_install(
    debs: Debs, box: tuple[SystemdBox, str], tmp_path: Path
) -> None:
    """Q13: two candidate identities -> `apt install` succeeds, nothing is copied
    or renamed, no identity is minted, the service does not start, and the
    message says what to do; the documented fix then brings the chosen node up."""
    from mf_fabric import enroll_identity

    from meshforge.agent.identity import load_or_create

    sb, _ = box
    ids = {}
    for user in ("alice", "bob"):
        folder = tmp_path / user / "identity"
        identity = load_or_create(folder)
        enroll_identity(identity)
        ids[user] = identity.node_id
        _seed_alice(sb, folder, user)

    done = sb.install(debs.first)
    assert done.returncode == 0, done.stdout[-4000:]
    assert "needs a decision" in done.stdout, done.stdout[-3000:]
    assert "--from" in done.stdout and "/home/alice/" in done.stdout, done.stdout[
        -3000:
    ]
    assert "is running" not in done.stdout
    for user in ("alice", "bob"):
        sb.ok(f"test -f /home/{user}/.local/share/meshforge/identity/node.key")
    assert sb.run(f"test -e {SYSTEM_IDENTITY}/node.key").returncode != 0
    assert sb.run(f"test -e {SYSTEM_IDENTITY}/migration.pending").returncode == 0
    assert sb.show(UNIT, "ActiveState")["ActiveState"] != "active"
    # the service would refuse to mint while the choice is open
    shown = sb.run("meshforge-agent identity show")
    assert shown.returncode != 0, shown.stdout

    sb.ok(
        "meshforge-agent migrate-identity --from "
        "/home/bob/.local/share/meshforge/identity"
    )
    sb.ok(f"systemctl enable --now {UNIT} {PATH_UNIT}")
    _wait_running(sb)
    assert sb.identity()["node_id"] == ids["bob"]
    sb.ok("test -f /home/alice/.local/share/meshforge/identity/node.key")
