"""V0.17 G5 (brief §5e item 1, Q36): upgrade from the **previous release** (a
different build, pinned in ``packaging/previous-release.toml``) to the build
under test, in clean Ubuntu systemd containers.

* agent ``.deb`` on 22.04 and 24.04: the same ``node_id``, the config and the
  owner settings kept, the service runs the new version (its console answers
  with the new version), the rollback copy is gone after the good upgrade;
* controller + agent ``.deb`` on one 24.04 machine with a seeded database (a
  real job run before the upgrade): both upgraded, the job still listed,
  migrations applied (``db status`` at head), the agent reconnects, and a
  ``compute run`` job succeeds **after** the upgrade.

Skips (with the reason) until a previous release is pinned, and runs only when
``MESHFORGE_TEST_UPGRADE_FROM_RELEASE=1`` (the CI ``upgrade-from-release`` job);
locally ``MESHFORGE_TEST_PREVIOUS_{AGENT,CONTROLLER}_DEB`` name older builds.
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
    CONTROLLER_UNIT,
    UBUNTU_VERSIONS,
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
    MANIFEST,
    connected,
    fabric_with_a_job,
    invariant,
    seed_laptop,
    verify,
)
from mf_previous_release import previous_asset, require_enabled

pytestmark = [
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.timeout(3600),
    pytest.mark.skipif(sys.platform != "linux", reason="Linux package"),
]

POLICY_LINE: Final = "max_cpu_percent=41"


@pytest.fixture(scope="module")
def debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    require_enabled()
    return built_debs(tmp_path_factory.mktemp("debs"))


@pytest.fixture(scope="module")
def controller_debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    require_enabled()
    return built_controller_debs(tmp_path_factory.mktemp("controller-debs"))


@pytest.fixture
def enrolled(tmp_path: Path) -> tuple[Path, str]:
    from mf_fabric import enroll_identity

    from meshforge.agent.identity import load_or_create

    folder = tmp_path / "identity"
    identity = load_or_create(folder)
    enroll_identity(identity)
    return folder, identity.node_id


def _box(ubuntu: str) -> Iterator[SystemdBox]:
    made = SystemdBox.start(systemd_image(ubuntu), name_hint=f"rel{ubuntu}")
    try:
        yield made
    finally:
        made.remove()


@pytest.fixture(params=UBUNTU_VERSIONS)
def any_box(request: pytest.FixtureRequest) -> Iterator[SystemdBox]:
    yield from _box(str(request.param))


@pytest.fixture
def box2404() -> Iterator[SystemdBox]:
    yield from _box("24.04")


def _console_version(box: SystemdBox) -> str:
    out = box.ok(
        f"for i in $(seq 30); do {AGENT_OPT}/current/python/bin/python3.12 -I -c "
        '"import json,urllib.request as u; print(json.load(u.urlopen('
        "'http://127.0.0.1:47130/console/v1/health', timeout=2))['version'])\" "
        "2>/dev/null && exit 0; sleep 1; done; exit 1"
    )
    return out.strip()


def test_agent_upgrade_from_the_previous_release_keeps_the_node(
    any_box: SystemdBox, debs: Debs, enrolled: tuple[Path, str]
) -> None:
    box = any_box
    previous = previous_asset("agent_deb")
    seed_laptop(box, enrolled[0])
    done = box.install(previous)
    assert done.returncode == 0, done.stdout[-4000:]
    box.wait_unit(UNIT, ["active"], within=60)
    assert box.identity()["node_id"] == enrolled[1]
    box.ok(f"meshforge-agent policy set {POLICY_LINE}")
    config_before = box.ok("cat /etc/meshforge/config.toml")
    old_target = box.ok(f"readlink {AGENT_OPT}/current").strip()

    done = box.install(debs.second)
    assert done.returncode == 0, done.stdout[-4000:]
    assert f"is running version {debs.bundle_version}" in done.stdout, done.stdout[
        -3000:
    ]
    assert box.identity()["node_id"] == enrolled[1]
    assert box.ok("cat /etc/meshforge/config.toml") == config_before
    assert "41" in box.ok("meshforge-agent policy show")
    assert _console_version(box) == debs.bundle_version
    assert invariant(box, UNIT, AGENT_OPT) == debs.bundle_version
    assert box.run(f"test -e {AGENT_OPT}/.previous").returncode != 0
    if old_target != debs.bundle_version:
        assert box.run(f"test -e {AGENT_OPT}/{old_target}").returncode != 0

    assert box.apt("purge -y meshforge-agent").returncode == 0
    verify(box, "release-agent-purge", data_removed=True)


def test_controller_and_agent_upgrade_from_the_previous_release_keep_the_fabric(
    box2404: SystemdBox, debs: Debs, controller_debs: Debs
) -> None:
    box = box2404
    previous_ctl = previous_asset("controller_deb")
    previous_agent = previous_asset("agent_deb")
    old = Debs(previous_ctl, previous_ctl, "")
    old_agent = Debs(previous_agent, previous_agent, "")
    job = fabric_with_a_job(box, old, old_agent)
    node_id = box.identity()["node_id"]

    done = box.install(controller_debs.second)
    assert done.returncode == 0, done.stdout[-4000:]
    assert f"is running version {controller_debs.bundle_version}" in done.stdout, (
        done.stdout[-3000:]
    )
    done = box.install(debs.second)
    assert done.returncode == 0, done.stdout[-4000:]
    assert f"is running version {debs.bundle_version}" in done.stdout, done.stdout[
        -3000:
    ]

    assert invariant(box, CONTROLLER_UNIT, CTL_OPT) == controller_debs.bundle_version
    assert invariant(box, UNIT, AGENT_OPT) == debs.bundle_version
    status = json.loads(box.ok("meshforge-controller db status --json"))
    assert status.get("running") is True, status
    jobs = box.ok(f"cd /root && {CTL_COMPUTE} jobs --json")
    assert job in jobs
    assert box.identity()["node_id"] == node_id
    connected(box)
    box.ok("cat > /root/proj/compute.yaml", stdin=MANIFEST)
    ran = box.ok(f"cd /root && {CTL_COMPUTE} run /root/proj -f", timeout=600)
    assert "hello-from-g5" in ran

    assert box.apt("purge -y meshforge-agent meshforge-controller").returncode == 0
    verify(box, "release-both-purge", data_removed=True)
