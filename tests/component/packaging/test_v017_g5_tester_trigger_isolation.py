"""V0.17 G5 tester (Q38): the forced-failure trigger cannot be planted by the
service account or its jobs - proven with the shipped shell library, as the real
root, in a real container, against a real unprivileged user (``nobody``, the
same position as the ``meshforge`` service user: no write access to
``/opt/meshforge``).

Even when an administrator's mistake makes the upgrade folder world-writable,
the library's own ownership check (second lock) still refuses a file written by
anyone but root.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from mf_deb import docker_cli_missing, systemd_image
from mf_test_containers import meshforge_test_labels

pytestmark = [pytest.mark.component, pytest.mark.docker]

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "packaging" / "linux" / "common" / "upgrade.sh"
OPT = "/opt/mft"


class Box:
    def __init__(self, name: str) -> None:
        self.name = name

    def run(
        self, command: str, *, user: str = "root"
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "exec", "-u", user, self.name, "sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def lib(self, commands: str) -> subprocess.CompletedProcess[str]:
        """The REAL library (no overrides): real install -o root, real
        ownership check, as root."""
        return self.run(
            f"MF_OPT={OPT}; MF_UNIT=meshforge-agent.service; MF_VERSION=0.17.1; "
            f". /upgrade.sh; {commands}"
        )


@pytest.fixture
def box() -> Iterator[Box]:
    missing = docker_cli_missing()
    if missing:
        pytest.skip(missing)
    image = systemd_image("24.04")
    name = f"mf-test-trig-{time.monotonic_ns() % 10**9}"
    argv = ["docker", "run", "-d", "--name", name, "--entrypoint", "sleep"]
    for key, value in meshforge_test_labels().items():
        argv += ["--label", f"{key}={value}"]
    subprocess.run([*argv, image, "600"], check=True, capture_output=True, timeout=120)
    try:
        subprocess.run(
            ["docker", "cp", str(LIB), f"{name}:/upgrade.sh"],
            check=True,
            capture_output=True,
            timeout=60,
        )
        made = Box(name)
        setup = made.run(
            f"mkdir -p {OPT}/0.17.0/bin && echo '{{}}' > {OPT}/0.17.0/BUILDINFO.json "
            f"&& echo x > {OPT}/0.17.0/bin/meshforge-agent && ln -s 0.17.0 {OPT}/current "
            f"&& chown -R root:root {OPT} && chmod 755 {OPT}"
        )
        assert setup.returncode == 0, setup.stderr
        begun = made.lib("mf_upgrade_begin")
        assert begun.returncode == 0, begun.stderr + begun.stdout
        yield made
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, timeout=120, check=False
        )


def test_the_upgrade_folder_is_root_only_as_created_by_the_library(box: Box) -> None:
    mode = box.run(f"stat -c '%U:%G %a' {OPT} {OPT}/.upgrade {OPT}/.previous").stdout
    assert mode.split("\n")[1].startswith("root:root 755"), mode


@pytest.mark.parametrize(
    "attack",
    [
        f"echo '*' > {OPT}/.upgrade/fail-upgrade-to",
        f"ln -s /etc/passwd {OPT}/.upgrade/fail-upgrade-to",
        f"rm -rf {OPT}/.upgrade && mkdir {OPT}/.upgrade"
        f" && echo '*' > {OPT}/.upgrade/fail-upgrade-to",
        f"mv {OPT}/.upgrade {OPT}/.upgrade.old && mkdir {OPT}/.upgrade",
        f"echo '*' > {OPT}/fail-upgrade-to",
        f"rm {OPT}/.upgrade/state",
        f"echo 'phase=unpacking' > {OPT}/.upgrade/state",
        f"ln -sfn /tmp {OPT}/current",
    ],
    ids=[
        "write",
        "symlink",
        "replace-dir",
        "rename-dir",
        "write-in-opt",
        "delete-state",
        "forge-state",
        "repoint-current",
    ],
)
def test_the_unprivileged_user_cannot_touch_the_upgrade_machinery(
    box: Box, attack: str
) -> None:
    done = box.run(attack, user="nobody")
    assert done.returncode != 0, f"{attack!r} was allowed: {done.stdout}{done.stderr}"
    # and nothing now counts as a forced failure
    assert box.lib("mf_forced_failure").returncode != 0
    assert box.run(f"readlink {OPT}/current").stdout.strip() == "0.17.0"


def test_even_a_world_writable_upgrade_folder_does_not_make_a_users_file_count(
    box: Box,
) -> None:
    """An admin mistake (chmod 777): the file's OWNER is still checked."""
    box.run(f"chmod 777 {OPT}/.upgrade")
    planted = box.run(f"echo '*' > {OPT}/.upgrade/fail-upgrade-to", user="nobody")
    assert planted.returncode == 0  # the mistake made it writable...
    assert box.lib("mf_forced_failure").returncode != 0  # ...but it does not count


def test_the_administrators_trigger_counts_and_is_one_shot(box: Box) -> None:
    box.run(f"echo 0.17.1 > {OPT}/.upgrade/fail-upgrade-to")
    assert box.lib("mf_forced_failure").returncode == 0
    # a root-owned symlink is still refused
    box.run(
        f"rm {OPT}/.upgrade/fail-upgrade-to; echo '*' > /root/t; "
        f"ln -s /root/t {OPT}/.upgrade/fail-upgrade-to"
    )
    assert box.lib("mf_forced_failure").returncode != 0
    # a root-owned regular file again, used by a rollback, is consumed
    box.run(
        f"rm {OPT}/.upgrade/fail-upgrade-to; echo 0.17.1 > {OPT}/.upgrade/fail-upgrade-to"
    )
    box.run(
        f"mkdir -p {OPT}/0.17.1/bin && echo '{{}}' > {OPT}/0.17.1/BUILDINFO.json "
        f"&& echo x > {OPT}/0.17.1/bin/meshforge-agent"
    )
    done = box.lib("mf_upgrade_switch; mf_upgrade_rollback forced norestart")
    assert done.returncode == 0, done.stderr + done.stdout
    assert box.run(f"test -e {OPT}/.upgrade/fail-upgrade-to").returncode != 0
    assert box.run(f"readlink {OPT}/current").stdout.strip() == ".previous/0.17.0"
