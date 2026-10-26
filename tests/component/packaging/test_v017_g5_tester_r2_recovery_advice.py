"""V0.17 G5 tester round 2: the recovery ADVICE for a cut-off upgrade, attacked
with the real agent ``.deb`` and the real dpkg in a systemd container.

The shipped advice differs by phase:

* cut off while unpacking  -> ``sudo dpkg -i ./<new>.deb`` (then
  ``apt --fix-broken install`` if apt asks); ``dpkg --configure -a`` is NOT
  enough there;
* cut off after the switch -> ``sudo dpkg --configure -a``.

For each phase: the service/doctor text says that, following it really repairs
the machine (healthy new version, same node_id, no upgrade leftovers, verifier
clean after removal), and the OTHER advice does what the docs claim it does.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("cryptography", reason="the test makes a real enrolled identity")

from mf_deb import UNIT, Debs, SystemdBox, built_debs, systemd_image
from mf_deb_upgrade import AGENT_OPT, invariant, seed_laptop, verify

pytestmark = [
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.timeout(3600),
    pytest.mark.skipif(sys.platform != "linux", reason="Linux package"),
]


@pytest.fixture(scope="module")
def debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    return built_debs(tmp_path_factory.mktemp("debs"))


@pytest.fixture
def box() -> Iterator[SystemdBox]:
    made = SystemdBox.start(systemd_image("24.04"), name_hint="g5adv")
    try:
        yield made
    finally:
        made.remove()


@pytest.fixture
def enrolled(tmp_path: Path) -> tuple[Path, str]:
    from mf_fabric import enroll_identity

    from meshforge.agent.identity import load_or_create

    folder = tmp_path / "identity"
    identity = load_or_create(folder)
    enroll_identity(identity)
    return folder, identity.node_id


def _cut_off(
    box: SystemdBox, debs: Debs, enrolled: tuple[Path, str], phase: str
) -> str:
    seed_laptop(box, enrolled[0])
    assert box.install(debs.first).returncode == 0
    deb = f"/tmp/{debs.second.name}"
    box.copy_in(debs.second, deb)
    box.ok(f"nohup dpkg -i {deb} >/tmp/dpkg.out 2>&1 &")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if (
            box.run(f"grep -s '^phase={phase}$' {AGENT_OPT}/.upgrade/state").returncode
            == 0
        ):
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"never reached {phase}")
    box.run("pkill -9 -x dpkg; pkill -9 -f postinst; true")
    time.sleep(0.5)
    box.run(
        f"sed -i 's/^boot=.*/boot=before-the-power-cut/' {AGENT_OPT}/.upgrade/state"
    )
    box.run(f"systemctl stop {UNIT}; systemctl reset-failed {UNIT}; true")
    box.ok(f"systemctl start {UNIT}")
    box.wait_unit(UNIT, ["active"], within=90)
    return deb


def _status(box: SystemdBox) -> str:
    return box.run(
        "dpkg-query -W -f='${db:Status-Status}' meshforge-agent"
    ).stdout.strip()


def test_unpack_cut_off_says_dpkg_i_and_dpkg_i_repairs_it(
    box: SystemdBox, debs: Debs, enrolled: tuple[Path, str]
) -> None:
    deb = _cut_off(box, debs, enrolled, "unpacking")
    version = debs.bundle_version
    # what every reader of the machine is told
    journal = box.journal(UNIT, lines=200)
    assert (
        "dpkg -i" in journal
        and "dpkg --configure -a" not in journal.split("interrupted")[-1][:400]
    )
    doctor = box.run("meshforge-agent doctor").stdout
    assert "dpkg -i" in doctor, doctor
    # the machine really is in the state the advice is about
    assert _status(box) != "installed"
    # following it
    done = box.run(f"dpkg -i {deb} 2>&1")
    assert done.returncode == 0, done.stdout[-3000:]
    assert _status(box) == "installed"
    box.wait_unit(UNIT, ["active"], within=90)
    assert invariant(box, UNIT, AGENT_OPT) == version
    assert box.identity()["node_id"] == enrolled[1]
    assert box.run(f"test -e {AGENT_OPT}/.previous").returncode != 0
    assert box.run(f"test -e {AGENT_OPT}/.upgrade/state").returncode != 0
    assert box.run("meshforge-agent doctor").returncode == 0
    assert box.apt("remove -y meshforge-agent").returncode == 0
    verify(box, "advice-unpack", data_removed=False)


def test_unpack_cut_off_configure_a_alone_is_not_the_repair(
    box: SystemdBox, debs: Debs, enrolled: tuple[Path, str]
) -> None:
    """The docs say ``dpkg --configure -a`` does not repair a cut-off UNPACK. If
    it did, the advice would be needlessly heavy; if it half-did, it could
    leave a lying 'installed'. Whatever it does must not claim success over a
    missing/partial tree, and the node must still run a complete version."""
    _cut_off(box, debs, enrolled, "unpacking")
    box.run("dpkg --configure -a 2>&1")
    running = invariant(box, UNIT, AGENT_OPT)  # a complete tree runs
    assert running
    assert box.identity()["node_id"] == enrolled[1]
    if _status(box) == "installed":
        # then it claims a finished upgrade: the new version must really run
        assert running == debs.bundle_version
        assert box.run("meshforge-agent doctor").returncode == 0


def test_unpack_cut_off_apt_fix_broken_never_costs_the_identity(
    box: SystemdBox, debs: Debs, enrolled: tuple[Path, str]
) -> None:
    """The advice adds 'then sudo apt --fix-broken install if apt asks'. Run it
    as a user would after the cut-off: whatever apt decides (it may remove the
    half-installed package), the node's identity and settings must survive."""
    _cut_off(box, debs, enrolled, "unpacking")
    box.run("apt-get --fix-broken install -y 2>&1 | tail -5")
    assert box.run("test -s /var/lib/meshforge/identity/node.crt").returncode == 0
    ident = box.run("cat /var/lib/meshforge/identity/node.crt | wc -c").stdout.strip()
    assert int(ident) > 100


