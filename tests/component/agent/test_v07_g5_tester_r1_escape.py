"""V0.7 G5 tester round 1: more escape attempts in a REAL container, beyond
``test_v07_g5_sandbox_escape.py``.

Kernel-interface escapes (cgroup release_agent, core_pattern, /proc/self/exe
overwrite, bpf / perf / keyctl / userfaultfd, open_by_handle_at "shocker",
chroot, setuid) and host-visibility checks (host pids, /proc/1/root, the Docker
socket, the cloud metadata address). Every probe must fail inside; the host's
files stay as they were.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")

from mf_runtime_harness import Capture, Target, containers_with_label, new_label

from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.docker_support import open_client
from meshforge.agent.execution.runtime import Stream
from meshforge.agent.settings import PYTHON_CPU_IMAGE

pytestmark = [pytest.mark.docker, pytest.mark.timeout(120)]


def _image_local(image: str) -> bool:
    client = open_client(10)
    try:
        client.images.get(image)
    except Exception:  # noqa: BLE001
        return False
    finally:
        client.close()
    return True


@pytest.fixture
def target() -> Iterator[Target]:
    if not _image_local(PYTHON_CPU_IMAGE):
        pytest.skip(f"{PYTHON_CPU_IMAGE} is not local: docker pull it first")
    label = new_label()
    yield Target(
        DockerRuntime(DockerConfig(agent_label=label)),
        "docker",
        PYTHON_CPU_IMAGE,
        label,
        "python",
    )
    assert containers_with_label(label) == []


PRELUDE = textwrap.dedent(
    """
    import ctypes, errno, json, os, socket, subprocess
    libc = ctypes.CDLL(None, use_errno=True)
    def err():
        return errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
    def tryopen(path, mode):
        try:
            with open(path, mode) as f:
                if "r" in mode:
                    f.read(16)
            return "OPENED"
        except OSError as exc:
            return errno.errorcode.get(exc.errno, str(exc.errno))
    r = {}
    """
)


def _run(target: Target, tmp_path: Path, body: str) -> dict[str, Any]:
    code = PRELUDE + textwrap.dedent(body) + "\nprint(json.dumps(r), flush=True)\n"
    spec = target.spec(tmp_path, code)
    capture = Capture()
    result = asyncio.run(target.runtime.run(spec, capture))
    lines = capture.data(Stream.STDOUT).decode(errors="replace").strip().splitlines()
    assert lines, (result, capture.data(Stream.STDERR)[-2000:])
    found: dict[str, Any] = json.loads(lines[-1])
    return found


def test_kernel_interfaces_that_escape_a_weak_container_are_closed(
    target: Target, tmp_path: Path
) -> None:
    found = _run(
        target,
        tmp_path,
        """
        r["release_agent_write"] = tryopen("/sys/fs/cgroup/release_agent", "w")
        r["cgroup_mkdir"] = "ok"
        try:
            os.mkdir("/sys/fs/cgroup/escape")
        except OSError as exc:
            r["cgroup_mkdir"] = errno.errorcode.get(exc.errno)
        r["core_pattern"] = tryopen("/proc/sys/kernel/core_pattern", "w")
        r["modprobe"] = tryopen("/proc/sys/kernel/modprobe", "w")
        r["proc_self_exe_w"] = tryopen("/proc/self/exe", "w")
        r["kcore"] = tryopen("/proc/kcore", "r")
        r["kmem"] = tryopen("/dev/kmem", "r")
        r["mem"] = tryopen("/dev/mem", "r")
        r["sda"] = tryopen("/dev/sda", "r")
        r["sys_kernel_w"] = tryopen("/sys/kernel/uevent_helper", "w")
        r["docker_sock"] = tryopen("/var/run/docker.sock", "r")
        r["dockerenv_host_root"] = tryopen("/proc/1/root/etc/shadow", "r")
        r["setuid_root"] = "ok"
        try:
            os.setuid(0)
            r["setuid_root"] = "ALREADY_ROOT_OR_ALLOWED" if os.getuid() == 0 else "ok"
        except OSError as exc:
            r["setuid_root"] = errno.errorcode.get(exc.errno)
        try:
            os.chroot("/work")
            r["chroot"] = "ALLOWED"
        except OSError as exc:
            r["chroot"] = errno.errorcode.get(exc.errno)
        # raw syscalls: bpf(2), perf_event_open, keyctl, userfaultfd, open_by_handle_at
        NR = {"bpf": 321, "perf_event_open": 298, "keyctl": 250,
              "userfaultfd": 323, "open_by_handle_at": 304, "kexec_load": 246,
              "init_module": 175, "reboot": 169, "swapon": 167, "pivot_root": 155}
        for name, number in NR.items():
            ctypes.set_errno(0)
            rc = libc.syscall(number, 0, 0, 0, 0, 0, 0)
            r["sys_" + name] = err() if rc == -1 else "RETURNED_%d" % rc
        r["uid"] = os.getuid()
        r["euid"] = os.geteuid()
        """,
    )
    blocked = {"EACCES", "EPERM", "EROFS", "ENOENT", "ENXIO", "ENOSYS", "ENODEV"}
    bad = {
        k: v
        for k, v in found.items()
        if k not in ("uid", "euid", "cgroup_mkdir")
        and v not in blocked
        and k != "setuid_root"
    }
    # bpf/keyctl/userfaultfd with bogus args may legitimately answer EINVAL/EFAULT:
    # what must never happen is a success.
    bad = {
        k: v
        for k, v in bad.items()
        if not (k.startswith("sys_") and v in ("EINVAL", "EFAULT", "EBADF"))
    }
    assert bad == {}, found
    assert found["cgroup_mkdir"] in blocked | {"EEXIST"}, found
    assert found["uid"] != 0 and found["euid"] != 0, found
    assert found["setuid_root"] in blocked, found


def test_the_container_sees_no_host_processes_and_no_metadata_service(
    target: Target, tmp_path: Path
) -> None:
    found = _run(
        target,
        tmp_path,
        """
        pids = [int(n) for n in os.listdir("/proc") if n.isdigit()]
        r["pids"] = len(pids)
        r["max_pid"] = max(pids)
        r["has_init_owner"] = open("/proc/1/cmdline", "rb").read()[:20].decode("latin1")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        try:
            s.connect(("169.254.169.254", 80))
            r["metadata"] = "CONNECTED"
        except OSError as exc:
            r["metadata"] = errno.errorcode.get(exc.errno, "TIMEOUT")
        r["ifaces"] = sorted(os.listdir("/sys/class/net"))
        """,
    )
    assert found["pids"] < 10, found  # the container's own few processes
    assert "systemd" not in found["has_init_owner"], found
    assert found["metadata"] != "CONNECTED", found
    assert found["ifaces"] == ["lo"], found


def test_a_symlink_the_job_plants_never_reads_outside_the_workdir_via_the_host(
    target: Target, tmp_path: Path
) -> None:
    """The job plants symlinks aimed at host files; they only dangle inside the
    container (the host path does not exist there), and the host files are
    untouched by writing through them."""
    secret = tmp_path.parent / f"{tmp_path.name}-secret.txt"
    secret.write_text("TOP SECRET")
    found = _run(
        target,
        tmp_path,
        f"""
        os.symlink({str(secret)!r}, "/work/leak")
        r["read"] = tryopen("/work/leak", "r")
        r["write"] = tryopen("/work/leak", "w")
        """,
    )
    assert found["read"] == "ENOENT" and found["write"] == "ENOENT", found
    assert secret.read_text() == "TOP SECRET"
