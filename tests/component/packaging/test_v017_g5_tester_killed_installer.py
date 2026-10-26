"""V0.17 G5 tester (brief §5e rollback matrix, last row; Q39): the installer is
really KILLED (SIGKILL, no clean-up) in the middle of an upgrade of the real
agent ``.deb`` in a systemd container; the next start must run a complete
version and the repair run (``dpkg --configure -a``) must finish the upgrade.

Two cut points, both found by watching the upgrade record, not by timing:

* while dpkg unpacks (record ``phase=unpacking``): the next start runs the kept
  previous version;
* after the switch, during the postinst (record ``phase=configuring``): the next
  start runs the new version (finishing).

A container shares the host's boot id, so "the machine rebooted" is the one
simulated part: the record's ``boot=`` is rewritten, exactly as a reboot would
make it differ.

Slow; needs the agent packages: ``MESHFORGE_TEST_DEB_DIR`` (CI) or a local build.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("cryptography", reason="the test makes a real enrolled identity")

from mf_deb import UNIT, Debs, SystemdBox, built_debs, systemd_image
from mf_deb_upgrade import AGENT_OPT, invariant, seed_laptop

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
    made = SystemdBox.start(systemd_image("24.04"), name_hint="g5kill")
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


def _start_upgrade_and_kill_at(box: SystemdBox, debs: Debs, phase: str) -> None:
    box.copy_in(debs.second, f"/tmp/{debs.second.name}")
    box.ok(f"nohup dpkg -i /tmp/{debs.second.name} >/tmp/dpkg.out 2>&1 &")
    state = f"{AGENT_OPT}/.upgrade/state"
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        seen = box.run(f"grep -s '^phase={phase}$' {state}")
        if seen.returncode == 0:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"the upgrade never reached phase {phase}")
    # no clean-up for the installer or any script it started
    box.run("pkill -9 -x dpkg; pkill -9 -f 'meshforge-agent.postinst|postinst'; true")
    time.sleep(0.5)
    left = box.run("pgrep -x dpkg").stdout.strip()
    assert left == "", f"dpkg survived the kill: {left}"


def _enrolled_laptop(box: SystemdBox, debs: Debs, enrolled: tuple[Path, str]) -> None:
    seed_laptop(box, enrolled[0])
    done = box.install(debs.first)
    assert done.returncode == 0, done.stdout[-4000:]
    assert box.identity()["node_id"] == enrolled[1]


def _reboot(box: SystemdBox) -> None:
    """Another boot id in the record; the service is down until the next start."""
    box.run(
        f"sed -i 's/^boot=.*/boot=before-the-power-cut/' {AGENT_OPT}/.upgrade/state"
    )
    box.run(f"systemctl stop {UNIT}; systemctl reset-failed {UNIT}; true")


def test_killed_during_the_unpack_the_next_start_runs_a_complete_previous_version(
    box: SystemdBox, debs: Debs, enrolled: tuple[Path, str]
) -> None:
    _enrolled_laptop(box, debs, enrolled)
    version = debs.bundle_version
    _start_upgrade_and_kill_at(box, debs, "unpacking")
    _reboot(box)
    box.ok(f"systemctl start {UNIT}")
    box.wait_unit(UNIT, ["active"], within=90)
    pointer = box.ok(f"readlink {AGENT_OPT}/current").strip()
    assert pointer in (f".previous/{version}", version), pointer
    # whatever the pointer says, the process runs from a COMPLETE tree
    running = invariant(box, UNIT, AGENT_OPT)
    assert running == pointer
    assert box.ok(f"{AGENT_OPT}/{pointer}/bin/meshforge-agent --version").strip()
    assert box.identity()["node_id"] == enrolled[1]
    # the repair: dpkg --configure -a finishes (or re-runs) the upgrade
    done = box.run("dpkg --configure -a 2>&1; dpkg -i /tmp/*.deb 2>&1 | tail -3")
    assert box.show(UNIT, "ActiveState")["ActiveState"] == "active", done.stdout[-2000:]
    status = box.ok("dpkg-query -W -f='${db:Status-Status}' meshforge-agent")
    assert status == "installed", done.stdout[-2000:]
    assert invariant(box, UNIT, AGENT_OPT) == version
    assert box.run(f"test -e {AGENT_OPT}/.previous").returncode != 0
    assert box.identity()["node_id"] == enrolled[1]


def test_killed_after_the_switch_the_next_start_runs_the_new_version_and_repair_confirms(
    box: SystemdBox, debs: Debs, enrolled: tuple[Path, str]
) -> None:
    _enrolled_laptop(box, debs, enrolled)
    version = debs.bundle_version
    _start_upgrade_and_kill_at(box, debs, "configuring")
    _reboot(box)
    box.ok(f"systemctl start {UNIT}")
    box.wait_unit(UNIT, ["active"], within=90)
    assert invariant(box, UNIT, AGENT_OPT) == version
    record = json.loads(box.ok(f"cat {AGENT_OPT}/.upgrade/interrupted.json"))
    assert record["action"] == "finishing"
    done = box.run("dpkg --configure -a 2>&1")
    assert done.returncode == 0, done.stdout[-3000:]
    assert box.show(UNIT, "ActiveState")["ActiveState"] == "active"
    assert invariant(box, UNIT, AGENT_OPT) == version
    assert box.run(f"test -e {AGENT_OPT}/.upgrade/state").returncode != 0
    assert box.identity()["node_id"] == enrolled[1]
