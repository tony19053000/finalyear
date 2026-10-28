# ruff: noqa: E501  (long xfail reasons / parametrize ids are clearer on one line)
"""V0.9 G2 tester round 1 (split from ``..._process.py``, G3 item 0.2): the generated
entrypoint run offline - an audit hook proves no socket, no download, no
subprocess, no write outside ``results/``, an import blocker proves the job
folder does not need MeshForge installed - and the stdlib families in a real
``--network none`` container.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from mf_bench_procs import (  # tests/_support, shared by the split files
    CPU_NAMES,
    TINY,
    TORCH_NAMES,
    _doc,
    _env,
    _tree,
    needs_torch,
    skip_without_numpy,
)

from meshforge.bench.workloads import (
    build_job,
)

pytestmark = [pytest.mark.component, pytest.mark.timeout(300)]


# ---------------------------------------------- the entrypoint, offline audit

_SITECUSTOMIZE = """
import atexit, json, os, sys
_fd = os.open(os.environ["MF_AUDIT_LOG"], os.O_WRONLY | os.O_CREAT | os.O_APPEND)
_cwd = os.path.realpath(os.getcwd())
def _log(kind, detail):
    os.write(_fd, (json.dumps([kind, detail]) + "\\n").encode())
def _hook(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                 "socket.bind", "socket.sendto", "socket.__new__"):
        _log("net", [event, repr(args)[:120]])
    elif event.startswith("urllib.") or event.startswith("http.client."):
        _log("net", [event, repr(args)[:120]])
    elif event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.fork", "os.forkpty"):
        _log("proc", [event, repr(args)[:160]])
    elif event == "open":
        path, mode = args[0], args[1]
        if mode and any(c in str(mode) for c in "wax+"):
            _log("write", str(path))
    elif event == "os.remove" or event == "os.rename" or event == "os.mkdir":
        _log("fsop", [event, repr(args)[:120]])
sys.addaudithook(_hook)

class _Block:
    def find_spec(self, name, path=None, target=None):
        if name == "meshforge" or name.startswith("meshforge."):
            raise ImportError("MeshForge is not installed in a job environment: " + name)
        return None
sys.meta_path.insert(0, _Block())
"""


def _audited(
    tmp: Path, argv: list[str], cwd: Path, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], list[list[Any]]]:
    hooks = tmp / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    log = tmp / "audit.log"
    log.write_text("")
    env = _env(
        MF_AUDIT_LOG=str(log),
        PYTHONPATH=str(hooks),
        PYTHONDONTWRITEBYTECODE="1",
        HTTP_PROXY="http://127.0.0.1:9",
        HTTPS_PROXY="http://127.0.0.1:9",
        **(extra_env or {}),
    )
    done = subprocess.run(
        [sys.executable, *argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=250,
        check=False,
    )
    events = [json.loads(line) for line in log.read_text().splitlines() if line]
    return done, events


def test_the_audit_harness_itself_sees_network_writes_and_blocks_meshforge(
    tmp_path: Path,
) -> None:
    """Negative control: without this the offline tests below could pass vacuously."""
    work = tmp_path / "w"
    work.mkdir()
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 9), timeout=0.2)\n"
        "except OSError:\n"
        "    pass\n"
        "open('leak.txt', 'w').write('x')\n"
        "try:\n"
        "    import meshforge\n"
        "except ImportError:\n"
        "    print('blocked')\n"
    )
    done, events = _audited(tmp_path, ["-c", code], work)
    assert done.returncode == 0, done.stderr
    assert "blocked" in done.stdout
    kinds = {e[0] for e in events}
    assert {"net", "write"} <= kinds, events


@pytest.mark.parametrize("name", CPU_NAMES)
def test_cpu_job_entrypoint_runs_offline_without_meshforge_and_writes_only_results(
    name: str, tmp_path: Path
) -> None:
    skip_without_numpy(name)
    built = build_job(name, {}, 3, tmp_path / "job")
    (built.path / "workload_params.json").write_text(
        json.dumps({"family": name, "params": TINY[name], "seed": 3}), encoding="utf-8"
    )
    before = _tree(built.path)
    done, events = _audited(tmp_path, ["run_workload.py"], built.path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not [e for e in events if e[0] == "net"], events
    assert not [e for e in events if e[0] == "proc"], events
    writes = {Path(e[1]).as_posix() for e in events if e[0] == "write"}
    allowed = {(built.path / "results" / "workload.json").resolve().as_posix()}
    assert {Path(w).resolve().as_posix() for w in writes} <= allowed | {"/dev/null"}, (
        writes
    )
    after = _tree(built.path)
    assert set(after) - set(before) == {"results/workload.json"}
    assert {k: v for k, v in after.items() if k != "results/workload.json"} == before
    doc = json.loads(after["results/workload.json"])
    assert doc["status"] == "ok" and doc["verified"] is True and doc["compute_ns"] > 0


@needs_torch
@pytest.mark.parametrize("name", TORCH_NAMES)
def test_torch_kernels_in_the_job_folder_run_offline_on_cpu(
    name: str, tmp_path: Path
) -> None:
    built = build_job(name, {}, 3, tmp_path / "job")
    code = (
        "import json, sys\n"
        "from bench_kernels.runner import execute\n"
        f"d = execute({name!r}, {TINY[name]!r}, 3, device='cpu')\n"
        "print(json.dumps([d['status'], d['verified'], d['compute_ns']]))\n"
    )
    done, events = _audited(tmp_path, ["-c", code], built.path)
    assert done.returncode == 0, done.stdout + done.stderr
    status, verified, compute_ns = json.loads(done.stdout.strip().splitlines()[-1])
    assert (status, verified) == ("ok", True) and compute_ns > 0
    assert not [e for e in events if e[0] == "net"], events


# ------------------------------------------- network none, in a real container

STDLIB_ONLY = ["cpu.zlib", "cpu.sha256", "cpu.primes"]


@pytest.mark.docker
@pytest.mark.parametrize("name", STDLIB_ONLY)
def test_stdlib_families_run_with_the_network_really_off_in_a_container(
    name: str, tmp_path: Path
) -> None:
    """``limits.network: none`` is only honest if the job truly needs none. The
    smallest job of each stdlib-only family runs in ``python:3.12-slim`` with
    ``--network none``, a non-root user and no capabilities; a control proves the
    container cannot reach anything."""
    image = "python:3.12-slim"
    probe = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    )
    if probe.returncode != 0:
        pytest.skip(f"{image} is not present locally (the test never pulls)")
    built = build_job(name, {}, 5, tmp_path / "job")
    (built.path / "workload_params.json").write_text(
        json.dumps({"family": name, "params": TINY[name], "seed": 5}), encoding="utf-8"
    )
    (built.path / "results").mkdir()
    (built.path / "results").chmod(0o777)
    base = [
        "docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{built.path}:/work", "-w", "/work", "-e", "PYTHONDONTWRITEBYTECODE=1",
        image,
    ]  # fmt: skip
    control = subprocess.run(
        [*base, "python", "-c",
         "import socket,sys\n"
         "try:\n"
         "    socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
         "except OSError:\n"
         "    sys.exit(0)\n"
         "sys.exit(1)\n"],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert control.returncode == 0, "the container had a network: the control failed"
    done = subprocess.run(
        [*base, "python", "run_workload.py"],
        capture_output=True, text=True, timeout=200, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    doc = _doc(built.path)
    assert doc["status"] == "ok" and doc["verified"] is True
    assert doc["libraries"]["numpy"] is None  # the stdlib families need no packages
