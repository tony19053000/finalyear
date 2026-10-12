"""V0.7 G6 item 0 (D161 tester follow-up): a REAL CPU share for the unsandboxed
Linux runtimes.

The agent side runs in a child process started as a transient systemd **user**
service with ``Delegate=yes`` + ``DelegateSubgroup=agent`` - exactly the
deployed unit's setup - so the kernel enforces ``cpu.max``:

* a busy loop capped at 0.2 CPUs gets about 0.2 s of CPU per wall second
  (asserted < 0.45; the uncapped control gets > 0.6);
* the job ran in its own ``mf-<attempt>`` cgroup, removed after the run;
* a descendant that escaped the process group with ``setsid`` is killed by
  ``cgroup.kill`` when the run ends;
* a job cgroup that cannot be created refuses the run ``config_error`` /
  ``limits_not_applied`` (no silent uncapped run);
* a leftover ``mf-*`` cgroup from a crashed agent is swept.

Skipped (with the reason) where systemd ``--user`` cannot delegate (no user
manager, systemd < 254, CI containers).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.component

if not sys.platform.startswith("linux"):
    pytest.skip("cgroup v2 is Linux only", allow_module_level=True)

CHILD = r"""
import asyncio, json, os, sys
from pathlib import Path
from meshforge.agent.execution.cpu_cgroup import CpuCgroups, probe_cpu_cgroups
from meshforge.agent.execution.runtime import RunLimits, RunSpec
from meshforge.agent.execution.subprocess_runtime import SubprocessRuntime
from meshforge.common.ids import new_attempt_id

WORK = r'''
import os, subprocess, sys, time
with open("/proc/self/cgroup") as fh:
    print("CG", fh.read().strip().rsplit("/", 1)[-1], flush=True)
if sys.argv[1] == "escape":
    child = subprocess.Popen(["sleep", "60"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("ESCAPED", child.pid, flush=True)
start_wall, start_cpu = time.monotonic(), time.process_time()
while time.monotonic() - start_wall < 1.5:
    pass
print("RATIO", (time.process_time() - start_cpu) / (time.monotonic() - start_wall))
'''

async def run(cgroups, mode, cpus, work):
    out = []

    async def sink(_stream, data):
        out.append(data.decode())

    attempt = str(new_attempt_id())
    spec = RunSpec(
        attempt_id=attempt,
        argv=(sys.executable, "-c", WORK, mode),
        workdir=work,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        limits=RunLimits(memory_bytes=256 << 20, pids=64, timeout_s=30, cpus=cpus),
    )
    runtime = SubprocessRuntime(lower_priority=False, cpu_cgroups=cgroups)
    result = await runtime.run(spec, sink)
    text = "".join(out)
    fields = dict(
        line.split(" ", 1) for line in text.splitlines() if " " in line
    )
    gone = not (cgroups.path_for(attempt)).exists() if cgroups else None
    return {
        "failure": result.failure_type.value if result.failure_type else None,
        "reason": result.reason,
        "ratio": float(fields["RATIO"]) if "RATIO" in fields else None,
        "cg": fields.get("CG"),
        "escaped": int(fields["ESCAPED"]) if "ESCAPED" in fields else None,
        "attempt": attempt.lower(),
        "removed": gone,
    }

async def main():
    found = probe_cpu_cgroups()
    if found.parent is None:
        print(json.dumps({"unavailable": found.reason}))
        return
    work = Path(sys.argv[1])
    cgroups = CpuCgroups(found.parent)
    stale = found.parent / "mf-stale-from-crash"
    stale.mkdir()
    report = {"swept": cgroups.sweep(), "stale_gone": not stale.exists()}
    report["capped"] = await run(cgroups, "plain", 0.2, work)
    report["uncapped"] = await run(cgroups, "plain", None, work)
    report["escape"] = await run(cgroups, "escape", 0.5, work)
    esc = report["escape"]["escaped"]
    try:
        os.kill(esc, 0)
        with open(f"/proc/{esc}/stat") as fh:
            alive = fh.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (ProcessLookupError, FileNotFoundError, TypeError):
        alive = False
    report["escape_alive"] = alive

    class Broken(CpuCgroups):
        def create(self, attempt_id, cpus):
            raise OSError(13, "denied")

    report["broken"] = await run(Broken(found.parent), "plain", 0.2, work)
    print(json.dumps(report))

asyncio.run(main())
"""


def _systemd_run() -> str | None:
    exe = shutil.which("systemd-run")
    if exe is None:
        return None
    probe = subprocess.run(
        [
            exe,
            "--user",
            "--wait",
            "--pipe",
            "--quiet",
            "-p",
            "Delegate=yes",
            "-p",
            "DelegateSubgroup=agent",
            "--",
            "true",
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return exe if probe.returncode == 0 else None


def test_a_delegated_agent_holds_jobs_to_their_cpu_share(tmp_path: Path) -> None:
    exe = _systemd_run()
    if exe is None:
        pytest.skip("systemd --user cannot start a delegated service here")
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    work = tmp_path / "work"
    work.mkdir()
    env_args = []
    for key in ("PATH",):
        env_args += ["--setenv", f"{key}={os.environ.get(key, '')}"]
    proc = subprocess.run(
        [
            exe,
            "--user",
            "--wait",
            "--pipe",
            "--quiet",
            "-p",
            "Delegate=yes",
            "-p",
            "DelegateSubgroup=agent",
            f"--working-directory={tmp_path}",
            *env_args,
            "--",
            sys.executable,
            str(script),
            str(work),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    if "unavailable" in report:
        pytest.skip(f"cgroup delegation unavailable: {report['unavailable']}")
    assert report["swept"] >= 1 and report["stale_gone"]
    capped, uncapped = report["capped"], report["uncapped"]
    assert capped["failure"] is None, capped
    assert capped["cg"] == f"mf-{capped['attempt']}"  # ran in its own cgroup
    assert capped["removed"] is True
    assert capped["ratio"] < 0.45, capped  # 0.2 CPUs (+ scheduler slack)
    assert uncapped["ratio"] > 0.6, uncapped  # the control: no cap, no cgroup
    assert uncapped["cg"] == "agent"
    assert report["escape"]["removed"] is True
    assert report["escape_alive"] is False  # cgroup.kill got the setsid child
    broken = report["broken"]
    assert broken["failure"] == "config_error"
    assert broken["reason"] == "limits_not_applied"
    assert broken["ratio"] is None  # the workload never ran
