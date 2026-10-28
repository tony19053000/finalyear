"""V1.0 G1 (roadmap 1.0.2, D409-D411) acceptance: the soak kit's compressed dry run.

Tier ``soak`` (``tests/cluster/soak/``; opt-in: ``--run-soak``). Linux only
(the harness's process control and the job-tree kill; the lab tools themselves
also run on Windows). The chaos kinds the dry run does, and the ones only the
lab does (``disk_fill``, ``restart_postgres``, ``revoke_rejoin``,
``upgrade_reinstall``), are listed in ``tests/_support/mf_soak_dryrun.py``.

The brief's acceptance: *a 1 h compressed run on a local 3-agent fabric with
6 chaos actions ends with lost = 0 and a report; resumes after the sampler is
killed.* The 72 h lab run and this one are the same tools with a different
plan (``lab/_soak/plan.py`` "Compressed runs"): ``soak.py plan`` writes the
plan + seeded chaos schedule, then the sampler, the transition recorder, the
load generator and the chaos driver run as subprocesses, the harness does the
chaos actions on the fabric (``mf_soak_dryrun``), the sampler is SIGKILLed
once and restarted, ``soak_load.py collect`` takes the final view and
``soak.py report`` applies the D410 rules.

Knobs: ``MESHFORGE_SOAK_DRYRUN_HOURS`` (default 1.0 - the acceptance run;
0.25 for a quick check), ``MESHFORGE_SOAK_DRYRUN_SEED`` (default 7),
``MESHFORGE_SOAK_DRYRUN_REPORT`` (a folder: the whole run folder - plan,
samples, ledger, chaos log, verdict, report - is copied there).

**What is asserted** (whatever the run length): D410 rule 1 (``lost_jobs``),
rule 2 (``crashes``: every restart the sampler saw is one the harness
caused), rule 7 (``heartbeat``: every OFFLINE explained), ``chaos`` (>= 1
applied action per 45 min; >= 6 applied in a 1 h run) and ``coverage`` all
PASS; the sampler's file holds a ``sampler_resumed`` event; the report
exists. Rules 3-6 (memory, FDs/threads, DB growth, disk) are **computed and
printed but not asserted**: their bounds are for 72 h with 6 h quiet windows;
a 6 min quiet window right after start-up measures warm-up, not leaks.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric, Timing
from mf_soak_dryrun import (
    CONTROLLER_LABEL,
    HARNESS_KINDS,
    ChaosHarness,
    LabTool,
    agent_label,
    lab_tool,
    targets_doc,
    tool_env,
)

HOURS: Final = float(os.environ.get("MESHFORGE_SOAK_DRYRUN_HOURS", "1.0"))
SEED: Final = int(os.environ.get("MESHFORGE_SOAK_DRYRUN_SEED", "7"))

pytestmark = [pytest.mark.soak, pytest.mark.timeout(int(HOURS * 3600 + 2400))]

if not sys.platform.startswith("linux"):
    pytest.skip("the soak dry run is Linux-only", allow_module_level=True)

AGENTS: Final = 3
TIMING: Final = Timing(
    name="soak-dryrun",
    # SUSPECT ~1.6 s, OFFLINE ~4 s after the last heartbeat: a fault is seen
    # quickly, but the fabric is not as twitchy as the chaos suite's.
    heartbeat_interval_s=1.0,
    miss_allowance_s=0.5,
    lease_multiplier=4,
    controller_hello_timeout_s=3.0,
    shutdown_drain_s=2.0,
    agent_reconnect_base_s=0.2,
    agent_reconnect_cap_s=2.0,
    agent_hello_timeout_s=3.0,
)
EXEC: Final = {
    "enabled": True,
    "allow_unsandboxed": True,
    "grace_s": 1.0,
    "max_cpu_jobs": 4,
    "max_concurrent_attempts": 4,
    "terminal_resend_s": 1.0,
}
CAS_QUOTA: Final = 32 * 1024 * 1024
CAS_CAP_MB: Final = 64
"""The watched cap of each agent's blob cache (rule 6): the GC quota plus one
GC interval of intake (a pass every 30 s evicts down to the quota)."""
JOB_GAP_S: Final = 20.0
DURATION_SCALE: Final = 0.1
SAMPLE_S: Final = 5.0
ASSERTED: Final = ("lost_jobs", "crashes", "heartbeat", "chaos", "coverage")


def _windows(hours: float) -> dict[str, Any]:
    quiet_h = round(hours * 0.1, 4)
    low, high = 3.0 * hours, 5.0 * hours
    return {
        "hours": hours,
        "quiet_h": quiet_h,
        "tail_h": round(max(0.05, hours * 0.1), 4),
        "warmup_min": round(quiet_h * 60 * 0.25, 2),
        "chaos_gap_min": f"{low:g}-{high:g}",
    }


def test_soak_dry_run(
    make_fabric: Callable[..., Fabric],
    pg_settings: Any,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    import certifi

    fabric = make_fabric(
        agents=AGENTS,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1, "reconnect_grace_s": 8.0},
        controller_extra={"split": {"visibility_timeout_s": 20.0, "wait_retry_s": 0.3}},
        agent_extra={"gc": {"cas_bytes": CAS_QUOTA, "interval_s": 30.0, "min_age_s": 30.0}},
        agent_env={
            "DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock",
            "SSL_CERT_FILE": certifi.where(),
        },
    )  # fmt: skip
    run_dir = tmp_path / "run"
    scratch = tmp_path / "scratch"
    logs = tmp_path / "tool-logs"
    scratch.mkdir()
    harness = ChaosHarness.start(fabric, tmp_path / "control")
    env = tool_env(harness, scratch)
    targets = scratch / "targets.json"
    targets.write_text(json.dumps(targets_doc(fabric)), "utf-8")
    kinds = list(HARNESS_KINDS)
    w = _windows(HOURS)
    planned = lab_tool(
        "plan", "soak.py",
        ["plan", "--out", str(run_dir), "--targets", str(targets), "--seed", str(SEED),
         "--start-in-min", "0.5", "--hours", str(w["hours"]), "--quiet-h", str(w["quiet_h"]),
         "--tail-h", str(w["tail_h"]), "--warmup-min", str(w["warmup_min"]),
         "--job-gap-s", str(JOB_GAP_S), "--chaos-gap-min", w["chaos_gap_min"],
         "--duration-scale", str(DURATION_SCALE), "--kinds", ",".join(kinds)],
        env, logs,
    ).start()  # fmt: skip
    assert planned.wait(120) == 0, planned.output()
    plan = json.loads((run_dir / "plan.json").read_text("utf-8"))
    schedule = json.loads((run_dir / "chaos-schedule.json").read_text("utf-8"))
    end_at = (
        datetime.fromisoformat(plan["t0"]).timestamp()
        + (plan["hours"] + plan["tail_h"]) * 3600.0
    )
    n_actions = len(schedule["actions"])
    print(f"\nsoak dry run seed={SEED} {HOURS} h, {n_actions} chaos actions")  # noqa: T201

    password = scratch / "db.password"
    password.write_text(pg_settings.password.get_secret_value(), "utf-8")
    password.chmod(0o600)
    controller_toml = fabric.workdir / "controller.toml"
    sampler_args = [
        "run", "--dir", str(run_dir), "--label", CONTROLLER_LABEL,
        "--interval", str(SAMPLE_S), "--dirs-every", "15", "--db-every", "15",
        "--db-url", f"postgresql://{pg_settings.user}@{pg_settings.host}:{pg_settings.port}/"
                    f"{pg_settings.name}",
        "--db-password-file", str(password),
        "--target", f"controller=controller@{CONTROLLER_LABEL}:{controller_toml}",
        *[a for n in fabric.nodes for a in (
            "--target", f"agent{n.index}=agent@{agent_label(n.index)}:{n.config}",
            "--dir-watch", f"cas{n.index}={n.data_dir / 'cas'}@{CAS_CAP_MB}",
        )],
    ]  # fmt: skip
    tools: list[LabTool] = []
    try:
        sampler = lab_tool("sampler", "soak.py", sampler_args, env, logs).start()
        recorder = lab_tool(
            "recorder", "fabric_check.py",
            ["--reports-dir", str(scratch / "reports"), "soak",
             "--hours", f"{(end_at - time.time()) / 3600.0:.4f}", "--link", "wifi",
             "--poll", "1", "--checkpoint", str(run_dir / "fabric-soak.checkpoint.json"),
             "--checkpoint-every", "30"],
            env, logs,
        ).start()  # fmt: skip
        load = lab_tool(
            "load", "soak_load.py", ["run", "--dir", str(run_dir)], env, logs
        )
        chaos = lab_tool(
            "chaos", "chaos.py",
            ["run", "--dir", str(run_dir), "--manual", "skip",
             "--control-dir", str(harness.control_dir), "--cancel-wait-min", "1"],
            env, logs,
        )  # fmt: skip
        tools += [sampler, recorder, load.start(), chaos.start()]
        kill_sampler_at = time.monotonic() + HOURS * 3600.0 * 0.45
        sampler_killed = False
        while load.running or chaos.running:
            harness.poll()
            if (
                not sampler_killed
                and time.monotonic() >= kill_sampler_at
                and not harness.busy()
            ):
                sampler.kill()  # SIGKILL: the resume path
                sampler_killed = True
                time.sleep(SAMPLE_S)
                sampler.start()
            for tool in (sampler, recorder):
                assert tool.running, (
                    f"{tool.name} exited early:\n{tool.output()[-4000:]}"
                )
            time.sleep(0.2)
        harness.restore_due(all_now=True)
        assert load.returncode == 0, load.output()[-4000:]
        assert chaos.returncode == 0, chaos.output()[-4000:]
        fabric.wait_all_connected(within=60)

        collect = lab_tool(
            "collect", "soak_load.py",
            ["collect", "--dir", str(run_dir), "--wait-min", "20", "--verify-sample", "60"],
            env, logs,
        ).start()  # fmt: skip
        tools.append(collect)
        assert collect.wait(1500) == 0, collect.output()[-4000:]
        left = max(0.0, end_at - time.time())
        assert sampler.wait(left + 120) == 0, sampler.output()[-4000:]
        recorder.wait(left + 180)  # 3 = OFFLINE events seen (expected under chaos)
        assert recorder.returncode in (0, 3), recorder.output()[-4000:]

        report_md = run_dir / "report.md"
        report = lab_tool(
            "report",
            "soak.py",
            ["report", str(run_dir), "--md", str(report_md)],
            env,
            logs,
        ).start()
        report_code = report.wait(300)
    finally:
        for tool in tools:
            tool.stop()
        harness.restore_due(all_now=True)
        out = os.environ.get("MESHFORGE_SOAK_DRYRUN_REPORT")
        if out and run_dir.exists():
            dest = Path(out) / f"soak-dryrun-{SEED}-{datetime.now(UTC):%Y%m%dT%H%M%S}"
            shutil.copytree(run_dir, dest)
            shutil.copytree(logs, dest / "tool-logs", dirs_exist_ok=True)
            (dest / "harness-log.json").write_text(json.dumps(harness.log, indent=1))

    print(report.output())  # noqa: T201 - the verdict table
    verdict = json.loads((run_dir / "verdict.json").read_text("utf-8"))
    rules = {r["id"]: r for r in verdict["rules"]}
    record_property("soak_verdict", json.dumps(
        {name: r["verdict"] for name, r in rules.items()}, sort_keys=True))  # fmt: skip
    assert report_md.is_file() and report_code in (0, 3, 4)
    samples = (run_dir / f"samples.{CONTROLLER_LABEL}.jsonl").read_text("utf-8")
    assert '"sampler_resumed"' in samples, "the sampler was killed but never resumed"
    applied = [a for a in harness.log if a["outcome"] == "done"]
    chaos_log = (run_dir / "chaos-log.jsonl").read_text("utf-8").splitlines()
    api_done = [
        r for r in map(json.loads, chaos_log)
        if r.get("type") == "end" and r.get("executor") == "api" and r.get("outcome") == "done"
    ]  # fmt: skip
    if HOURS >= 1.0:
        assert len(applied) + len(api_done) >= 6, harness.log
    failed = [a for a in harness.log if a["outcome"] == "failed"]
    assert not failed, failed
    bad = {name: rules[name] for name in ASSERTED if rules[name]["verdict"] != "PASS"}
    assert not bad, json.dumps(bad, indent=1)[:6000]
    lost = rules["lost_jobs"]["details"]
    assert lost["jobs"] > 0 and not lost["lost"], lost
