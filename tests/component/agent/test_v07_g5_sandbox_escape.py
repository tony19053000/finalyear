"""V0.7 G5 part 2 (roadmap 0.7.9, D143): escape attempts in a REAL container.

Each test runs a hostile workload in the real Docker sandbox, exactly as the
agent creates it (:class:`DockerRuntime`), and checks two things: the attempt
**fails inside** (the workload reports the error it got), and the **host is
intact** afterwards. The agent's data folder is laid out as in production -
``<data>/identity/node.key`` (a real ADR-0013 identity) beside
``<data>/work/<attempt>`` - so "read the agent key" is the real path.

Attacks: ``mount`` (and a new user namespace, which the default seccomp
profile blocks), ``/proc/sysrq-trigger``, raw and packet sockets, ``ptrace`` of
a host process, a fork bomb, disk fill of the size-capped ``/tmp`` tmpfs and of
the output folder (through the executor's output cap), reaching the
controller / peer ports and a listener on the host under ``network none``,
writing outside the workdir, reading the agent key.

Real Docker only; skips without Docker or without the pinned image.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")

from mf_runtime_harness import Capture, Target, containers_with_label, new_label

from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.docker_support import open_client
from meshforge.agent.execution.runtime import RunResult, RunSpec, Stream
from meshforge.agent.identity import KEY_FILE_NAME, load_or_create
from meshforge.agent.settings import PYTHON_CPU_IMAGE
from meshforge.common.errors import FailureType

pytestmark = [pytest.mark.docker, pytest.mark.timeout(180)]

MIB = 1024 * 1024


def _image_local(image: str) -> bool:
    client = open_client(10)
    try:
        client.images.get(image)
    except Exception:  # noqa: BLE001
        return False
    finally:
        client.close()
    return True


def _target(**config: Any) -> Target:
    label = new_label()
    return Target(
        DockerRuntime(DockerConfig(agent_label=label, **config)),
        "docker",
        PYTHON_CPU_IMAGE,
        label,
        "python",
    )


@pytest.fixture
def target() -> Iterator[Target]:
    if not _image_local(PYTHON_CPU_IMAGE):
        pytest.skip(f"{PYTHON_CPU_IMAGE} is not local: docker pull it first")
    made = _target()
    yield made
    assert containers_with_label(made.label) == [], "a container survived the test"


@pytest.fixture
def data(tmp_path: Path) -> Path:
    """``<data>/identity`` (a real node identity) and ``<data>/work``."""
    root = tmp_path / "data"
    load_or_create(root / "identity")
    (root / "work").mkdir(parents=True)
    (root / "work").chmod(0o700)
    return root


@pytest.fixture
def workdir(data: Path) -> Path:
    work = data / "work" / "att_escape"
    work.mkdir()
    work.chmod(0o700)
    return work


def _host_state(root: Path) -> dict[str, tuple[int, int]]:
    """Every path under ``root`` except the workdir: (size, mtime_ns)."""
    out: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        if "att_escape" in path.parts:
            continue
        info = path.lstat()
        out[str(path.relative_to(root))] = (info.st_size, info.st_mtime_ns)
    return out


def _run(target: Target, spec: RunSpec) -> tuple[RunResult, dict[str, Any]]:
    capture = Capture()
    result = asyncio.run(target.runtime.run(spec, capture))
    lines = capture.data(Stream.STDOUT).decode(errors="replace").strip().splitlines()
    assert lines, (result, capture.data(Stream.STDERR)[-2000:])
    found: dict[str, Any] = json.loads(lines[-1])
    return result, found


PRELUDE = textwrap.dedent(
    """
    import ctypes, errno, json, os, socket
    libc = ctypes.CDLL(None, use_errno=True)
    def err():
        return errno.errorcode.get(ctypes.get_errno(), str(ctypes.get_errno()))
    r = {}
    """
)


def _code(body: str) -> str:
    return PRELUDE + textwrap.dedent(body) + "\nprint(json.dumps(r), flush=True)\n"


# --------------------------------------------------------------- privileges


def test_mount_and_new_namespaces_are_refused(
    target: Target, workdir: Path, data: Path
) -> None:
    before = _host_state(data)
    code = _code(
        """
        os.makedirs("/tmp/m", exist_ok=True)
        rc = libc.mount(b"none", b"/tmp/m", b"tmpfs", 0, None)
        r["mount"] = "ok" if rc == 0 else err()
        rc = libc.mount(b"/work", b"/tmp/m", None, 4096, None)  # MS_BIND
        r["bind"] = "ok" if rc == 0 else err()
        rc = libc.unshare(0x10000000)  # CLONE_NEWUSER
        r["userns"] = "ok" if rc == 0 else err()
        rc = libc.unshare(0x00020000)  # CLONE_NEWNS
        r["mountns"] = "ok" if rc == 0 else err()
        """
    )
    result, r = _run(target, target.spec(workdir, code))
    assert result.ok, result
    assert r["mount"] in ("EPERM", "EACCES"), r
    assert r["bind"] in ("EPERM", "EACCES"), r
    assert r["userns"] in ("EPERM", "EACCES", "ENOSYS"), r
    assert r["mountns"] in ("EPERM", "EACCES"), r
    assert _host_state(data) == before


def test_sysrq_trigger_cannot_be_written(target: Target, workdir: Path) -> None:
    code = _code(
        """
        for path in ("/proc/sysrq-trigger", "/proc/sys/kernel/sysrq"):
            try:
                with open(path, "w") as f:
                    f.write("h")
                r[path] = "written"
            except OSError as e:
                r[path] = errno.errorcode.get(e.errno, str(e.errno))
        """
    )
    result, r = _run(target, target.spec(workdir, code))
    assert result.ok, result
    assert r["/proc/sysrq-trigger"] in ("EROFS", "EACCES", "EPERM", "ENOENT"), r
    assert r["/proc/sys/kernel/sysrq"] in ("EROFS", "EACCES", "EPERM"), r


def test_raw_and_packet_sockets_are_refused(target: Target, workdir: Path) -> None:
    code = _code(
        """
        for name, args in (
            ("raw_icmp", (socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)),
            ("raw_tcp", (socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_TCP)),
            ("packet", (socket.AF_PACKET, socket.SOCK_RAW, 0)),
        ):
            try:
                socket.socket(*args).close()
                r[name] = "opened"
            except OSError as e:
                r[name] = errno.errorcode.get(e.errno, str(e.errno))
        """
    )
    result, r = _run(target, target.spec(workdir, code))
    assert result.ok, result
    for name in ("raw_icmp", "raw_tcp", "packet"):
        assert r[name] in ("EPERM", "EACCES"), r


def test_ptrace_cannot_reach_a_host_process(target: Target, workdir: Path) -> None:
    """The test process itself (the would-be agent) is the victim: its host pid
    does not exist in the container's PID namespace, and attaching to the
    container's own init needs privileges the job does not have."""
    host_pid = os.getpid()
    code = _code(
        f"""
        PTRACE_ATTACH = 16
        rc = libc.ptrace(PTRACE_ATTACH, {host_pid}, None, None)
        r["host"] = "attached" if rc == 0 else err()
        r["host_visible"] = os.path.exists("/proc/{host_pid}/cmdline") and (
            b"pytest" in open("/proc/{host_pid}/cmdline", "rb").read()
        )
        rc = libc.ptrace(PTRACE_ATTACH, 1, None, None)
        r["init"] = "attached" if rc == 0 else err()
        """
    )
    result, r = _run(target, target.spec(workdir, code))
    assert result.ok, result
    assert r["host"] in ("ESRCH", "EPERM"), r
    assert r["host_visible"] is False
    assert r["init"] in ("EPERM", "ESRCH", "EACCES"), r