def test_switch_cut_off_says_configure_a_and_it_repairs_it(
    box: SystemdBox, debs: Debs, enrolled: tuple[Path, str]
) -> None:
    _cut_off(box, debs, enrolled, "configuring")
    version = debs.bundle_version
    journal = box.journal(UNIT, lines=200)
    assert re.search(r"interrupted after the switch.*dpkg --configure -a", journal)
    doctor = box.run("meshforge-agent doctor").stdout
    assert "dpkg --configure -a" in doctor, doctor
    assert _status(box) in ("unpacked", "half-configured", "half-installed")
    done = box.run("dpkg --configure -a 2>&1")
    assert done.returncode == 0, done.stdout[-3000:]
    assert _status(box) == "installed"
    box.wait_unit(UNIT, ["active"], within=90)
    assert invariant(box, UNIT, AGENT_OPT) == version
    assert box.identity()["node_id"] == enrolled[1]
    assert box.run(f"test -e {AGENT_OPT}/.upgrade/state").returncode != 0
    assert box.run(f"test -e {AGENT_OPT}/.previous").returncode != 0
    assert box.run("meshforge-agent doctor").returncode == 0
    assert box.apt("purge -y meshforge-agent").returncode == 0
    verify(box, "advice-configure", data_removed=True)


def test_switch_cut_off_dpkg_i_also_works_and_leaves_one_version(
    box: SystemdBox, debs: Debs, enrolled: tuple[Path, str]
) -> None:
    """A user who follows the OTHER advice for a switch cut-off must not end in
    a worse state."""
    deb = _cut_off(box, debs, enrolled, "configuring")
    done = box.run(f"dpkg -i {deb} 2>&1")
    assert done.returncode == 0, done.stdout[-3000:]
    assert _status(box) == "installed"
    assert invariant(box, UNIT, AGENT_OPT) == debs.bundle_version
    assert box.run(f"test -e {AGENT_OPT}/.previous").returncode != 0
