"""D63-2 on a real daemon: the attempt workdir is owner-only.

* After a real run the workdir is ``0o700`` and the workload (the agent's own uid,
  ``run_as = "auto"``) could write in it.
* **Another local uid cannot enter it**: a throw-away container running as a
  different uid, with the *work root* bind-mounted, is refused at the attempt dir.
  (Docker is used as "another user on this laptop" - no sudo needed.)

Skips without Docker, without the local ``python-cpu`` image, on Windows, or when
the test runs as root (root enters any directory, so the check would be moot).
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")

from mf_runtime_harness import Capture, Target, containers_with_label, new_label

from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.docker_support import open_client
from meshforge.agent.execution.runtime import Stream
from meshforge.agent.settings import PYTHON_CPU_IMAGE

pytestmark = [
    pytest.mark.docker,
    pytest.mark.timeout(120),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions"),
]

OTHER_UID = 54321


def _image_local() -> bool:
    try:
        client = open_client(10)
    except Exception:  # noqa: BLE001
        return False
    try:
        client.images.get(PYTHON_CPU_IMAGE)
    except Exception:  # noqa: BLE001
        return False
    finally:
        client.close()
    return True


def test_another_local_uid_cannot_enter_the_attempt_workdir(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("running as root: every directory is enterable")
    if not _image_local():
        pytest.skip(f"{PYTHON_CPU_IMAGE} is not local: docker pull it first")
    label = new_label()
    target = Target(
        DockerRuntime(DockerConfig(agent_label=label)),
        "docker",
        PYTHON_CPU_IMAGE,
        label,
        "python",
    )
    root = tmp_path / "work"
    root.mkdir(mode=0o700)
    attempt = root / "attempt_x"
    attempt.mkdir(mode=0o777)
    attempt.chmod(0o777)  # start wide open: the runtime must close it
    code = "open('/work/written.txt', 'w').write('ok'); print('WROTE')"
    capture = Capture()
    result = asyncio.run(target.runtime.run(target.spec(attempt, code), capture))
    assert result.ok, capture.data(Stream.STDERR)
    assert (attempt / "written.txt").read_text() == "ok"
    assert stat.S_IMODE(attempt.stat().st_mode) == 0o700
    assert containers_with_label(label) == []

    # Positive control first: the probe *can* enter an open directory, so a
    # DENIED below is the attempt dir's permissions, not a broken probe/mount.
    tmp_path.chmod(0o755)
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    open_dir.chmod(0o755)
    root.chmod(0o711)  # traversable, so only the attempt dir's own mode decides
    assert _other_uid_listing(tmp_path, "open") == "ENTERED"
    assert _other_uid_listing(tmp_path, "work/attempt_x") == "DENIED"


def _other_uid_listing(parent: Path, relative: str) -> str:
    client = open_client(10)
    try:
        output = client.containers.run(
            PYTHON_CPU_IMAGE,
            [
                "sh",
                "-c",
                f"ls /mnt/{relative} >/dev/null 2>&1 && echo ENTERED || echo DENIED",
            ],
            user=f"{OTHER_UID}:{OTHER_UID}",
            volumes={str(parent): {"bind": "/mnt", "mode": "ro"}},
            network_mode="none",
            remove=True,
            labels={"meshforge.test": "1"},
        )
    finally:
        client.close()
    return bytes(output).decode().strip()