# ------------------------------------------------------------- resources


def test_a_fork_bomb_is_contained(target: Target, workdir: Path) -> None:
    code = _code(
        """
        import time
        made = 0
        for _ in range(1000):
            try:
                if os.fork() == 0:
                    time.sleep(30)
                    os._exit(0)
                made += 1
            except OSError as e:
                r["stopped_by"] = errno.errorcode.get(e.errno, str(e.errno))
                break
        r["made"] = made
        """
    )
    result, r = _run(target, target.spec(workdir, code, pids=32))
    assert result.failure_type in (None, FailureType.USER_ERROR)
    assert r["made"] < 32 and r["stopped_by"] == "EAGAIN", r


def test_filling_tmp_stops_at_its_size_cap(workdir: Path) -> None:
    if not _image_local(PYTHON_CPU_IMAGE):
        pytest.skip(f"{PYTHON_CPU_IMAGE} is not local: docker pull it first")
    target = _target(tmp_size_mb=16)
    code = _code(
        """
        written = 0
        try:
            with open("/tmp/fill", "wb") as f:
                while written < 256 * 1024 * 1024:
                    f.write(b"\\0" * 1024 * 1024)
                    f.flush()
                    written += 1024 * 1024
            r["stopped_by"] = "nothing"
        except OSError as e:
            r["stopped_by"] = errno.errorcode.get(e.errno, str(e.errno))
        r["written_mib"] = written // (1024 * 1024)
        """
    )
    try:
        result, r = _run(target, target.spec(workdir, code))
    finally:
        assert containers_with_label(target.label) == []
    assert result.ok, result
    assert r["stopped_by"] == "ENOSPC", r
    assert r["written_mib"] <= 16, r
    assert list(workdir.iterdir()) == []  # the tmpfs never touched the host


