"""V0.17 G1 tester round 2: attack the root-reads-user-files hardening (safe_source).

The implementer covered foreign owners only with a patched ``fstat``; here root
(``postinst`` / ``migrate-identity``) reads sources owned by REAL other uids in a
real container, through symlinked parents, group-writable shared groups, sticky
parents, a swap race between plan and execute, a rotation in progress, a lock held
by a running user agent - and the hardened unit must still start on 22.04 and 24.04.

Invariant for every refusal: non-zero exit, no traceback, the refusal names a path,
the source tree is byte-for-byte untouched, no key reaches ``/var/lib/meshforge``.
Slow; ``--run-slow``; uses ``$MESHFORGE_TEST_DEB_DIR`` or ``<repo>/dist/debs``.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

pytest.importorskip("cryptography", reason="the test makes real enrolled identities")

from mf_deb import (
    REPO,
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
    pytest.mark.timeout(1500),
    pytest.mark.skipif(sys.platform != "linux", reason="Linux package"),
]

SYS_ID: Final = "/var/lib/meshforge/identity"
SRC: Final = "/home/alice/.local/share/meshforge/identity"
MARKER: Final = "SECRET-ROOT-ONLY-MARKER"
PY: Final = "/opt/meshforge/current/python/bin/python3"


@pytest.fixture(scope="module")
def debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    if not os.environ.get("MESHFORGE_TEST_DEB_DIR"):
        local = REPO / "dist" / "debs"
        if list(local.glob("*-1_amd64.deb")) and list(local.glob("*-2_amd64.deb")):
            os.environ["MESHFORGE_TEST_DEB_DIR"] = str(local)
    return built_debs(tmp_path_factory.mktemp("debs"))


@pytest.fixture(params=UBUNTU_VERSIONS)
def box_any(request: pytest.FixtureRequest) -> Iterator[SystemdBox]:
    made = SystemdBox.start(systemd_image(str(request.param)), name_hint="r2")
    try:
        yield made
    finally:
        made.remove()


@pytest.fixture
def box() -> Iterator[SystemdBox]:
    made = SystemdBox.start(systemd_image("24.04"), name_hint="r2")
    try:
        yield made
    finally:
        made.remove()


@pytest.fixture
def ident(tmp_path: Path) -> tuple[Path, str]:
    from mf_fabric import enroll_identity

    from meshforge.agent.identity import load_or_create

    folder = tmp_path / "identity"
    identity = load_or_create(folder)
    enroll_identity(identity)
    return folder, identity.node_id


class Lab:
    """A fresh (not joined) install + alice with a pristine enrolled identity and
    config, restorable between sub-cases."""

    def __init__(self, box: SystemdBox, debs: Debs, folder: Path) -> None:
        self.box = box
        assert box.install(debs.first).returncode == 0
        box.ok("useradd -m -s /bin/sh alice; useradd -m -s /bin/sh bob")
        box.ok(
            "install -d -o alice -g alice -m 0700 /home/alice/.local/share/meshforge"
        )
        box.copy_in(folder, SRC)
        box.ok(f"chown -R alice:alice {SRC} && chmod 0700 {SRC}")
        box.ok("install -d -o alice -g alice -m 0700 /home/alice/.config/meshforge")
        box.ok(
            'printf \'[agent]\\ncontroller_address = "127.0.0.1:47199"\\n'
            "\\n[agent.discovery]\\nenabled = false\\n' > "
            "/home/alice/.config/meshforge/config.toml && "
            "chown alice:alice /home/alice/.config/meshforge/config.toml"
        )
        box.ok("chown alice:alice /home/alice/.local /home/alice/.local/share")
        box.ok("rm -rf /root/pristine && cp -a /home/alice /root/pristine")
        box.ok(
            f"mkdir -p /root/evil && cp -a {SRC}/. /root/evil/ && chown -R root:root /root/evil"
        )
        box.ok(f"echo {MARKER} > /root/evil/ca.crt && echo {MARKER} > /root/secret.txt")
        box.ok("chmod 600 /root/secret.txt")

    def reset(self) -> None:
        # (the service runs and starts its fabric part once an enrolled identity
        # appears (V0.17 G3); stop it so it cannot touch the folder being reset)
        self.box.ok("systemctl stop meshforge-agent.service")
        self.box.ok(
            "rm -rf /home/alice /var/lib/meshforge/identity /root/stash && "
            "cp -a /root/pristine /home/alice && mkdir -p /root/stash"
        )

    def tree(self) -> str:
        return self.box.run(
            "find /home/alice -printf '%p %y %U:%G %m %s %l\\n' | sort; "
            "find /home/alice -type f -exec sha256sum {} + | sort"
        ).stdout

    def refused(self, why: str, *fragments: str, args: str = "") -> str:
        snap = self.tree()
        r = self.box.run(f"meshforge-agent migrate-identity {args}")
        text = r.stdout + r.stderr
        assert r.returncode != 0, (why, text)
        assert "Traceback" not in text, (why, text)
        assert any(f in text for f in fragments), (why, fragments, text)
        assert self.tree() == snap, f"{why}: source changed"
        assert self.box.run(f"test -e {SYS_ID}/node.key").returncode != 0, why
        leak = self.box.run(f"grep -rl {MARKER} /var/lib/meshforge /etc/meshforge")
        assert leak.stdout.strip() == "", (why, leak.stdout)
        return text


@pytest.fixture
def lab(box: SystemdBox, debs: Debs, ident: tuple[Path, str]) -> Lab:
    return Lab(box, debs, ident[0])


def _converges(lab: Lab, node_id: str) -> None:
    r = lab.box.run("meshforge-agent migrate-identity")
    assert r.returncode == 0, r.stdout + r.stderr
    lab.box.ok(f"grep -q {node_id} {SYS_ID}/node.json")


def test_control_the_clean_source_still_migrates(
    lab: Lab, ident: tuple[Path, str]
) -> None:
    _converges(lab, ident[1])


def test_foreign_real_uids_are_refused_as_root(lab: Lab) -> None:
    b = lab.box
    cases = {
        "key owned by root": f"chown root:root {SRC}/node.key",
        "key owned by bob": f"chown bob:bob {SRC}/node.key",
        "cert owned by bob": f"chown bob:bob {SRC}/node.crt",
        "ca owned by root": f"chown root:alice {SRC}/ca.crt",
        "identity folder owned by bob": f"chown bob:bob {SRC}",
        "identity folder owned by root": f"chown root:root {SRC}",
        "config owned by bob": "chown bob:bob /home/alice/.config/meshforge/config.toml",
        "home owned by bob": "chown bob:bob /home/alice",
        "key world-writable": f"chmod 0666 {SRC}/node.key",
        "key group-writable by shared group": (
            f"groupadd devs; usermod -aG devs alice; usermod -aG devs bob; "
            f"chgrp devs {SRC}/node.key; chmod 0660 {SRC}/node.key"
        ),
    }
    for why, setup in cases.items():
        lab.reset()
        b.ok(setup)
        lab.refused(why, "alice", "/home/alice", "uid", "writable", "belongs")


def test_symlinked_parents_and_files_are_refused(lab: Lab) -> None:
    b = lab.box
    cases = {
        "~alice is a symlink": (
            "mv /home/alice /root/realhome && ln -s /root/realhome /home/alice"
        ),
        ".local is a symlink": (
            "mv /home/alice/.local /root/loc && ln -s /root/loc /home/alice/.local"
        ),
        ".local/share is a symlink": (
            "mv /home/alice/.local/share /root/shr && ln -s /root/shr /home/alice/.local/share"
        ),
        "meshforge is a symlink": (
            "mv /home/alice/.local/share/meshforge /root/mf && "
            "ln -s /root/mf /home/alice/.local/share/meshforge"
        ),
        "identity is a symlink to root's evil dir": (
            f"mv {SRC} /root/stash/id && ln -s /root/evil {SRC}"
        ),
        "node.key is a symlink": (
            f"rm {SRC}/node.key && ln -s /root/secret.txt {SRC}/node.key"
        ),
        "config.toml is a symlink": (
            "rm /home/alice/.config/meshforge/config.toml && "
            "ln -s /root/secret.txt /home/alice/.config/meshforge/config.toml"
        ),
        ".config is a symlink": (
            "mkdir -p /root/cfg/meshforge && cp /home/alice/.config/meshforge/config.toml "
            "/root/cfg/meshforge/ && rm -rf /home/alice/.config && "
            "ln -s /root/cfg /home/alice/.config"
        ),
    }
    for why, setup in cases.items():
        lab.reset()
        b.ok(setup)
        # symlinked-ancestor cases are refused with a path; alice's own 700 home
        # is the only thing that could make root reach /root/*
        lab.refused(why, "symbolic link", "not a plain", "alice", "/root", "refus")
    b.ok(f"test -e {SYS_ID}/migration.pending")


def test_symlinked_from_argument_parent_is_refused(lab: Lab) -> None:
    lab.box.ok("ln -s /home/alice/.local/share /srv/viewlink")
    lab.refused("--from through a symlink", "symbolic link", "refus",
                args="--from /srv/viewlink/meshforge/identity")  # fmt: skip


def test_shared_group_writable_parents_refused_private_group_allowed(
    lab: Lab, ident: tuple[Path, str]
) -> None:
    b = lab.box
    b.ok("groupadd devs; usermod -aG devs alice; usermod -aG devs bob")
    for path in (
        "/home/alice/.local/share/meshforge",
        "/home/alice/.local/share",
        "/home/alice",
        SRC,
    ):
        lab.reset()
        b.ok(f"chgrp devs {path} && chmod 2775 {path}")
        lab.refused(f"shared group on {path}", "group", "writable", path)
    # a user-private group with umask 002 (0775 everywhere) is the Ubuntu default
    # and must keep working
    lab.reset()
    b.ok(f"chmod 0775 /home/alice /home/alice/.local /home/alice/.local/share")  # noqa: F541
    b.ok(f"chmod 0775 /home/alice/.local/share/meshforge {SRC}")
    _converges(lab, ident[1])


def test_other_writable_parents_and_sticky_semantics(
    lab: Lab, ident: tuple[Path, str]
) -> None:
    b = lab.box
    # a world-writable, non-sticky ancestor is never acceptable
    lab.reset()
    b.ok("chmod 0777 /home/alice/.local/share")
    lab.refused("0777 ancestor", "writable by every user")
    # a user-owned sticky world-writable ancestor: not root-owned -> refused
    lab.reset()
    b.ok("chmod 1777 /home/alice/.local/share")
    lab.refused("alice-owned 1777 ancestor", "writable by every user")
    # root-owned sticky ancestor (/tmp style) with an alice-owned private subtree: fine
    lab.reset()
    b.ok("mkdir -p /tmp/stage && chown alice:alice /tmp/stage && chmod 0700 /tmp/stage")
    b.ok(f"cp -a {SRC} /tmp/stage/identity")
    assert b.ok("stat -c %a /tmp").strip() == "1777"
    r = b.run("meshforge-agent migrate-identity --from /tmp/stage/identity")
    assert r.returncode == 0, r.stdout + r.stderr
    b.ok(f"grep -q {ident[1]} {SYS_ID}/node.json")
    # the same under a non-sticky world-writable root-owned parent: refused
    lab.reset()
    b.ok("mkdir -p /tmp/open && chmod 0777 /tmp/open && mkdir /tmp/open/s && "
         "chown alice:alice /tmp/open/s && chmod 0700 /tmp/open/s")  # fmt: skip
    b.ok(f"cp -a {SRC} /tmp/open/s/identity")
    lab.refused("0777 non-sticky /tmp/open", "writable by every user", "/tmp/open",
                args="--from /tmp/open/s/identity")  # fmt: skip


def test_swap_race_between_plan_and_execute_never_leaks(lab: Lab) -> None:
    """alice swaps ``identity`` between the real folder and a symlink to root's
    secret-laden directory as fast as she can while root migrates, 15 times."""
    b = lab.box
    swapper = (
        "import os,sys,time\n"
        f"p='{SRC}';real=p+'.real';evil='/root/evil'\n"
        "end=time.time()+float(sys.argv[1])\n"
        "while time.time()<end:\n"
        "  try:\n"
        "    os.rename(p,real); os.symlink(evil,p); os.unlink(p); os.rename(real,p)\n"
        "  except OSError: pass\n"
    )
    b.ok("cat > /tmp/swap.py && chmod 644 /tmp/swap.py", stdin=swapper)
    outcomes: list[int] = []
    for _ in range(15):
        lab.reset()
        b.ok(f"(runuser -u alice -- {PY} /tmp/swap.py 4 &) ; sleep 0.3")
        r = b.run("meshforge-agent migrate-identity", timeout=120)
        outcomes.append(r.returncode)
        assert "Traceback" not in r.stdout + r.stderr, r.stderr[-1500:]
        b.run("pkill -f swap.py; sleep 0.2")
        leak = b.run(f"grep -rl {MARKER} /var/lib/meshforge /etc/meshforge")
        assert leak.stdout.strip() == "", (
            f"root's secret reached the service: {leak.stdout}"
        )
        assert b.run(f"test -L {SYS_ID}; test -L {SYS_ID}/ca.crt").returncode != 0
        # whatever happened the target is either complete+consistent or absent/blocked
        if r.returncode == 0 and "no per-user identity" in r.stdout + r.stderr:
            pass  # the swap hid the identity at planning time: nothing to move
        elif r.returncode == 0:
            b.ok(f"test -f {SYS_ID}/node.key && test -f {SYS_ID}/node.json")
        else:
            assert b.run(f"test -e {SYS_ID}/node.key").returncode != 0, r.stdout
    assert outcomes  # (record: both 0 and refusals are acceptable)


def test_rotation_in_progress_source_is_not_silently_truncated(
    lab: Lab, ident: tuple[Path, str]
) -> None:
    b = lab.box
    b.ok(f"cp {SRC}/node.key {SRC}/node.key.next && chown alice:alice {SRC}/node.key.next "
         f"&& chmod 600 {SRC}/node.key.next")  # fmt: skip
    snap = lab.tree()
    r = b.run("meshforge-agent migrate-identity")
    text = r.stdout + r.stderr
    assert "Traceback" not in text, text
    if r.returncode == 0:
        # carried over: a half-finished rotation must not be dropped on the floor
        assert b.run(f"test -f {SYS_ID}/node.key.next").returncode == 0, (
            "rotation state (node.key.next) lost in migration", text,
        )  # fmt: skip
        b.ok(f"grep -q {ident[1]} {SYS_ID}/node.json")
    else:
        assert lab.tree() == snap, "refused but changed the source"
        assert b.run(f"test -e {SYS_ID}/node.key").returncode != 0
        assert "node.key.next" in text or "rotation" in text.lower(), text


def test_running_user_agent_holding_the_lock_blocks_the_move(
    lab: Lab, ident: tuple[Path, str]
) -> None:
    b = lab.box
    b.ok(
        f'(runuser -u alice -- {PY} -c "import fcntl,time;'
        f"f=open('{SRC}/identity.lock','a+');fcntl.flock(f,fcntl.LOCK_EX);"
        f'fcntl.lockf(f,fcntl.LOCK_EX);time.sleep(40)" &) ; sleep 1'
    )
    snap = lab.tree()
    t0 = time.monotonic()
    r = b.run("meshforge-agent migrate-identity", timeout=120)
    text = r.stdout + r.stderr
    assert time.monotonic() - t0 < 90
    assert "Traceback" not in text, text
    assert r.returncode != 0, "migrated an identity another process is using"
    assert "lock" in text.lower(), text
    assert lab.tree() == snap
    b.run("pkill -f 'LOCK_EX'")
    time.sleep(1)
    lab.box.ok(f"rm -rf {SYS_ID}")
    _converges(lab, ident[1])


def test_foreign_owned_extra_files_and_hardlinks(lab: Lab) -> None:
    b = lab.box
    # an unexpected extra file owned by root in alice's folder
    # (an unknown file is never read or copied, whoever owns it)
    b.ok(f"echo {MARKER} > {SRC}/stray && chown root:root {SRC}/stray")
    r = b.run("meshforge-agent migrate-identity")
    assert "Traceback" not in r.stdout + r.stderr
    assert (
        b.run(f"grep -rl {MARKER} /var/lib/meshforge /etc/meshforge").stdout.strip()
        == ""
    )
    lab.reset()
    # hard link to a root-owned file inside the folder (root-owned inode -> refused)
    b.ok(f"rm {SRC}/ca.crt && ln /root/secret.txt {SRC}/ca.crt")
    lab.refused("hardlink to root's file", "belongs", "uid", "ca.crt")


@pytest.mark.parametrize("ubuntu", UBUNTU_VERSIONS)
def test_hardened_unit_starts_and_stays_up(
    debs: Debs, ident: tuple[Path, str], ubuntu: str
) -> None:
    b = SystemdBox.start(systemd_image(ubuntu), name_hint=f"r2u{ubuntu}")
    try:
        b.ok("useradd -m alice")
        b.ok("install -d -o alice -g alice -m 0700 /home/alice/.local/share/meshforge")
        b.copy_in(ident[0], SRC)
        b.ok(f"chown -R alice:alice {SRC} && chmod 0700 {SRC}")
        done = b.install(debs.first)
        assert done.returncode == 0, done.stdout[-3000:]
        facts = b.wait_unit(UNIT, ["active"], within=60)
        time.sleep(12)
        facts = b.show(UNIT, "ActiveState", "NRestarts", "Result", "ExecMainStatus")
        assert facts["ActiveState"] == "active" and facts["NRestarts"] == "0", (
            facts, b.journal())  # fmt: skip
        joined = b.journal()
        for bad in ("status=218", "status=226", "status=216", "status=203",
                    "Failed at step", "Permission denied"):  # fmt: skip
            assert bad not in joined, (bad, joined[-2500:])
        # upgrade restarts it under the (possibly newer) unit and it comes back
        assert b.install(debs.second).returncode == 0
        facts = b.wait_unit(UNIT, ["active"], within=60)
        assert b.identity()["node_id"] == ident[1]
        # kill -9 -> restarted
        b.ok(f"kill -9 {facts['MainPID']}")
        again = b.wait_unit(UNIT, ["active"], within=40)
        assert again["MainPID"] != facts["MainPID"] or again["NRestarts"] != "0"
    finally:
        b.remove()
