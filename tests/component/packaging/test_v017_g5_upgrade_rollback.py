"""V0.17 G5 (brief §5e rollback matrix, Linux rows; Q35/Q38/Q39): the real
agent and controller ``.deb`` in clean Ubuntu systemd containers, every failure
point driven for real.

Agent (an enrolled laptop: its identity was migrated from a per-user agent, so
the health check also needs ``agent.started``):

* **new version unhealthy** (the Q38 trigger in ``/opt/meshforge/.upgrade``):
  ``apt install`` of revision 2 rolls back to the hard-link copy in
  ``.previous`` - the previous version runs, same ``node_id``, settings kept,
  the install output and ``meshforge-agent doctor`` say so; the trigger is
  one-shot, so installing revision 2 again works and removes ``.previous``;
* **power loss in the unpack** (``dpkg --unpack`` + another boot id in the
  upgrade record): the next start runs the kept previous version
  (``interrupted.json``); dpkg has the package half-way, so the repair is to
  install the new package again (``dpkg -i``; ``apt install --reinstall`` refuses
  that state), as doctor says;
* **rollback -> purge** and **upgrade -> remove**: the uninstall verifier is
  clean (``.previous`` / ``.upgrade`` are the likely leak).

Controller (24.04, with the agent package next to it and a real job in its
database): the trigger fails the post-migration health check -> G2 restores
the pre-upgrade backup and exits 4 -> postinst switches back to ``.previous``
-> the previous version runs on the restored database, the job is still
there, the agent reconnects. Then both packages are removed in both orders
with ``verify_uninstall --package`` for each.

After every outcome: the running process's interpreter lives in the folder
``current`` resolves to (the pointer/binary invariant).

Slow: CI's package-linux job runs it with the packages it built.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("cryptography", reason="the test makes a real enrolled identity")

from mf_deb import (
    CONTROLLER_UNIT,
    UNIT,
    Debs,
    SystemdBox,
    built_controller_debs,
    built_debs,
    systemd_image,
)
from mf_deb_upgrade import (
    AGENT_OPT,
    CTL_COMPUTE,
    CTL_OPT,
    connected,
    fabric_with_a_job,
    invariant,
    seed_laptop,
    trigger,
    verify,
)

pytestmark = [
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.timeout(3600),
    pytest.mark.skipif(sys.platform != "linux", reason="Linux package"),
]


@pytest.fixture(scope="module")
def debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    return built_debs(tmp_path_factory.mktemp("debs"))


@pytest.fixture(scope="module")
def controller_debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    return built_controller_debs(tmp_path_factory.mktemp("controller-debs"))


@pytest.fixture
def box() -> Iterator[SystemdBox]:
    made = SystemdBox.start(systemd_image("24.04"), name_hint="g5")
    try:
        yield made
    finally:
        made.remove()


@pytest.fixture
def enrolled(tmp_path: Path) -> tuple[Path, str]:
    """An enrolled per-user identity (real code, a throw-away CA) + node_id."""
    from mf_fabric import enroll_identity

    from meshforge.agent.identity import load_or_create

    folder = tmp_path / "identity"
    identity = load_or_create(folder)
    enroll_identity(identity)
    return folder, identity.node_id


def _install_enrolled_agent(
    box: SystemdBox, debs: Debs, enrolled: tuple[Path, str]
) -> str:
    seed_laptop(box, enrolled[0])
    done = box.install(debs.first)
    out = done.stdout
    assert done.returncode == 0, out[-4000:]
    assert f"is running version {debs.bundle_version}" in out, out[-3000:]
    assert box.identity()["node_id"] == enrolled[1]
    return enrolled[1]


# ------------------------------------------------------------------- agent
def test_agent_unhealthy_upgrade_rolls_back_then_installs_again_then_removes(
    box: SystemdBox, debs: Debs, enrolled: tuple[Path, str]
) -> None:
    node_id = _install_enrolled_agent(box, debs, enrolled)
    box.ok("meshforge-agent policy set max_cpu_percent=37")
    version = debs.bundle_version
    assert invariant(box, UNIT, AGENT_OPT) == version

    # row 1: the new version fails its health check (Q38 trigger)
    trigger(box, AGENT_OPT, version)
    done = box.install(debs.second)
    out = done.stdout
    assert done.returncode == 0, out[-4000:]  # never a broken dpkg state (Q13)
    assert "UPGRADE FAILED" in out and "forced failure" in out, out[-3000:]
    assert f"ROLLED BACK: the previous version {version} runs again" in out, out
    assert box.ok(f"readlink {AGENT_OPT}/current").strip() == f".previous/{version}"
    facts = box.wait_unit(UNIT, ["active"], within=60)
    assert facts["ActiveState"] == "active"
    assert invariant(box, UNIT, AGENT_OPT) == f".previous/{version}"
    assert box.identity()["node_id"] == node_id
    shown = box.ok("meshforge-agent policy show")
    assert "max_cpu_percent" in shown and "37" in shown, shown
    gone = box.run(f"test -e {AGENT_OPT}/.upgrade/fail-upgrade-to")
    assert gone.returncode != 0  # one-shot
    record = json.loads(box.ok(f"cat {AGENT_OPT}/.upgrade/rolled-back.json"))
    assert record["from"] == version and record["action"] == "rolled_back"
    doctor = box.run("meshforge-agent doctor")
    assert doctor.returncode == 1, doctor.stdout + doctor.stderr
    assert "running the previous version" in doctor.stdout

    # the same package again (the cause fixed): healthy, .previous forgotten
    done = box.apt(f"install -y --reinstall /tmp/{debs.second.name}")
    assert done.returncode == 0, done.stdout[-4000:]
    assert f"is running version {version}" in done.stdout, done.stdout[-3000:]
    assert box.ok(f"readlink {AGENT_OPT}/current").strip() == version
    assert box.run(f"test -e {AGENT_OPT}/.previous").returncode != 0
    assert box.run(f"test -e {AGENT_OPT}/.upgrade/rolled-back.json").returncode != 0
    assert invariant(box, UNIT, AGENT_OPT) == version
    assert box.run("meshforge-agent doctor").returncode == 0
    assert box.identity()["node_id"] == node_id

    # upgrade -> remove: nothing of the upgrade machinery stays
    assert box.apt("remove -y meshforge-agent").returncode == 0
    verify(box, "agent-upgrade-remove", data_removed=False)


def test_agent_rollback_then_purge_leaves_nothing(
    box: SystemdBox, debs: Debs, enrolled: tuple[Path, str]
) -> None:
    _install_enrolled_agent(box, debs, enrolled)
    trigger(box, AGENT_OPT, debs.bundle_version)
    done = box.install(debs.second)
    assert "ROLLED BACK" in done.stdout, done.stdout[-3000:]
    kept = box.run(f"test -d {AGENT_OPT}/.previous/{debs.bundle_version}")
    assert kept.returncode == 0
    assert box.apt("purge -y meshforge-agent").returncode == 0
    verify(box, "agent-rollback-purge", data_removed=True)


def test_agent_power_loss_during_unpack_reverts_then_repair_finishes(
    box: SystemdBox, debs: Debs, enrolled: tuple[Path, str]
) -> None:
    node_id = _install_enrolled_agent(box, debs, enrolled)
    version = debs.bundle_version
    # dpkg unpacks the new version (old files replaced, never configured) and
    # then the power goes: simulated by the upgrade record's boot id.
    box.copy_in(debs.second, f"/tmp/{debs.second.name}")
    done = box.run(f"dpkg --unpack /tmp/{debs.second.name} 2>&1")
    assert done.returncode == 0, done.stdout
    state = box.ok(f"cat {AGENT_OPT}/.upgrade/state")
    assert "phase=unpacking" in state and f"previous=.previous/{version}" in state
    box.ok(f"sed -i 's/^boot=.*/boot=before-the-power-cut/' {AGENT_OPT}/.upgrade/state")
    box.ok(f"systemctl restart {UNIT}")
    box.wait_unit(UNIT, ["active"], within=60)
    assert box.ok(f"readlink {AGENT_OPT}/current").strip() == f".previous/{version}"
    assert invariant(box, UNIT, AGENT_OPT) == f".previous/{version}"
    record = json.loads(box.ok(f"cat {AGENT_OPT}/.upgrade/interrupted.json"))
    assert record["action"] == "reverted"
    assert "interrupted" in box.journal(UNIT)
    doctor = box.run("meshforge-agent doctor")
    assert doctor.returncode == 1, doctor.stdout
    assert "sudo dpkg -i" in doctor.stdout, doctor.stdout
    assert "dpkg --configure -a" not in doctor.stdout
    # the repair the owner is told: install the new package again
    done = box.run(
        f"DEBIAN_FRONTEND=noninteractive dpkg -i /tmp/{debs.second.name} 2>&1"
    )
    assert done.returncode == 0, done.stdout[-3000:]
    assert f"is running version {version}" in done.stdout, done.stdout[-3000:]
    assert box.ok(f"readlink {AGENT_OPT}/current").strip() == version
    assert box.run(f"test -e {AGENT_OPT}/.previous").returncode != 0
    assert box.run(f"test -e {AGENT_OPT}/.upgrade/state").returncode != 0
    assert invariant(box, UNIT, AGENT_OPT) == version
    assert box.identity()["node_id"] == node_id
    status = box.ok("dpkg-query -W -f='${db:Status-Status}' meshforge-agent")
    assert status == "installed"


# -------------------------------------------------------------- controller
def test_controller_failed_migration_restores_and_rolls_back_then_removal_orders(
    box: SystemdBox, controller_debs: Debs, debs: Debs
) -> None:
    job = fabric_with_a_job(box, controller_debs, debs)
    version = controller_debs.bundle_version
    assert invariant(box, CONTROLLER_UNIT, CTL_OPT) == version
    # an "older" last good start, so this is an upgrade with a backup (G2)
    box.ok(
        'sed -i \'s/"version": "[^"]*"/"version": "0.0.9"/\' '
        "/var/lib/meshforge/db/schema-version.json"
    )
    trigger(box, CTL_OPT, version)
    done = box.install(controller_debs.second)
    out = done.stdout
    assert done.returncode == 0, out[-4000:]
    assert "database was restored" in out, out[-3000:]
    assert "ROLLED BACK" in out and f"previous version {version}" in out, out[-3000:]
    assert box.ok(f"readlink {CTL_OPT}/current").strip() == f".previous/{version}"
    facts = box.wait_unit(CONTROLLER_UNIT, ["active"], within=120)
    assert facts["ActiveState"] == "active"
    assert invariant(box, CONTROLLER_UNIT, CTL_OPT) == f".previous/{version}"
    assert "forced upgrade failure" in box.journal(CONTROLLER_UNIT, lines=400)
    assert box.run(f"test -e {CTL_OPT}/.upgrade/fail-upgrade-to").returncode != 0
    # the data survived (restored backup) and the agent reconnects by itself
    assert job in box.ok(f"cd /root && {CTL_COMPUTE} jobs --json")
    connected(box)
    status = json.loads(box.ok("meshforge-controller db status --json"))
    assert status.get("rollback_failed") in (None, False, {}), status

    # the upgrade, for real this time
    done = box.apt(f"install -y --reinstall /tmp/{controller_debs.second.name}")
    assert done.returncode == 0, done.stdout[-4000:]
    assert f"is running version {version}" in done.stdout, done.stdout[-3000:]
    assert box.run(f"test -e {CTL_OPT}/.previous").returncode != 0
    assert invariant(box, CONTROLLER_UNIT, CTL_OPT) == version

    # either removal order, each package verified on its own files
    assert box.apt("purge -y meshforge-controller").returncode == 0
    verify(box, "controller-first", data_removed=True, package="controller")
    assert box.show(UNIT, "ActiveState")["ActiveState"] == "active"
    assert box.apt("purge -y meshforge-agent").returncode == 0
    verify(box, "then-agent", data_removed=True, package="agent")
    verify(box, "both-gone", data_removed=True)


def test_agent_removed_first_next_to_a_controller(
    box: SystemdBox, controller_debs: Debs, debs: Debs
) -> None:
    assert box.install(controller_debs.first).returncode == 0
    assert box.install(debs.first).returncode == 0
    assert box.apt("remove -y meshforge-agent").returncode == 0
    verify(box, "agent-first-remove", data_removed=False, package="agent")
    assert box.apt("purge -y meshforge-agent").returncode == 0
    verify(box, "agent-first-purge", data_removed=True, package="agent")
    facts = box.wait_unit(CONTROLLER_UNIT, ["active"], within=60)
    assert facts["ActiveState"] == "active"
    assert box.apt("purge -y meshforge-controller").returncode == 0
    verify(box, "then-controller", data_removed=True, package="controller")
    verify(box, "nothing-left", data_removed=True)
