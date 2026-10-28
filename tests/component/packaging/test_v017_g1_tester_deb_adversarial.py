"""V0.17 G1 tester: adversarial acceptance of the real meshforge-agent .deb.

Derived from brief §5a (success criteria, standing checklist "no confidently-wrong
output"), not from the implementation. The implementer's lifecycle test covers the
happy path; these try to break it: idempotent re-runs, hostile source trees
(symlinks, FIFOs, corrupt files), disk full mid-migration, concurrent migrations,
bad ``--from`` values, hostile environments, non-admin users, unusual dpkg
sequences (purge without remove, downgrade, no systemd), hardening/integrity.

Every test starts its own clean Ubuntu systemd container (removed afterwards) and
uses the packages in ``$MESHFORGE_TEST_DEB_DIR`` or ``<repo>/dist/debs`` (else it
builds them: minutes). Slow: ``--run-slow``.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

pytest.importorskip("cryptography", reason="the test makes real enrolled identities")

from mf_deb import (
    REPO,
    UNIT,
    Debs,
    SystemdBox,
    built_debs,
    systemd_image,
)

pytestmark = [
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.timeout(1500),
    pytest.mark.skipif(sys.platform != "linux", reason="Linux package"),
]

SYS_ID: Final = "/var/lib/meshforge/identity"
ALICE_ID: Final = "/home/alice/.local/share/meshforge/identity"
CONFIG: Final = (
    '[agent]\ncontroller_address = "127.0.0.1:47199"\n\n'
    "[agent.discovery]\nenabled = false\n\n[agent.scores]\nenabled = false\n"
)


@pytest.fixture(scope="module")
def debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    if not os.environ.get("MESHFORGE_TEST_DEB_DIR"):
        local = REPO / "dist" / "debs"
        if list(local.glob("*-1_amd64.deb")) and list(local.glob("*-2_amd64.deb")):
            os.environ["MESHFORGE_TEST_DEB_DIR"] = str(local)
    return built_debs(tmp_path_factory.mktemp("debs"))


@pytest.fixture
def box() -> Iterator[SystemdBox]:
    made = SystemdBox.start(systemd_image("24.04"), name_hint="debadv")
    try:
        yield made
    finally:
        made.remove()


def _make_identity(folder: Path) -> str:
    from mf_fabric import enroll_identity

    from meshforge.agent.identity import load_or_create

    identity = load_or_create(folder)
    enroll_identity(identity)
    return identity.node_id


@pytest.fixture
def ident(tmp_path: Path) -> tuple[Path, str]:
    folder = tmp_path / "identity"
    return folder, _make_identity(folder)


def _seed(
    box: SystemdBox,
    folder: Path,
    user: str = "alice",
    config: str | None = CONFIG,
    create_user: bool = True,
) -> str:
    home = f"/home/{user}"
    target = f"{home}/.local/share/meshforge/identity"
    if create_user:
        box.ok(f"useradd --create-home --shell /bin/sh {user}")
    box.ok(f"install -d -o {user} -g {user} -m 0700 {home}/.local/share/meshforge")
    box.copy_in(folder, target)
    box.ok(f"chown -R {user}:{user} {target} && chmod 0700 {target}")
    if config is not None:
        box.ok(f"install -d -o {user} -g {user} -m 0700 {home}/.config/meshforge")
        box.ok(
            f"cat > {home}/.config/meshforge/config.toml && "
            f"chown {user}:{user} {home}/.config/meshforge/config.toml",
            stdin=config,
        )
    return target


def _tree(box: SystemdBox, *roots: str) -> str:
    """A content+mode+owner fingerprint of the given trees (empty if absent)."""
    quoted = " ".join(f"'{r}'" for r in roots)
    return box.run(
        f'for r in {quoted}; do [ -e "$r" ] || [ -L "$r" ] || continue; '
        "find \"$r\" -printf '%p %y %U:%G %m %s %l\\n' | sort; "
        'find "$r" -type f -exec sha256sum {} + | sort; done'
    ).stdout


def _stable(snapshot: str) -> str:
    """Without node.json: a running agent legitimately rewrites its own session
    counters there."""
    return "\n".join(ln for ln in snapshot.splitlines() if "node.json" not in ln)


def _running(box: SystemdBox) -> dict[str, str]:
    facts = box.wait_unit(UNIT, ["active"], within=60)
    assert int(facts["MainPID"]) > 0
    return facts


def _node_id(box: SystemdBox) -> str:
    return str(box.identity()["node_id"])


# ---------------------------------------------------------------- idempotency


def test_reinstall_and_rerunning_maintainer_scripts_change_nothing(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, node_id = ident
    _seed(box, folder)
    done = box.install(debs.first)
    assert done.returncode == 0, done.stdout[-3000:]
    _running(box)
    before = _stable(_tree(box, SYS_ID, "/etc/meshforge"))
    # same package again, the postinst/preinst by hand (twice), dpkg --configure -a
    again = box.apt(f"install -y --reinstall /tmp/{debs.first.name}")
    assert again.returncode == 0, again.stdout[-3000:]
    for _ in range(2):
        box.ok("/var/lib/dpkg/info/meshforge-agent.preinst upgrade 0.0.1")
        box.ok(
            f"/var/lib/dpkg/info/meshforge-agent.postinst configure {debs.bundle_version}-1"
        )
    box.ok("dpkg --configure -a")
    _running(box)
    assert _node_id(box) == node_id
    assert _stable(_tree(box, SYS_ID, "/etc/meshforge")) == before
    assert box.ok("getent passwd meshforge | wc -l").strip() == "1"
    assert box.ok("getent group meshforge meshforge-admin | wc -l").strip() == "2"
    moved = box.ok("ls -d /home/alice/.local/share/meshforge/identity.migrated-*")
    assert len(moved.split()) == 1  # not renamed again, not duplicated
    assert "identity" in box.ok("ls /var/lib/meshforge").split()


def test_existing_account_and_tightened_modes_survive_install_and_upgrade(
    debs: Debs, box: SystemdBox
) -> None:
    # an admin who already made the account / folder (wrong owner, loose mode)
    box.ok("groupadd --system meshforge && useradd --system -g meshforge -M meshforge")
    box.ok("mkdir -p /var/lib/meshforge && chmod 0777 /var/lib/meshforge")
    done = box.install(debs.first)
    assert done.returncode == 0, done.stdout[-3000:]
    assert box.ok("stat -c '%U:%G' /var/lib/meshforge").strip() == "meshforge:meshforge"
    # now the admin tightens, and an upgrade must never loosen
    box.ok("chmod 0700 /var/lib/meshforge")
    assert box.install(debs.second).returncode == 0
    assert box.ok("stat -c '%a' /var/lib/meshforge").strip() == "700"


def test_admin_tightened_etc_meshforge_mode_is_not_loosened_by_upgrade(
    debs: Debs, box: SystemdBox
) -> None:
    """postinst's own contract ("never loosened if an admin tightened them
    further") - /etc/meshforge is reset to 0755 by `install -d -m 0755` on every
    upgrade."""
    assert box.install(debs.first).returncode == 0
    box.ok("chown root:meshforge /etc/meshforge && chmod 0750 /etc/meshforge")
    assert box.install(debs.second).returncode == 0
    assert box.ok("stat -c '%a' /etc/meshforge").strip() == "750"


def test_docker_group_is_never_granted_gpu_groups_are(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, node_id = ident
    box.ok("groupadd docker; groupadd render; useradd -m -G docker bob")
    _seed(box, folder)
    done = box.install(debs.first)
    assert done.returncode == 0, done.stdout[-3000:]
    groups = box.ok("id -nG meshforge").split()
    assert "docker" not in groups, groups  # Q12: root-equivalent
    assert "render" in groups and "video" in groups, groups
    assert "docker: no access" in done.stdout
    assert "usermod -aG docker meshforge" in done.stdout  # the one command
    _running(box)
    assert _node_id(box) == node_id


# ------------------------------------------------- migration refusals (real fs)


@pytest.mark.parametrize(
    "damage",
    [
        "echo garbage > {d}/node.key",
        ": > {d}/node.key",
        "rm {d}/node.key",
        "rm {d}/node.json",
        'sed -i \'s/"node_id": "./"node_id": "Z/\' {d}/node.json',
        "echo junk > {d}/node.crt",
        "rm {d}/ca.crt",
    ],
    ids=[
        "garbage-key",
        "empty-key",
        "missing-key",
        "missing-json",
        "node-id-edited",
        "garbage-cert",
        "missing-ca",
    ],
)
def test_damaged_per_user_identity_is_refused_and_untouched(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str], damage: str
) -> None:
    folder, _ = ident
    src = _seed(box, folder)
    box.ok(damage.format(d=src))
    snap = _tree(box, "/home/alice")
    done = box.install(debs.first)
    # Q13: dpkg stays configured; the service is not started; nothing minted
    assert done.returncode == 0, done.stdout[-3000:]
    assert "is running" not in done.stdout
    assert src in done.stdout or "alice" in done.stdout, done.stdout[-3000:]
    assert _tree(box, "/home/alice") == snap, (
        "the source must be byte-for-byte untouched"
    )
    assert box.run(f"test -e {SYS_ID}/node.key").returncode != 0
    assert box.show(UNIT, "ActiveState")["ActiveState"] != "active"
    assert box.run(f"ls -d {src}.migrated-* 2>/dev/null").stdout == ""
    # and the node did not silently mint a new identity behind the refusal
    # (identity.json absent in a *fresh* target is fine; a key is not)
    assert "identity.created" not in " ".join(
        str(e.get("event")) for e in box.journal_events()
    )


def test_symlinked_secret_in_source_is_not_followed(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, _ = ident
    src = _seed(box, folder)
    box.ok(
        "echo SECRET-ROOT-ONLY-MARKER > /root/secret.txt && chmod 600 /root/secret.txt"
    )
    box.ok(f"rm {src}/ca.crt && ln -s /root/secret.txt {src}/ca.crt")
    done = box.install(debs.first)
    assert done.returncode == 0, done.stdout[-3000:]
    leaked = box.run(
        "grep -rl SECRET-ROOT-ONLY-MARKER /var/lib/meshforge /etc/meshforge"
    )
    assert leaked.stdout.strip() == "", (
        "root-only file content copied into the service's state: " + leaked.stdout
    )


def test_symlinked_identity_dir_is_not_followed(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, _ = ident
    box.ok(
        "useradd -m alice && mkdir -p /root/other && echo MARK > /root/other/node.key"
    )
    box.ok("install -d -o alice -g alice /home/alice/.local/share/meshforge")
    box.copy_in(folder, "/root/other2")
    box.ok(f"ln -s /root/other2 {ALICE_ID} && chown -h alice:alice {ALICE_ID}")
    done = box.install(debs.first)
    assert done.returncode == 0, done.stdout[-3000:]
    # either refused or migrated, but never a symlink planted in the target
    assert box.run(f"test -L {SYS_ID}").returncode != 0
    assert box.run("test -L /var/lib/meshforge/identity/node.key").returncode != 0


def test_fifo_in_source_does_not_hang_the_install(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, _ = ident
    src = _seed(box, folder)
    box.ok(f"mkfifo {src}/stray.fifo && chown alice:alice {src}/stray.fifo")
    started = time.monotonic()
    done = box.install(debs.first)  # box.run has a 600 s timeout: a hang fails here
    assert time.monotonic() - started < 240
    assert done.returncode == 0, done.stdout[-3000:]


def test_unknown_config_key_blocks_the_whole_move_all_or_nothing(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, _ = ident
    src = _seed(box, folder, config=CONFIG + "\n[agent]\nno_such_key = 1\n")
    # (duplicate [agent] table is invalid TOML too: both bad-config shapes)
    box.ok(
        "printf '[agent]\\nno_such_key = 1\\n' > /home/alice/.config/meshforge/config.toml"
    )
    snap = _tree(box, "/home/alice")
    done = box.install(debs.first)
    assert done.returncode == 0, done.stdout[-3000:]
    assert "no_such_key" in done.stdout or "config.toml" in done.stdout, done.stdout[
        -3000:
    ]
    assert "is running" not in done.stdout
    assert _tree(box, "/home/alice") == snap
    assert box.run(f"test -e {SYS_ID}/node.key").returncode != 0
    assert box.run("test -e /etc/meshforge/config.toml").returncode != 0, (
        "config must not be half-moved with the identity refused"
    )
    del src


@pytest.mark.parametrize(
    "payload",
    [
        "printf '\\377\\376binary\\000' > {c}",
        "head -c 3000000 /dev/urandom > {c}",
        ": > {c}",
    ],
    ids=["binary", "3MB-random", "empty"],
)
def test_hostile_config_files_do_not_crash_or_hang_the_install(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str], payload: str
) -> None:
    folder, _ = ident
    _seed(box, folder)
    box.ok(payload.format(c="/home/alice/.config/meshforge/config.toml"))
    done = box.install(debs.first)
    assert done.returncode == 0, done.stdout[-3000:]
    assert "Traceback" not in done.stdout, done.stdout[-3000:]


def test_sudo_user_picks_its_own_identity_among_several(
    debs: Debs, box: SystemdBox, tmp_path: Path
) -> None:
    ids = {}
    for user in ("alice", "bob"):
        f = tmp_path / user
        ids[user] = _make_identity(f)
        _seed(box, f, user)
    box.copy_in(debs.first, f"/tmp/{debs.first.name}")
    done = box.run(
        "SUDO_USER=bob DEBIAN_FRONTEND=noninteractive apt-get install -y "
        f"/tmp/{debs.first.name} 2>&1",
        timeout=600,
    )
    assert done.returncode == 0, done.stdout[-3000:]
    _running(box)
    assert _node_id(box) == ids["bob"]
    box.ok("test -f /home/alice/.local/share/meshforge/identity/node.key")


def test_target_already_present_wins_over_a_new_user_identity(
    debs: Debs, box: SystemdBox, tmp_path: Path
) -> None:
    a = _make_identity(tmp_path / "a")
    _seed(box, tmp_path / "a", "alice")
    assert box.install(debs.first).returncode == 0
    _running(box)
    b = _make_identity(tmp_path / "b")
    bob_src = _seed(box, tmp_path / "b", "bob")
    snap = _tree(box, "/home/bob")
    done = box.install(debs.second)
    assert done.returncode == 0, done.stdout[-3000:]
    assert _node_id(box) == a != b
    assert _tree(box, "/home/bob") == snap  # bob's source untouched
    del bob_src


def test_dry_run_and_bad_from_values_write_nothing(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, node_id = ident
    assert box.install(debs.first).returncode == 0  # fresh, not joined
    src = _seed(box, folder)
    watched = ("/home/alice", "/var/lib/meshforge", "/etc/meshforge")
    snap = _tree(box, *watched)
    home_snap = _tree(box, "/home/alice")
    # the install ran before alice existed; a dry run now must only print
    dry = box.run("meshforge-agent migrate-identity --dry-run")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert _tree(box, *watched) == snap
    dry_json = box.run("meshforge-agent migrate-identity --dry-run --json")
    assert dry_json.returncode == 0
    json.loads(dry_json.stdout)
    assert "PRIVATE KEY" not in dry.stdout + dry_json.stdout
    assert _tree(box, *watched) == snap
    box.ok("mkdir -p '/srv/ünï code/id dir' && touch /srv/f")
    for bad in (
        "/does/not/exist",
        "relative/path",
        "/srv/f",
        "/",
        "/etc",
        "/srv/ünï code/id dir",  # exists but empty
        "",
        "/home/alice/.local/share/meshforge/identity/node.key",
    ):
        r = box.run(f"meshforge-agent migrate-identity --from {shlex.quote(bad)}")
        text = r.stdout + r.stderr
        assert "Traceback" not in text, (bad, text)
        assert text.strip(), bad
        # a refusal exits non-zero; a "nothing migrated" no-op names why
        assert r.returncode != 0 or "nothing migrated" in text, (bad, text)
        # the source is untouched and no identity is minted or half-copied
        assert _tree(box, "/home/alice") == home_snap, bad
        assert box.run(f"test -e {SYS_ID}/node.key").returncode != 0, bad
    # the real run still converges afterwards (the documented fix)
    real = box.run(f"meshforge-agent migrate-identity --from {src}")
    assert real.returncode == 0, real.stdout + real.stderr
    assert (
        box.ok(f"cat {SYS_ID}/node.json | grep -o '{node_id}' | head -1").strip()
        == node_id
    )


def test_unicode_and_spaced_source_path(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, node_id = ident
    assert box.install(debs.first).returncode == 0
    box.ok("mkdir -p '/srv/ünï code'")
    box.copy_in(folder, "/srv/ünï code/id dir")
    r = box.run("meshforge-agent migrate-identity --from '/srv/ünï code/id dir'")
    assert r.returncode == 0, r.stdout + r.stderr
    box.ok(f"systemctl start {UNIT}")
    _running(box)
    assert _node_id(box) == node_id


def test_disk_full_mid_migration_leaves_source_intact_and_a_rerun_converges(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, node_id = ident
    assert box.install(debs.first).returncode == 0
    src = _seed(box, folder)
    snap = _tree(box, "/home/alice")
    uid = box.ok("id -u meshforge").strip()
    box.ok(
        f"mount -t tmpfs -o size=8k,mode=0750,uid={uid},gid={uid} tmpfs /var/lib/meshforge"
    )
    r = box.run("meshforge-agent migrate-identity")
    assert r.returncode != 0, "a full disk must not look like success"
    assert "Traceback" not in r.stdout + r.stderr, r.stderr[-1500:]
    assert _tree(box, "/home/alice") == snap, "source must stay intact"
    text = r.stdout + r.stderr
    assert "space" in text.lower() or "/var/lib/meshforge" in text, text
    # the service must not run on a half identity
    box.ok("umount /var/lib/meshforge")
    box.ok(
        f"mount -t tmpfs -o size=1m,mode=0750,uid={uid},gid={uid} tmpfs /var/lib/meshforge"
    )
    again = box.run(f"meshforge-agent migrate-identity --from {src}")
    assert again.returncode == 0, again.stdout + again.stderr
    box.ok(f"systemctl start {UNIT}")
    _running(box)
    assert _node_id(box) == node_id
    box.ok("umount /var/lib/meshforge")


def test_concurrent_migrations_do_not_corrupt(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, node_id = ident
    assert box.install(debs.first).returncode == 0
    _seed(box, folder)
    out = box.ok(
        "for i in 1 2 3 4; do (meshforge-agent migrate-identity >/tmp/m$i.out 2>&1; "
        "echo $? >/tmp/m$i.rc) & done; wait; "
        "cat /tmp/m*.rc"
    )
    codes = sorted(out.split())
    # CI fix round 4: runs are serialised by migrate-identity.lock - one
    # migrates, the others then find the target present; every run succeeds
    # (before, a run that planned against a stale view could block the target)
    assert codes == ["0"] * 4, (codes, box.ok("cat /tmp/m*.out"))
    outputs = box.ok("cat /tmp/m*.out")
    assert outputs.count("migrated node") == 1, outputs
    assert outputs.count("already has an identity") == 3, outputs
    box.ok("rm -f /tmp/m*.rc")
    # whatever the interleaving: one consistent, startable identity
    assert box.run(f"test -e {SYS_ID}/migration.pending").returncode != 0
    box.ok(f"systemctl start {UNIT}")
    _running(box)
    assert _node_id(box) == node_id
    assert (
        len(
            box.ok(
                "ls -d /home/alice/.local/share/meshforge/identity.migrated-*"
            ).split()
        )
        == 1
    )


def test_no_secret_key_material_in_install_output_or_journal(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, _ = ident
    _seed(box, folder)
    done = box.install(debs.first)
    _running(box)
    key_lines = [
        ln
        for ln in (folder / "node.key").read_text().splitlines()
        if len(ln) > 20 and "-----" not in ln
    ]
    blob = done.stdout + box.journal(lines=500)
    assert "PRIVATE KEY" not in blob
    for ln in key_lines:
        assert ln not in blob
    modes = box.ok(f"find {SYS_ID} -type f -printf '%f %U %m\\n'").splitlines()
    assert modes, "migration produced no files"
    for m in modes:
        name, owner, mode = m.split()
        assert owner == box.ok("id -u meshforge").strip(), m
        assert int(mode, 8) & 0o077 == 0, f"{name} is group/world accessible: {mode}"


# ------------------------------------------------------------- service lifecycle


def test_stopped_by_admin_stays_stopped_and_disabled_survives_upgrade(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, node_id = ident
    _seed(box, folder)
    assert box.install(debs.first).returncode == 0
    _running(box)
    box.ok(f"systemctl stop {UNIT}")
    time.sleep(8)
    assert (
        box.show(UNIT, "ActiveState")["ActiveState"] == "inactive"
    )  # nothing starts it again behind the admin's back
    box.ok(f"systemctl disable --now {UNIT}")
    done = box.install(debs.second)
    assert done.returncode == 0, done.stdout[-3000:]
    assert "is running" not in done.stdout  # never claim success
    assert box.show(UNIT, "ActiveState")["ActiveState"] == "inactive"
    assert _node_id(box) == node_id


def test_upgrade_with_a_broken_config_does_not_claim_the_service_is_running(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, node_id = ident
    _seed(box, folder)
    assert box.install(debs.first).returncode == 0
    _running(box)
    box.ok("printf '[agent]\\nbogus_key = 1\\n' >> /etc/meshforge/config.toml")
    done = box.install(debs.second)
    assert done.returncode == 0, done.stdout[-3000:]
    state = box.run(f"systemctl is-active {UNIT}").stdout.strip()
    assert state != "active"
    assert "is running" not in done.stdout, done.stdout[-2500:]
    assert "NOT running" in done.stdout, done.stdout[-2500:]
    # data is kept for the fix
    assert box.run(f"test -f {SYS_ID}/node.key").returncode == 0
    assert node_id


def test_stop_reaps_every_process_of_the_service_cgroup(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, _ = ident
    _seed(box, folder)
    assert box.install(debs.first).returncode == 0
    box.ok("mkdir -p /etc/systemd/system/meshforge-agent.service.d")
    # a main process that spawns a child and a setsid'd grandchild, like a job tree
    box.ok(
        "printf '[Service]\\nExecStart=\\nExecStart=/bin/sh -c "
        '"sleep 4242 & setsid sleep 4243 & wait"\\n\' '
        "> /etc/systemd/system/meshforge-agent.service.d/jobs.conf && systemctl daemon-reload"
    )
    box.ok(f"systemctl restart {UNIT}")
    _running(box)
    assert "4243" in box.ok("ps -eo args")
    t0 = time.monotonic()
    box.ok(f"systemctl stop {UNIT}")
    assert time.monotonic() - t0 < 40
    leftover = box.run("pgrep -fa 'sleep 424[23]'").stdout
    assert leftover.strip() == "", f"job tree outlived the service: {leftover}"


# -------------------------------------------------------- integrity / hardening


def test_running_service_does_not_modify_the_installed_program(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, _ = ident
    _seed(box, folder)
    assert box.install(debs.first).returncode == 0
    _running(box)
    time.sleep(3)
    ver = debs.bundle_version
    checked = box.run(
        f"cd /opt/meshforge/{ver} && sha256sum --quiet -c MANIFEST.sha256"
    )
    assert checked.returncode == 0, checked.stdout[-1500:] + checked.stderr[-500:]
    listed = box.ok(f"cd /opt/meshforge/{ver} && find . -type f | wc -l").strip()
    in_manifest = box.ok(
        f"cd /opt/meshforge/{ver} && grep -vc '^#' MANIFEST.sha256"
    ).strip()
    # MANIFEST.sha256 lists every file except itself
    assert int(listed) == int(in_manifest) + 1, (listed, in_manifest)
    assert box.run("dpkg -V meshforge-agent").stdout.strip() == ""
    assert box.ok(f"stat -c '%U' /opt/meshforge/{ver}").strip() == "root"
    assert (
        box.run(
            "find /opt/meshforge -writable -not -type l | head -1", user="meshforge"
        ).stdout.strip()
        == ""
    )


def test_hostile_python_environment_cannot_hijack_the_launchers(
    debs: Debs, box: SystemdBox
) -> None:
    assert box.install(debs.first).returncode == 0
    box.ok(
        "mkdir -p /tmp/evil/meshforge && cd /tmp/evil && "
        "for f in sitecustomize usercustomize json encodings; do "
        '  echo \'open("/tmp/PWNED","a").write("x")\' > $f.py; done && '
        'echo \'open("/tmp/PWNED","a").write("pkg")\' > meshforge/__init__.py && '
        "cp sitecustomize.py startup.py"
    )
    env = (
        "PYTHONPATH=/tmp/evil PYTHONSTARTUP=/tmp/evil/startup.py "
        "PYTHONUSERBASE=/tmp/evil PYTHONHOME=/nonexistent PYTHONINSPECT=1 "
        "PYTHONSAFEPATH= PYTHONWARNINGS=error"
    )
    for cmd in ("meshforge-agent --version", "compute --version"):
        r = box.run(f"cd /tmp/evil && env {env} {cmd}")
        assert r.returncode == 0, (cmd, r.stdout, r.stderr)
    assert box.run("test -e /tmp/PWNED").returncode != 0, "attacker code ran"
    r = box.run("env -i /usr/bin/meshforge-agent --version")
    assert r.returncode == 0, r.stderr


def test_non_admin_user_cannot_create_a_shadow_identity_in_home(
    debs: Debs, box: SystemdBox
) -> None:
    assert box.install(debs.first).returncode == 0
    box.ok("useradd -m eve; useradd -m alice")
    for argv in ("identity show", "policy show", "join NOT-A-REAL-CODE"):
        r = box.run(f"meshforge-agent {argv}", user="eve")
        assert r.returncode != 0, (argv, r.stdout)
        assert "Traceback" not in r.stdout + r.stderr, (argv, r.stderr)
        assert r.stderr.strip() or r.stdout.strip(), argv
    assert box.run("test -e /home/eve/.local/share/meshforge").returncode != 0
    assert box.run("test -e /home/eve/.config/meshforge").returncode != 0
    # an allowed sudoer is re-executed as the service user; arguments stay data
    box.ok("echo 'alice ALL=(meshforge) NOPASSWD: ALL' > /etc/sudoers.d/alice")
    r = box.run("meshforge-agent identity show --json", user="alice")
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout)["path"] == SYS_ID
    assert box.run("test -e /home/alice/.local/share/meshforge").returncode != 0
    r = box.run(
        "meshforge-agent join 'x; touch /tmp/pwned #ünï \"q\" $(touch /tmp/pwned2)'",
        user="alice",
    )
    assert r.returncode != 0
    assert box.run("ls /tmp/pwned /tmp/pwned2").returncode != 0, (
        "argument was shell-interpreted"
    )
    # explicit opt-out still works for a developer (user mode in $HOME)
    r = box.run(
        "MESHFORGE_INSTALL_MODE=user HOME=/home/eve meshforge-agent identity show --json",
        user="eve",
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "/home/eve" in json.loads(r.stdout)["path"]


# ---------------------------------------------------------- dpkg sequences


def test_purge_without_remove_then_clean_fresh_reinstall(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, node_id = ident
    _seed(box, folder)
    assert box.install(debs.first).returncode == 0
    _running(box)
    assert box.apt("purge -y meshforge-agent").returncode == 0
    check = box.verify_uninstall(data_removed=True)
    assert check.returncode == 0, check.stdout + check.stderr
    assert box.run("getent passwd meshforge").returncode != 0
    assert (
        box.run(
            "find / -xdev \\( -nouser -o -nogroup \\) -path '/var/*' | head -3"
        ).stdout.strip()
        == ""
    )
    # purging twice / removing what is gone is harmless
    assert box.run("dpkg --purge meshforge-agent").returncode == 0
    # a re-install after purge is a *fresh* install: not joined, nothing resurrected
    done = box.install(debs.first)
    assert done.returncode == 0, done.stdout[-3000:]
    assert "has not joined" in done.stdout
    assert box.run("meshforge-agent identity show --json").stdout.count(node_id) == 0
    # V0.17 G3: running, waiting for the join with only its console up
    assert box.show(UNIT, "ActiveState")["ActiveState"] == "active"


def test_remove_then_dpkg_purge_and_remove_twice(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, _ = ident
    _seed(box, folder)
    assert box.install(debs.first).returncode == 0
    _running(box)
    assert box.apt("remove -y meshforge-agent").returncode == 0
    assert box.apt("remove -y meshforge-agent").returncode == 0
    check = box.verify_uninstall(data_removed=False)
    assert check.returncode == 0, check.stdout + check.stderr
    assert box.run("dpkg --purge meshforge-agent").returncode == 0
    check = box.verify_uninstall(data_removed=True)
    assert check.returncode == 0, check.stdout + check.stderr


def test_downgrade_keeps_the_node_and_the_running_service(
    debs: Debs, box: SystemdBox, ident: tuple[Path, str]
) -> None:
    folder, node_id = ident
    _seed(box, folder)
    assert box.install(debs.second).returncode == 0
    _running(box)
    box.copy_in(debs.first, f"/tmp/{debs.first.name}")
    done = box.apt(f"install -y --allow-downgrades /tmp/{debs.first.name}")
    assert done.returncode == 0, done.stdout[-3000:]
    _running(box)
    assert _node_id(box) == node_id
    assert box.ok("dpkg-query -W -f='${Version}' meshforge-agent").endswith("-1")


def test_install_without_systemd_runs_to_completion(
    debs: Debs, ident: tuple[Path, str]
) -> None:
    """A chroot / plain container: no systemd. The identity is still migrated,
    the message says the service was not started, remove and purge work."""
    folder, node_id = ident
    image = systemd_image("24.04")
    name = f"mf-test-nosd-{os.getpid()}"
    from mf_test_containers import meshforge_test_labels

    argv = ["docker", "run", "-d", "--name", name, "--entrypoint", "sleep"]
    for k, v in meshforge_test_labels().items():
        argv += ["--label", f"{k}={v}"]
    subprocess.run(
        [*argv, image, "infinity"], check=True, capture_output=True, timeout=120
    )
    sb = SystemdBox(name)
    try:
        assert sb.run("test -d /run/systemd/system").returncode != 0
        _seed(sb, folder)
        done = sb.install(debs.first)
        assert done.returncode == 0, done.stdout[-3000:]
        assert "systemd is not running" in done.stdout, done.stdout[-2000:]
        assert (
            sb.ok("meshforge-agent identity show --json | grep -c " + node_id).strip()
            == "1"
        )
        assert sb.apt("remove -y meshforge-agent").returncode == 0
        assert sb.apt("purge -y meshforge-agent").returncode == 0
        assert sb.run("test -e /var/lib/meshforge").returncode != 0
    finally:
        sb.remove()