def test_filling_the_output_folder_is_stopped_by_the_agent(
    target: Target, tmp_path: Path
) -> None:
    """Through the executor (the cap is the agent's, not Docker's): the job
    writes into /work until stopped; the attempt fails output_limit long before
    it could fill the disk."""
    from mf_one_attempt import run_one_attempt

    code = (
        "import time\n"
        "with open('/work/fill.bin', 'wb') as f:\n"
        "    for _ in range(400):\n"
        "        f.write(b'\\1' * (1024 * 1024)); f.flush(); time.sleep(0.01)\n"
    )
    final, workdir_bytes = run_one_attempt(
        tmp_path,
        target.runtime,
        entrypoint=["python", "-c", code],
        execution={"max_written_bytes": 8 * MIB, "output_check_interval_s": 0.1},
    )
    assert final.state == "failed", final
    assert final.failure_type is FailureType.OUTPUT_ERROR
    assert final.message.startswith("output_limit:")
    assert workdir_bytes < 200 * MIB, workdir_bytes


# --------------------------------------------------------------- network


def _bridge_gateway() -> str | None:
    client = open_client(10)
    try:
        configs = client.networks.get("bridge").attrs.get("IPAM", {}).get("Config")
    except Exception:  # noqa: BLE001
        return None
    finally:
        client.close()
    for config in configs or []:
        gateway = config.get("Gateway")
        if gateway and ":" not in gateway:
            return str(gateway)
    return None


REACH = """
targets = TARGETS
for name, host, port in targets:
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect((host, port))
        r[name] = "connected"
    except OSError as e:
        r[name] = errno.errorcode.get(e.errno, type(e).__name__)
    finally:
        s.close()
r["ifaces"] = sorted(os.listdir("/sys/class/net"))
"""


