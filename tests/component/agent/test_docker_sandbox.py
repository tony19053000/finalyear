"""V0.4 success criterion 5 - the sandbox test the roadmap names - plus GPU
pass-through by exact NVML index (0.4.8) and the orphan sweep (brief D51).

Real containers only. Skips with the reason without Docker, without the
python-cpu image locally, and (GPU cases) without an NVIDIA GPU + the CUDA base
image. Each test uses its own agent label and asserts nothing with it survives.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")

from mf_runtime_harness import (
    Capture,
    Target,
    containers_with_label,
    new_label,
)

from meshforge.agent.execution.docker_runtime import (
    DockerConfig,
    DockerRuntime,
)
from meshforge.agent.execution.docker_support import (
    LABEL_AGENT,
    LABEL_ATTEMPT,
    LABEL_MANAGED,
    LABEL_ROLE,
    open_client,
)
from meshforge.agent.execution.runtime import RunResult, RunSpec, Stream
from meshforge.agent.settings import PYTHON_CPU_IMAGE
from meshforge.common.errors import FailureType

pytestmark = [pytest.mark.docker, pytest.mark.timeout(180)]

CUDA_BASE_IMAGE = "nvidia/cuda:12.6.3-base-ubuntu24.04"
SENTINEL = "mf-g3-sentinel-7c1d0e"


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
    assert containers_with_label(label) == [], "a container survived the test"


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """The attempt workdir, with a host sentinel file *next to* it (not inside)."""
    (tmp_path / "secret.txt").write_text(SENTINEL)
    (tmp_path / "outside").mkdir()
    work = tmp_path / "work"
    work.mkdir()
    return work


def _run(target: Target, spec: RunSpec) -> tuple[RunResult, Capture]:
    capture = Capture()
    return asyncio.run(target.runtime.run(spec, capture)), capture


def _json(capture: Capture) -> dict[str, object]:
    out = capture.data(Stream.STDOUT).decode().strip().splitlines()
    parsed: dict[str, object] = json.loads(out[-1])
    return parsed


# ------------------------------------------------------------ criterion 5
PROBE = textwrap.dedent(
    """
    import json, os, socket
    r = {}
    try:
        r["host_file"] = open(HOST_SECRET).read()
    except OSError as e:
        r["host_file"] = type(e).__name__
    found = []
    for root, dirs, files in os.walk("/"):
        if root.startswith(("/proc", "/sys", "/dev")):
            dirs[:] = []
            continue
        for f in files:
            p = os.path.join(root, f)
            try:
                if os.path.getsize(p) < 4096 and SENTINEL in open(p, errors="ignore").read():
                    found.append(p)
            except OSError:
                pass
    r["sentinel_found_at"] = found
    written = []
    for d in ("/", "/etc", "/usr", "/root", "/var", HOST_OUTSIDE, "/work/.."):
        try:
            with open(os.path.join(d, "mf-escape.txt"), "w") as f:
                f.write("x")
            written.append(d)
        except OSError:
            pass
    r["written_outside_work"] = written
    open("/work/inside.txt", "w").write("ok")
    pids = [p for p in os.listdir("/proc") if p.isdigit()]
    cmds = []
    for p in pids:
        try:
            cmds.append(open(f"/proc/{p}/cmdline", "rb").read().split(b"\\0")[0].decode())
        except OSError:
            pass
    r["processes"] = cmds
    r["uid"], r["gid"] = os.getuid(), os.getgid()
    status = open("/proc/self/status").read()
    r["cap_eff"] = [l.split()[1] for l in status.splitlines() if l.startswith("CapEff")][0]
    r["cap_bnd"] = [l.split()[1] for l in status.splitlines() if l.startswith("CapBnd")][0]
    r["no_new_privs"] = [l.split()[1] for l in status.splitlines() if l.startswith("NoNewPrivs")][0]
    r["net_ifaces"] = sorted(os.listdir("/sys/class/net"))
    try:
        socket.create_connection(("1.1.1.1", 53), timeout=2).close()
        r["net"] = "connected"
    except OSError as e:
        r["net"] = type(e).__name__
    print(json.dumps(r))
    """
)


def test_the_workload_is_confined_to_its_workdir(target: Target, workdir: Path) -> None:
    """Criterion 5: no read/write outside the workdir, no host processes, non-root,
    no capabilities, no network."""
    host_secret = str(workdir.parent / "secret.txt")
    host_outside = str(workdir.parent / "outside")
    code = (
        f"HOST_SECRET={host_secret!r}; HOST_OUTSIDE={host_outside!r}; "
        f"SENTINEL={SENTINEL!r}\n" + PROBE
    )
    before = sorted(p.name for p in workdir.parent.iterdir())
    result, capture = _run(target, target.spec(workdir, code, network=False))
    assert result.ok, (result, capture.data(Stream.STDERR)[-2000:])
    r = _json(capture)
    # read: the host file is not there, and the sentinel is nowhere in the container
    assert r["host_file"] in ("FileNotFoundError", "PermissionError")
    assert r["sentinel_found_at"] == []
    # write: nothing outside /work, the host unchanged, /work itself writable
    assert r["written_outside_work"] == []
    assert sorted(p.name for p in workdir.parent.iterdir()) == before
    assert list((workdir.parent / "outside").iterdir()) == []
    assert (workdir / "inside.txt").read_text() == "ok"
    # processes: only docker-init and the workload
    procs = r["processes"]
    assert isinstance(procs, list) and 1 <= len(procs) <= 3, procs
    assert all(
        p.rsplit("/", 1)[-1] in ("docker-init", "python", "python3") for p in procs
    )
    # identity and privileges
    assert r["uid"] != 0 and r["gid"] != 0
    assert r["cap_eff"] == "0000000000000000"
    # the bounding set too: nothing a set-uid binary could ever regain (cap_drop=ALL;
    # CapEff alone is 0 for any non-root user and would not catch a lost cap_drop)
    assert r["cap_bnd"] == "0000000000000000"
    assert r["no_new_privs"] == "1"
    # network really off
    assert r["net_ifaces"] == ["lo"]
    assert r["net"] != "connected"


def test_network_on_when_asked_has_an_interface(target: Target, workdir: Path) -> None:
    code = "import os; print(sorted(os.listdir('/sys/class/net')))"
    result, capture = _run(target, target.spec(workdir, code, network=True))
    assert result.ok
    assert capture.data(Stream.STDOUT).decode().strip() != "['lo']"


FORK_BOMB = textwrap.dedent(
    """
    import os, time
    made = 0
    for _ in range(500):
        try:
            if os.fork() == 0:
                time.sleep(60)
                os._exit(0)
            made += 1
        except OSError:
            break
    print(made, flush=True)
    """
)


def test_the_pids_limit_contains_a_fork_bomb(target: Target, workdir: Path) -> None:
    started = time.monotonic()
    result, capture = _run(target, target.spec(workdir, FORK_BOMB, pids=32))
    assert result.failure_type in (None, FailureType.USER_ERROR)  # typed, not a hang
    made = int(capture.data(Stream.STDOUT).split()[0])
    assert made < 32, made
    assert time.monotonic() - started < 30


def test_the_memory_limit_ends_the_run_as_oom(target: Target, workdir: Path) -> None:
    code = "chunks = []\nwhile True:\n    chunks.append(bytearray(8 << 20))"
    result, _ = _run(target, target.spec(workdir, code, memory_mb=64))
    assert result.oom_killed
    assert result.failure_type is FailureType.OOM


def test_a_duplicate_start_of_one_attempt_is_a_typed_conflict(
    target: Target, workdir: Path
) -> None:
    spec = target.spec(
        workdir, "import time; print('READY', flush=True); time.sleep(30)"
    )

    async def scenario() -> RunResult:
        capture = Capture()
        first = asyncio.create_task(target.runtime.run(spec, capture))
        await asyncio.wait_for(capture.seen.wait(), 30)
        second = await target.runtime.run(spec, Capture())
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        return second

    second = asyncio.run(scenario())
    assert (second.failure_type, second.reason) == (
        FailureType.CONFLICT,
        "duplicate_run",
    )


def test_a_missing_image_that_cannot_be_pulled_is_env_error(
    target: Target, workdir: Path
) -> None:
    image = "meshforge-g3-does-not-exist@sha256:" + "0" * 64
    result, _ = _run(target, target.spec(workdir, "print(1)", image=image))
    assert result.failure_type is FailureType.ENV_ERROR
    assert result.reason == "image_pull_failed"


# ------------------------------------------------------------------- GPU
def _gpu_uuid(index: int) -> str | None:
    if shutil.which("nvidia-smi") is None:
        return None
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    ).stdout
    for line in out.splitlines():
        idx, _, uuid = line.partition(",")
        if idx.strip() == str(index):
            return uuid.strip()
    return None


@pytest.fixture
def gpu_target(target: Target) -> Target:
    if _gpu_uuid(0) is None:
        pytest.skip("no NVIDIA GPU (nvidia-smi) on this host")
    if not _image_local(CUDA_BASE_IMAGE):
        pytest.skip(f"{CUDA_BASE_IMAGE} is not local: docker pull it first")
    return target


GPU_ARGV = (
    "sh",
    "-c",
    "ls /dev | grep -c '^nvidia[0-9]' ; nvidia-smi -L || true;"
    ' echo "NVD=$NVIDIA_VISIBLE_DEVICES"',
)


@pytest.mark.gpu
def test_the_requested_gpu_is_visible_by_its_nvml_index(
    gpu_target: Target, workdir: Path
) -> None:
    spec = gpu_target.spec(
        workdir, "", argv=GPU_ARGV, image=CUDA_BASE_IMAGE, gpu_indices=(0,)
    )
    result, capture = _run(gpu_target, spec)
    assert result.ok, capture.data(Stream.STDERR)
    out = capture.data(Stream.STDOUT).decode()
    assert out.splitlines()[0].strip() == "1"  # exactly one GPU device node
    uuid = _gpu_uuid(0)
    assert uuid is not None and uuid in out


@pytest.mark.gpu
def test_no_gpu_is_visible_when_none_was_requested(
    gpu_target: Target, workdir: Path
) -> None:
    """The CUDA image sets NVIDIA_VISIBLE_DEVICES=all; it must still get nothing."""
    spec = gpu_target.spec(workdir, "", argv=GPU_ARGV, image=CUDA_BASE_IMAGE)
    result, capture = _run(gpu_target, spec)
    assert result.ok
    out = capture.data(Stream.STDOUT).decode()
    assert out.splitlines()[0].strip() == "0"
    assert "GPU 0" not in out
    # Hidden explicitly, so a daemon whose *default* runtime is nvidia cannot hand
    # the image's NVIDIA_VISIBLE_DEVICES=all to it (not exercised on runc hosts).
    assert "NVD=void" in out


# ------------------------------------------------------------ orphan sweep
KILLED_AGENT = textwrap.dedent(
    """
    import asyncio, sys
    from pathlib import Path
    from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
    from meshforge.agent.execution.runtime import RunLimits, RunSpec
    from meshforge.common.ids import new_attempt_id

    async def out(stream, data):
        print("OUTPUT", flush=True)

    async def main():
        rt = DockerRuntime(DockerConfig(agent_label=sys.argv[1]))
        spec = RunSpec(attempt_id=new_attempt_id(),
                       argv=("python", "-c", "import time; print(1, flush=True); time.sleep(600)"),
                       workdir=Path(sys.argv[2]), env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
                       limits=RunLimits(memory_bytes=64 << 20, pids=32, timeout_s=600),
                       image=sys.argv[3])
        await rt.run(spec, out)

    asyncio.run(main())
    """
)


def test_containers_of_a_killed_agent_are_swept_at_the_next_start(
    target: Target, workdir: Path
) -> None:
    other = new_label()  # another agent on this machine: must not be touched
    client = open_client(10)
    foreign = client.containers.create(
        PYTHON_CPU_IMAGE,
        command=["true"],
        labels={LABEL_MANAGED: "1", LABEL_AGENT: other, LABEL_ROLE: "run"},
    )
    try:
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                KILLED_AGENT,
                target.label,
                str(workdir),
                PYTHON_CPU_IMAGE,
            ],
            stdout=subprocess.PIPE,
            text=True,
        ) as agent:
            assert agent.stdout is not None
            assert agent.stdout.readline().strip() == "OUTPUT"
            agent.kill()  # SIGKILL: no cleanup code runs
            agent.wait(10)
        left = containers_with_label(target.label)
        assert len(left) == 1 and left[0].labels[LABEL_ATTEMPT].startswith("att_")
        removed = asyncio.run(target.runtime.sweep_orphans())  # type: ignore[attr-defined]
        assert removed == 1
        assert containers_with_label(target.label) == []
        assert len(containers_with_label(other)) == 1
    finally:
        foreign.remove(force=True)
        for c in containers_with_label(target.label):
            c.remove(force=True)
        client.close()
