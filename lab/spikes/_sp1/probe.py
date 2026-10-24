"""SP1 probe: runs *as the service's child*, i.e. as ``NT SERVICE\\MeshForgeAgent``
in session 0, and reports what the agent would see from there (D388: checks are
made by the service itself, not by the installer's admin process).

Started by the service host instead of the agent (``winservice host ... -- <this>``):

1. who am I (``whoami /user``), which session, which profile;
2. ``meshforge-agent hardware show --json`` - the agent's own GPU / Docker / torch
   probes (on a CI runner: no NVIDIA GPU; Docker may exist but be unreachable for
   the service account - the report must say so, not crash);
3. a ``uv venv`` environment build with the agent's pinned ``uv``;
4. a job-like tree started with the agent's own :func:`meshforge.agent.process.spawn`
   (a nested job object inside the service's job) whose child starts two
   grandchildren - then it writes ``ready`` and sleeps, **ignoring stdin**, so only
   the host's job-object kill can end the tree.

Writes one JSON file (``--out``) and never exits on its own.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_GRANDCHILDREN = """
import subprocess, sys, time
kids = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
        for _ in range(2)]
print(" ".join(str(k.pid) for k in kids), flush=True)
time.sleep(3600)
"""


def _run(
    argv: list[str], timeout: float, env: dict[str, str] | None = None
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": r.returncode == 0,
        "exit_code": r.returncode,
        "duration_s": round(time.monotonic() - started, 2),
        "stdout": r.stdout.decode("utf-8", "replace")[-4000:],
        "stderr": r.stderr.decode("utf-8", "replace")[-2000:],
    }


def _session_id() -> int | None:
    if sys.platform == "win32":
        sid = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
        return int(sid.value) if ok else None
    else:  # both branches platform-checked: mypy reachability on either OS
        return None


def _hardware(config: str) -> dict[str, Any]:
    res = _run(
        [sys.executable, "-I", "-m", "meshforge.agent", "hardware", "show", "--json",
         "--config", config],
        timeout=240,
    )  # fmt: skip
    out: dict[str, Any] = {"command": {k: v for k, v in res.items() if k != "stdout"}}
    if res.get("ok"):
        try:
            payload = json.loads(res["stdout"].strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            out["parse_error"] = str(exc)
            return out
        out["capabilities"] = payload.get("capabilities")
        out["gpus"] = payload.get("gpus")
        out["degraded"] = payload.get("degraded")
        out["probes"] = payload.get("probes")
    return out


def _docker_direct() -> dict[str, Any]:
    docker = shutil.which("docker")
    info: dict[str, Any] = {"on_path": docker}
    if docker:
        info["version"] = _run(
            [docker, "version", "--format", "{{.Server.Version}}"], 30
        )
    return info


def _uv_build(cache: Path) -> dict[str, Any]:
    try:
        import uv  # the agent's pinned uv wheel (agent extra)

        uv_bin = uv.find_uv_bin()
    except (ImportError, FileNotFoundError) as exc:
        return {"ok": False, "error": f"uv not found: {exc}"}
    env = dict(os.environ)
    env["UV_CACHE_DIR"] = str(cache / "uv-cache")
    target = cache / "sp1-venv"
    shutil.rmtree(target, ignore_errors=True)
    res = _run([uv_bin, "venv", "--python", sys.executable, str(target)], 120, env)
    res["venv_python_exists"] = (target / "Scripts" / "python.exe").exists() or (
        target / "bin" / "python"
    ).exists()
    return res


async def _job_tree() -> dict[str, Any]:
    from meshforge.agent.process import spawn

    handle = await spawn(
        [sys.executable, "-c", _GRANDCHILDREN],
        env=dict(os.environ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert handle.proc.stdout is not None  # noqa: S101 - PIPE requested
    line = await asyncio.wait_for(handle.proc.stdout.readline(), 60)
    grand = [int(p) for p in line.decode().split()]
    return {
        "child_pid": handle.pid,
        "grandchild_pids": grand,
        "nested_job_object": handle.uses_job_object,
        "_handle": handle,
    }


def _write(path: Path, report: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), "utf-8")
    os.replace(tmp, path)


async def _main(out: Path, config: str, cache: Path) -> None:
    report: dict[str, Any] = {
        "ready": False,
        "pid": os.getpid(),
        "session_id": _session_id(),
        "whoami": _run(["whoami", "/user", "/fo", "csv", "/nh"], 30),
        "env": {
            k: os.environ.get(k) for k in ("USERNAME", "USERPROFILE", "LOCALAPPDATA")
        },
        "nvidia_smi": {"on_path": shutil.which("nvidia-smi")},
    }
    if report["nvidia_smi"]["on_path"]:
        report["nvidia_smi"]["run"] = _run(["nvidia-smi", "-L"], 30)
    report["docker"] = _docker_direct()
    _write(out, report)
    report["hardware_show"] = _hardware(config)
    report["uv_venv"] = _uv_build(cache)
    try:
        tree = await _job_tree()
    except Exception as exc:  # noqa: BLE001 - recorded, the spike reads it
        report["job_tree"] = {"error": f"{type(exc).__name__}: {exc}"}
        tree = {}
    _handle = tree.pop("_handle", None)  # keep the tree's handle alive
    report["job_tree"] = report.get("job_tree") or tree
    report["ready"] = True
    _write(out, report)
    # Never set: stdin is ignored, only the service's job-object kill ends us.
    await asyncio.Event().wait()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--cache", required=True)
    a = p.parse_args()
    asyncio.run(_main(Path(a.out), a.config, Path(a.cache)))


if __name__ == "__main__":
    main()
