"""V0.4 G3 (D51): the Docker availability / GPU pass-through probe against the real
daemon, and against a daemon address where nothing listens. Skips without Docker.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")

from meshforge.agent.execution.docker_support import (
    LABEL_AGENT,
    open_client,
    probe_docker,
)
from meshforge.agent.settings import PYTHON_CPU_IMAGE

pytestmark = [pytest.mark.docker, pytest.mark.timeout(120)]


def _host_has_nvidia_gpu() -> bool:
    return shutil.which("nvidia-smi") is not None and Path("/dev/nvidiactl").exists()


def _nvidia_smi_failure() -> str | None:
    """None when ``nvidia-smi`` works on this host, else why it does not
    (``exit 9: NVIDIA-SMI has failed because ...``). V0.7 G6 gate: a host whose
    tool and device node exist but whose driver cannot be reached cannot run a
    GPU container either - the only case in which the positive check below is
    skipped."""
    try:
        done = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{type(exc).__name__}: {exc}"
    if done.returncode == 0:
        return None
    lines = (done.stdout + done.stderr).strip().splitlines()
    return f"exit {done.returncode}: {lines[0] if lines else 'no output'}"


def _local_image() -> str | None:
    client = open_client(10)
    try:
        client.images.get(PYTHON_CPU_IMAGE)
    except Exception:  # noqa: BLE001 - not pulled here: the probe uses its fallback
        return None
    finally:
        client.close()
    return PYTHON_CPU_IMAGE


def test_a_reachable_daemon_is_available_with_a_version() -> None:
    found = probe_docker(
        timeout_s=10,
        host_has_nvidia_gpu=False,
        gpu_probe_image=None,
        agent_label="probe-test",
    )
    assert found.available and found.sandboxed
    assert found.version
    assert (found.gpu_capable, found.gpu_reason) == (False, "no_nvidia_gpu")


def test_gpu_probe_really_runs_a_container_and_leaves_nothing(
    tmp_path: Path,
) -> None:
    has_gpu = _host_has_nvidia_gpu()
    image = _local_image()
    label = f"probe-{tmp_path.name}"[:60]
    found = probe_docker(
        timeout_s=10,
        host_has_nvidia_gpu=True,  # claim a GPU: the probe must *measure* it
        gpu_probe_image=image,
        agent_label=label,
    )
    assert found.available
    # Whatever the probe found, it leaves no container behind.
    client = open_client(10)
    try:
        left = client.containers.list(
            all=True, filters={"label": f"{LABEL_AGENT}={label}"}
        )
    finally:
        client.close()
    assert left == []
    if image is None:
        pytest.skip("the python-cpu image is not local; only the heuristic ran")
    not_capable = {"no_nvidia_container_runtime", "gpu_probe_no_device_node"}
    if has_gpu and not found.gpu_capable:
        broken = _nvidia_smi_failure()
        if broken is not None:
            # The host's own driver is unreachable: the probe must say "not
            # capable" with a typed reason (it did not claim a GPU it cannot
            # use) - checked - and the positive check cannot run here.
            assert found.gpu_reason in not_capable or found.gpu_reason.startswith(
                "gpu_probe_failed"
            ), found.gpu_reason
            pytest.skip(
                "nvidia-smi itself fails on this host "
                f"({broken}): no GPU container can work here; the probe "
                f"answered {found.gpu_reason!r} and left no container"
            )
    if has_gpu:
        # nvidia-smi works on the host: never skipped, the probe must verify.
        assert found.gpu_capable, found.gpu_reason
        assert found.gpu_reason.startswith("verified")
    else:
        assert not found.gpu_capable
        assert found.gpu_reason in not_capable or found.gpu_reason.startswith(
            "gpu_probe_failed"
        )


def test_an_unreachable_daemon_is_a_typed_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DOCKER_HOST", f"unix://{tmp_path / 'nothing-here.sock'}")
    found = probe_docker(
        timeout_s=2,
        host_has_nvidia_gpu=True,
        gpu_probe_image=None,
        agent_label="probe-test",
    )
    assert not found.available
    assert found.reason == "docker_daemon_unreachable"
    assert not found.gpu_capable
