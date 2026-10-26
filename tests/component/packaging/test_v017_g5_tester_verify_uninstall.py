"""V0.17 G5 tester (brief §5e item 5): ``lab/verify_uninstall.py --package
agent|controller`` against a REAL probe run in a real container (not canned
facts): upgrade leftovers (``.previous``, ``.upgrade``, version folders, pointer
temp links) on a shared machine, one package of two removed.

The container is a plain Ubuntu (the project's systemd test image with its init
replaced by ``sleep``): the verifier only reads the file system, ``dpkg-query``
and ``getent``. "Installed" is faked with dpkg status stanzas.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from mf_deb import docker_cli_missing, systemd_image
from mf_test_containers import meshforge_test_labels

pytestmark = [pytest.mark.component, pytest.mark.docker]

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "lab" / "verify_uninstall.py"


class Box:
    def __init__(self, name: str) -> None:
        self.name = name

    def sh(self, command: str) -> None:
        done = subprocess.run(
            ["docker", "exec", self.name, "sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == 0, done.stderr + done.stdout

    def installed(self, package: str, state: str = "install ok installed") -> None:
        self.sh(
            "printf 'Package: %s\\nStatus: %s\\nArchitecture: amd64\\nVersion: 0.1.0\\n"
            "Description: x\\n\\n' "
            f"{package} '{state}' >> /var/lib/dpkg/status"
        )

    def verify(
        self, package: str, *, purge: bool = False
    ) -> tuple[int, dict[str, object]]:
        argv = [
            sys.executable,
            str(SCRIPT),
            "--os",
            "linux",
            "--docker",
            self.name,
            "--package",
            package,
            "--json",
        ]
        if purge:
            argv.append("--data-removed")
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=120, check=False
        )
        assert done.returncode in (0, 1), done.stderr + done.stdout
        return done.returncode, dict(json.loads(done.stdout))


@pytest.fixture
def box() -> Iterator[Box]:
    missing = docker_cli_missing()
    if missing:
        pytest.skip(missing)
    image = systemd_image("24.04")
    name = f"mf-test-vu-{time.monotonic_ns() % 10**9}"
    argv = ["docker", "run", "-d", "--name", name, "--entrypoint", "sleep"]
    for key, value in meshforge_test_labels().items():
        argv += ["--label", f"{key}={value}"]
    subprocess.run([*argv, image, "600"], check=True, capture_output=True, timeout=120)
    try:
        made = Box(name)
        # an Ubuntu base has no meshforge user; the stanza file exists
        made.sh("test -f /var/lib/dpkg/status")
        yield made
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, timeout=120, check=False
        )


def _leftovers(result: dict[str, object]) -> list[str]:
    return [str(x) for x in result["leftovers"]]  # type: ignore[attr-defined]


@pytest.mark.parametrize("package", ["all", "agent", "controller"])
@pytest.mark.parametrize("purge", [False, True])
def test_a_clean_machine_is_clean_in_every_scope(
    box: Box, package: str, purge: bool
) -> None:
    code, result = box.verify(package, purge=purge)
    assert code == 0, result
    assert result["clean"] is True and _leftovers(result) == []
    assert result["package"] == package


def test_agent_upgrade_leftovers_are_found_when_the_agent_alone_is_removed(
    box: Box,
) -> None:
    # agent removed (nothing installed), but a rollback copy and the upgrade
    # record were left in the program folder
    box.sh(
        "mkdir -p /opt/meshforge/.previous/0.17.0 /opt/meshforge/.upgrade "
        "&& echo phase=x > /opt/meshforge/.upgrade/state"
    )
    code, result = box.verify("agent")
    assert code == 1
    assert any("/opt/meshforge" in item for item in _leftovers(result)), result


def test_shared_machine_agent_removed_controller_stays(box: Box) -> None:
    """The controller (installed, running its own tree) must not be reported;
    the agent's leftovers must be, entry by entry."""
    box.installed("meshforge-controller")
    box.sh(
        "mkdir -p /opt/meshforge/controller/0.17.1 /opt/meshforge/controller/.upgrade "
        "/opt/meshforge/controller/.previous/0.17.0 /var/lib/meshforge/db "
        "&& ln -s 0.17.1 /opt/meshforge/controller/current"
    )
    code, result = box.verify("agent", purge=False)
    assert code == 0, result  # the controller's tree is not the agent's leftover
    # now the agent's upgrade leftovers appear
    box.sh(
        "mkdir -p /opt/meshforge/.previous/0.17.0 /opt/meshforge/.upgrade "
        "/opt/meshforge/0.17.0 && ln -s 0.17.0 /opt/meshforge/.current.4242"
    )
    code, result = box.verify("agent", purge=False)
    assert code == 1
    text = "\n".join(_leftovers(result))
    for name in (".previous", ".upgrade", "0.17.0", ".current.4242"):
        assert f"/opt/meshforge/{name}" in text, (name, text)
    assert "/opt/meshforge/controller" not in text  # not blamed on the agent
    # remove them one by one: each removal shrinks the report, last one clears it
    box.sh("rm -rf /opt/meshforge/.previous /opt/meshforge/.upgrade")
    code, result = box.verify("agent")
    assert code == 1 and ".previous" not in "\n".join(_leftovers(result))
    box.sh("rm -rf /opt/meshforge/0.17.0 /opt/meshforge/.current.4242")
    code, result = box.verify("agent")
    assert code == 0, result