def test_controller_and_peer_ports_are_unreachable_without_a_network(
    target: Target, workdir: Path
) -> None:
    gateway = _bridge_gateway()
    if gateway is None:
        pytest.skip("no IPv4 docker bridge gateway (rootless / Desktop daemon)")
    # A real listener on the host side of the bridge: the positive control
    # proves it is reachable from a container that HAS a network.
    with socket.socket() as listener:
        try:
            listener.bind((gateway, 0))
        except OSError:
            pytest.skip(f"cannot bind the bridge gateway {gateway} here")
        listener.listen(8)
        port = listener.getsockname()[1]
        targets = [
            ("listener", gateway, port),
            ("controller", gateway, 47100),
            ("rest", gateway, 47110),
            ("peer", gateway, 47120),
            ("loopback_listener", "127.0.0.1", port),
        ]
        code = _code(REACH.replace("TARGETS", repr(targets)))
        _, closed = _run(target, target.spec(workdir, code, network=False))
        _, opened = _run(target, target.spec(workdir, code, network=True))
    assert closed["ifaces"] == ["lo"]
    for name in ("listener", "controller", "rest", "peer"):
        assert closed[name] in ("ENETUNREACH", "EHOSTUNREACH", "TimeoutError"), closed
    assert closed["loopback_listener"] == "ECONNREFUSED", closed
    assert opened["listener"] == "connected", opened  # the control


# ----------------------------------------------------------------- files


def test_nothing_outside_the_workdir_can_be_written(
    target: Target, workdir: Path, data: Path
) -> None:
    before = _host_state(data)
    host_targets = [
        str(data / "escape.txt"),
        str(data / "identity" / "escape.txt"),
        str(data / "work" / "escape.txt"),
    ]
    code = _code(
        f"""
        written = []
        for path in {host_targets!r} + [
            "/escape.txt", "/etc/escape.txt", "/usr/escape.txt",
            "/work/../escape.txt", "/proc/self/root/../escape.txt",
        ]:
            try:
                with open(path, "w") as f:
                    f.write("x")
                written.append(path)
            except OSError:
                pass
        os.symlink("/etc", "/work/etc-link")
        try:
            open("/work/etc-link/escape.txt", "w").write("x")
            written.append("via-symlink")
        except OSError:
            pass
        r["written"] = written
        """
    )
    result, r = _run(target, target.spec(workdir, code))
    assert result.ok, result
    assert r["written"] == [], r
    assert _host_state(data) == before
    assert not (data / "escape.txt").exists()


def test_the_agent_key_cannot_be_read(
    target: Target, workdir: Path, data: Path
) -> None:
    key_path = data / "identity" / KEY_FILE_NAME
    secret_line = next(
        line
        for line in key_path.read_text().splitlines()
        if line and not line.startswith("-----")
    )[:40]
    code = _code(
        f"""
        tried = {{}}
        for path in ({str(key_path)!r}, "/work/../identity/{KEY_FILE_NAME}",
                     "/work/../../identity/{KEY_FILE_NAME}"):
            try:
                tried[path] = open(path).read()[:10]
            except OSError as e:
                tried[path] = errno.errorcode.get(e.errno, str(e.errno))
        r["tried"] = tried
        found = []
        for root, dirs, files in os.walk("/"):
            if root.startswith(("/proc", "/sys", "/dev")):
                dirs[:] = []
                continue
            for name in files:
                p = os.path.join(root, name)
                try:
                    if os.path.getsize(p) < 16384 and {secret_line!r} in open(
                        p, errors="ignore"
                    ).read():
                        found.append(p)
                except OSError:
                    pass
        r["found"] = found
        """
    )
    result, r = _run(target, target.spec(workdir, code))
    assert result.ok, result
    assert all(v in ("ENOENT", "EACCES") for v in r["tried"].values()), r
    assert r["found"] == [], r
    assert key_path.read_text().find(secret_line) >= 0  # untouched on the host


def test_the_host_still_has_its_files(data: Path) -> None:
    """Sanity for the fixtures above: the identity exists and is private."""
    key = data / "identity" / KEY_FILE_NAME
    assert key.is_file() and (key.stat().st_mode & 0o777) == 0o600
    assert shutil.disk_usage(data).free > 0