def test_shared_machine_controller_removed_agent_stays(box: Box) -> None:
    box.installed("meshforge-agent")
    box.sh(
        "mkdir -p /opt/meshforge/0.17.1 /opt/meshforge/.previous/0.17.0 "
        "/opt/meshforge/.upgrade && ln -s .previous/0.17.0 /opt/meshforge/current "
        "&& mkdir -p /var/lib/meshforge/identity /etc/meshforge"
    )
    # a rolled-back agent: its .previous is what RUNS, not a controller leftover
    code, result = box.verify("controller")
    assert code == 0, result
    box.sh("mkdir -p /opt/meshforge/controller/.previous/0.17.0")
    code, result = box.verify("controller")
    assert code == 1
    assert any("/opt/meshforge/controller" in item for item in _leftovers(result))


def test_the_last_package_gone_leaves_nothing_in_the_shared_folders(box: Box) -> None:
    box.installed("meshforge-agent")
    box.installed("meshforge-controller")
    box.sh("mkdir -p /opt/meshforge/controller/.upgrade /opt/meshforge/.previous/0.1")
    # both removed in sequence; the second one must see the shared folders
    box.sh("sed -i '/^Package: meshforge-agent$/,/^$/d' /var/lib/dpkg/status")
    code, result = box.verify("agent")
    assert code == 1, result  # the agent's own .previous is a leftover even now
    assert "/opt/meshforge/.previous" in "\n".join(_leftovers(result))
    box.sh("rm -rf /opt/meshforge/.previous")
    code, result = box.verify("agent")
    assert code == 0, result
    box.sh("sed -i '/^Package: meshforge-controller$/,/^$/d' /var/lib/dpkg/status")
    code, result = box.verify("controller")
    assert code == 1, result
    assert any("/opt/meshforge" in item for item in _leftovers(result))
    box.sh("rm -rf /opt/meshforge")
    code, result = box.verify("controller")
    assert code == 0, result


def test_data_removed_flags_the_removed_mark_and_identity(box: Box) -> None:
    box.sh(
        "mkdir -p /var/lib/meshforge/identity && touch /var/lib/meshforge/.package-removed"
    )
    code, result = box.verify("agent", purge=False)
    assert code == 0, result  # remove keeps data on purpose
    # with purge the data must be reported by name
    code, result = box.verify("agent", purge=True)
    assert code == 1
    assert any("/var/lib/meshforge" in item for item in _leftovers(result))


def test_json_artefact_is_complete(box: Box) -> None:
    box.sh("mkdir -p /opt/meshforge/.previous")
    _, result = box.verify("agent")
    assert set(result) >= {
        "os",
        "data_removed",
        "package",
        "leftovers",
        "kept",
        "notes",
        "clean",
    }
    assert result["os"] == "linux" and result["clean"] is False
