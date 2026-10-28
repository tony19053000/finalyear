"""MeshForge v1.0 demonstration scenarios (roadmap 1.0.7, D420).

    python scripts/demo/demo.py SCENARIO [--dry-run] [--reset] [options]
    scripts/demo/01-local.sh [options]          (the same, one wrapper each)

Scenarios (shipped modes only - Mode 2 and Mode 4 are deferred by ADR-0012
and have no scenario, see scripts/demo/README.md):

==================  ==========================================================
``01-local``        the baseline: a job on this laptop's own agent
``02-mode1-offload`` the same job sent to another laptop; the same bytes back
``03-mode3-split``  a job split over the laptops; one laptop killed mid-way;
                    the job still succeeds with the verified output
``04-planner-explains`` three jobs, three answers: Local, Mode 1, Mode 3 -
                    and ``compute explain`` / the dashboard say why
``05-install-join`` a join code for a new laptop (the Windows install is the
                    recorded part); waits until it has joined
==================  ==========================================================

Every scenario first runs its **reset** (cancel its leftover jobs, revoke its
unused invites, bring a killed laptop back) and ends with ``DEMO <name>:
PASS`` or ``FAIL``; ``--reset`` runs only the reset. ``--dry-run`` is the
unattended rehearsal (CI against the cluster-harness fabric, or the lab the day
before): no pauses, smaller jobs, every expectation still checked on real jobs.

Exit codes: 0 PASS; 1 an expectation failed or a command failed; 2 usage
(e.g. ``03`` in a dry run without ``--kill-cmd``); 130 interrupted.
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_HERE: Final = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from demo_kit import (  # noqa: E402
    JOB_PREFIX,
    TERMINAL,
    Compute,
    DemoError,
    DemoState,
    Show,
    chosen,
    chosen_mode,
    expected_job_result,
    expected_split_lines,
    fetch_outputs,
    last_node,
    node_label,
    reset_fabric_objects,
    run_hook,
    wait_for,
    wait_job,
    write_job_project,
    write_split_project,
)

BUDGET_S: Final = 300.0
"""D420: every scenario runs in under 5 minutes."""


@dataclass(frozen=True)
class Profile:
    """Sizes: the live show (visible on a laptop) vs the dry run (fast)."""

    rounds: int
    busy_hold_s: float
    split_items: int
    split_chunk: int
    split_rounds: int
    join_wait_s: float


LIVE: Final = Profile(3_000_000, 30.0, 48, 4, 400_000, 600.0)
DRY: Final = Profile(200_000, 8.0, 36, 2, 500_000, 120.0)


@dataclass
class Ctx:
    show: Show
    compute: Compute
    state: DemoState
    work: Path
    args: argparse.Namespace
    profile: Profile

    def name(self, suffix: str) -> str:
        return f"{JOB_PREFIX}{self.args.scenario}-{suffix}"

    def dashboard(self, job_id: str) -> None:
        url = self.args.dashboard_url
        if url:
            self.show.say(
                f"dashboard: {url.rstrip('/')}/jobs/{job_id}  (Plan tab = the 'why')"
            )


# --------------------------------------------------------------- 01 local
def local_baseline(ctx: Ctx) -> str:
    """Run the demo job on this laptop's agent; returns ``result.json``."""
    show, compute = ctx.show, ctx.compute
    project = write_job_project(
        ctx.work / "job", name=ctx.name("local"), rounds=ctx.profile.rounds
    )
    show.step("Local: run the job on this laptop's own agent")
    show.say("--local pins the job to the agent on the laptop compute runs on.")
    job_id = compute.submit(project, "--local")
    ctx.state.add_job(job_id)
    job = wait_job(compute, job_id, within_s=BUDGET_S)
    show.expect(job["state"] == "succeeded", "the job succeeded", job["state"])
    local = job.get("submitter_node_id")
    show.expect(bool(local), "compute knows this laptop's node", local)
    show.expect(last_node(job) == local, "it ran on this laptop", last_node(job))
    out = fetch_outputs(compute, job_id, ctx.work / "out-local")
    text = (out / "results" / "result.json").read_text(encoding="utf-8")
    show.expect(
        text == expected_job_result(ctx.profile.rounds), "result.json is right", text
    )
    show.say(f"result: {text.strip()}")
    ctx.state.save()
    (ctx.work / "local-node").write_text(str(local), encoding="utf-8")
    return text


def scenario_local(ctx: Ctx) -> None:
    local_baseline(ctx)


# ------------------------------------------------------------- 02 mode 1
def scenario_offload(ctx: Ctx) -> None:
    show, compute = ctx.show, ctx.compute
    baseline = local_baseline(ctx)
    local = (ctx.work / "local-node").read_text(encoding="utf-8")
    nodes = compute.nodes()
    others = [
        n
        for n in nodes
        if n.get("node_id") != local
        and n.get("connected")
        and n.get("state") == "online"
    ]
    if ctx.args.target:
        target = ctx.args.target
    elif others:
        target = sorted(str(n["node_id"]) for n in others)[0]
    else:
        raise DemoError("no other laptop is online to offload to")
    show.step(f"Mode 1: send the same job to {node_label(nodes, target)}")
    show.pause()
    project = write_job_project(
        ctx.work / "job", name=ctx.name("offload"), rounds=ctx.profile.rounds
    )
    job_id = compute.submit(project, "--node", target)
    ctx.state.add_job(job_id)
    job = wait_job(compute, job_id, within_s=BUDGET_S)
    show.expect(
        job["state"] == "succeeded", "the offloaded job succeeded", job["state"]
    )
    show.expect(last_node(job) == target, "it ran on the other laptop", last_node(job))
    out = fetch_outputs(compute, job_id, ctx.work / "out-offload")
    text = (out / "results" / "result.json").read_text(encoding="utf-8")
    show.expect(
        text == baseline, "only the output came back, byte-identical to Local", text
    )
    compute.run("jobs", job_id)
    ctx.dashboard(job_id)


# ------------------------------------------------------------- 03 mode 3
def _lane_to_kill(progress: dict[str, Any], local: str | None) -> str | None:
    """A live lane on another laptop, preferring one that holds a chunk now."""
    live = [
        ln
        for ln in progress.get("lanes") or ()
        if ln.get("live") and ln.get("node_id") != local
    ]
    live.sort(key=lambda ln: (ln.get("current") is None, str(ln.get("node_id"))))
    return str(live[0]["node_id"]) if live else None


def scenario_split(ctx: Ctx) -> None:
    show, compute, prof = ctx.show, ctx.compute, ctx.profile
    project = write_split_project(
        ctx.work / "split",
        name=ctx.name("split"),
        items=prof.split_items,
        chunk=prof.split_chunk,
        rounds=prof.split_rounds,
    )
    show.step(
        f"Mode 3: {prof.split_items} items in pieces of {prof.split_chunk}; the planner decides"
    )
    job_id = compute.submit(project)
    ctx.state.add_job(job_id)
    decision = wait_for(
        "the planner's decision", lambda: compute.decision(job_id), within_s=120
    )
    row = chosen(decision) or {}
    show.expect(
        chosen_mode(decision) == "mode3", "the planner split the job", row.get("mode")
    )
    show.say(f"why: {decision.get('margin_sentence') or '-'}")
    lanes = list(row.get("node_set") or ())
    show.say(f"lanes on {len(lanes)} laptops")
    local = compute.job(job_id).get("submitter_node_id")

    def ready() -> str | None:
        progress = compute.split(job_id)
        if progress.get("job_state") in TERMINAL:
            raise DemoError(
                "the job ended before a laptop could be killed - use more items"
            )
        chunks = progress.get("chunks") or {}
        done, pending = int(chunks.get("done") or 0), int(chunks.get("pending") or 0)
        # mid-way: a piece is finished and enough are still to do that the
        # killed laptop's share must move to the others
        return _lane_to_kill(progress, local) if done >= 1 and pending >= 3 else None

    victim = wait_for("the first finished piece", ready, within_s=120, every_s=0.3)
    nodes = compute.nodes()
    show.step(f"Kill a laptop mid-way: {node_label(nodes, victim)}")
    if ctx.args.kill_cmd:
        run_hook(show, ctx.args.kill_cmd, node_id=victim)
    else:
        show.say(f"Now stop that laptop's agent (or pull its network cable): {victim}")
        show.pause("press Enter once it is down")
    ctx.state.killed.append(victim)
    ctx.state.save()
    job = wait_job(compute, job_id, within_s=BUDGET_S)
    show.step("Recovery: the other laptops took over its pieces")
    show.expect(
        job["state"] == "succeeded", "the split job still succeeded", job["state"]
    )
    progress = compute.split(job_id)
    for lane in progress.get("lanes") or ():
        show.say(
            f"lane {node_label(nodes, lane.get('node_id'))}: {lane.get('state')}, "
            f"{lane.get('chunks_done')} pieces"
        )
    killed_lanes = [
        ln for ln in progress.get("lanes") or () if ln.get("node_id") == victim
    ]
    show.expect(
        any(ln.get("state") != "done" for ln in killed_lanes),
        "the killed laptop's lane did not finish",
        [ln.get("state") for ln in killed_lanes],
    )
    out = fetch_outputs(compute, job_id, ctx.work / "out-split")
    got = (out / "hashes.txt").read_text(encoding="utf-8").splitlines()
    want = expected_split_lines(prof.split_items, prof.split_rounds)
    show.expect(
        got == want,
        f"all {prof.split_items} results present, in order, each correct",
        f"{len(got)} lines",
    )
    ctx.dashboard(job_id)
    revive(ctx)


def revive(ctx: Ctx) -> None:
    """Bring back the laptops a split scenario killed (``--revive-cmd``)."""
    if not ctx.state.killed:
        return
    for node_id in list(ctx.state.killed):
        if ctx.args.revive_cmd:
            ctx.show.step(f"Bring {node_id} back")
            run_hook(ctx.show, ctx.args.revive_cmd, node_id=node_id)
        else:
            ctx.show.say(f"start the agent on {node_id} again (no --revive-cmd given)")
        ctx.state.killed.remove(node_id)
    ctx.state.save()


# ---------------------------------------------------- 04 planner explains
def scenario_planner(ctx: Ctx) -> None:
    show, compute, prof = ctx.show, ctx.compute, ctx.profile
    show.step("Job A - needs no network: objective min_network")
    a_project = write_job_project(
        ctx.work / "job-a",
        name=ctx.name("a-local"),
        rounds=prof.rounds,
        hold_s=prof.busy_hold_s,
        objective="min_network",
    )
    job_a = compute.submit(a_project)
    ctx.state.add_job(job_a)
    dec_a = wait_for("job A's decision", lambda: compute.decision(job_a), within_s=120)
    show.expect(
        chosen_mode(dec_a) == "local",
        "the planner kept job A on this laptop",
        chosen_mode(dec_a),
    )
    show.say(f"why: {dec_a.get('margin_sentence') or 'Local moves no bytes'}")
    compute.run("explain", job_a)
    ctx.dashboard(job_a)

    def a_running() -> bool | None:
        state = compute.job(job_a).get("state")
        if state in TERMINAL:
            raise DemoError("job A ended before job B was planned - raise --busy-s")
        return True if state == "running" else None

    wait_for("job A to run", a_running, within_s=120, every_s=0.3)
    show.pause()
    show.step("Job B - while this laptop is busy with A: fastest finish (min_time)")
    b_project = write_job_project(
        ctx.work / "job-b", name=ctx.name("b-offload"), rounds=prof.rounds
    )
    job_b = compute.submit(b_project)
    ctx.state.add_job(job_b)
    dec_b = wait_for("job B's decision", lambda: compute.decision(job_b), within_s=120)
    show.expect(
        chosen_mode(dec_b) == "mode1",
        "the planner offloaded job B (Mode 1)",
        chosen_mode(dec_b),
    )
    local_rows = [c for c in dec_b.get("candidates") or () if c.get("mode") == "local"]
    show.expect(
        any(c.get("reject_code") == "slots_full" for c in local_rows),
        "because this laptop's slot is full (Local rejected: slots_full)",
        [c.get("reject_code") for c in local_rows],
    )
    still = compute.job(job_a).get("state")
    show.expect(
        still not in TERMINAL, "job A was still running when B was planned", still
    )
    compute.run("explain", job_b)
    ctx.dashboard(job_b)
    for job_id in (job_a, job_b):
        job = wait_job(compute, job_id, within_s=BUDGET_S)
        show.expect(job["state"] == "succeeded", f"{job_id} succeeded", job["state"])
    show.pause()
    show.step(f"Job C - {prof.split_items} independent items, every laptop free")
    c_project = write_split_project(
        ctx.work / "job-c",
        name=ctx.name("c-split"),
        items=prof.split_items,
        chunk=prof.split_chunk,
        rounds=prof.split_rounds,
    )
    job_c = compute.submit(c_project)
    ctx.state.add_job(job_c)
    dec_c = wait_for("job C's decision", lambda: compute.decision(job_c), within_s=120)
    show.expect(
        chosen_mode(dec_c) == "mode3",
        "the planner split job C (Mode 3)",
        chosen_mode(dec_c),
    )
    show.say(f"why: {dec_c.get('margin_sentence') or '-'}")
    compute.run("explain", job_c)
    ctx.dashboard(job_c)
    job = wait_job(compute, job_c, within_s=BUDGET_S)
    show.expect(job["state"] == "succeeded", "job C succeeded", job["state"])
    out = fetch_outputs(compute, job_c, ctx.work / "out-c")
    got = (out / "hashes.txt").read_text(encoding="utf-8").splitlines()
    show.expect(
        got == expected_split_lines(prof.split_items, prof.split_rounds),
        "job C's combined output is correct",
        f"{len(got)} lines",
    )
    show.step("Three jobs, three answers")
    for label, dec in (("A", dec_a), ("B", dec_b), ("C", dec_c)):
        show.say(f"job {label}: {chosen_mode(dec)}")


# ---------------------------------------------------------- 05 install/join
def scenario_join(ctx: Ctx) -> None:
    show, compute, prof = ctx.show, ctx.compute, ctx.profile
    before = {str(n["node_id"]) for n in compute.nodes()}
    show.step("Make a single-use join code for the new laptop")
    made = compute.json("fabric", "invite", "--ttl", "15m")
    invite_id = str(made.get("invite_id"))
    ctx.state.invites.append(invite_id)
    ctx.state.save()
    code = str(made.get("code", ""))
    if ctx.args.dry_run:
        show.say(f"invite {invite_id} made (code not printed in a dry run)")
    else:
        show.say(f"join code: {code}")
        show.say(
            "On the Windows laptop: run MeshForge-Agent-Setup-<version>.exe, Finish with"
        )
        show.say(
            "'Open MeshForge' ticked, paste this code on the page that opens (or its QR)."
        )
        show.say("Screens: docs/user/install-windows.md, docs/user/join-a-laptop.md.")
    listed = compute.json("fabric", "invites").get("invites", [])
    show.expect(
        any(r.get("id") == invite_id and r.get("state") == "pending" for r in listed),
        "the invite is listed (pending, its code never shown again)",
    )
    if ctx.args.join_cmd:
        show.step("Join the new laptop with the code")
        run_hook(show, ctx.args.join_cmd, stdin=code + "\n")
    elif ctx.args.dry_run:
        show.say("no --join-cmd: the join itself is skipped in this dry run")
        return
    show.step("Wait until the new laptop has joined")

    def joined() -> str | None:
        rows = compute.json("fabric", "invites").get("invites", [])
        row = next((r for r in rows if r.get("id") == invite_id), None)
        return str(row["used_by_node"]) if row and row.get("used_by_node") else None

    node_id = wait_for(
        "the code to be used", joined, within_s=prof.join_wait_s, every_s=1.0
    )
    show.expect(node_id not in before, "a new node joined with the code", node_id)
    ctx.state.invites.remove(invite_id)
    ctx.state.save()
    if ctx.args.dry_run and not ctx.args.wait_online:
        show.say(
            f"joined as {node_id} (its agent starts as a service on the real laptop)"
        )
        return

    def online() -> bool | None:
        nodes = {str(n["node_id"]): n for n in compute.nodes()}
        node = nodes.get(node_id)
        return True if node and node.get("connected") else None

    wait_for(
        "the new laptop to connect", online, within_s=prof.join_wait_s, every_s=1.0
    )
    show.expect(True, f"{node_id} is online")
    compute.run("nodes")


SCENARIOS: Final[dict[str, Callable[[Ctx], None]]] = {
    "01-local": scenario_local,
    "02-mode1-offload": scenario_offload,
    "03-mode3-split": scenario_split,
    "04-planner-explains": scenario_planner,
    "05-install-join": scenario_join,
}


def usage_problem(args: argparse.Namespace) -> str | None:
    """Why these options cannot run (checked before anything is touched)."""
    if args.scenario == "03-mode3-split" and args.dry_run and not args.kill_cmd:
        return (
            "03-mode3-split --dry-run needs --kill-cmd (how to kill a laptop's agent)"
        )
    return None


def reset(ctx: Ctx) -> None:
    ctx.show.step(f"Reset {ctx.args.scenario}")
    reset_fabric_objects(ctx.compute, ctx.state)
    revive(ctx)
    ctx.state.jobs.clear()
    ctx.state.invites.clear()
    ctx.state.save()
    for sub in ctx.work.iterdir() if ctx.work.is_dir() else ():
        if sub.is_dir() and sub.name.startswith(("out-", "job", "split")):
            shutil.rmtree(sub)
    ctx.show.say("clean: no demo job running, no demo invite open, outputs removed")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="demo.py",
        description=(__doc__ or "").splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("scenario", choices=sorted(SCENARIOS))
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="unattended rehearsal: no pauses, small jobs, all checks",
    )
    p.add_argument(
        "--reset", action="store_true", help="only undo what the scenario left"
    )
    p.add_argument(
        "--work", type=Path, help="folder for projects, outputs and state.json"
    )
    p.add_argument(
        "--compute", default="", help="how to start compute (default: this Python's)"
    )
    p.add_argument(
        "--dashboard-url", default="", help="controller URL, to print job links"
    )
    p.add_argument("--target", default="", help="02: the node to offload to")
    p.add_argument(
        "--kill-cmd",
        default="",
        help="03: command that kills a laptop's agent; {node_id} is filled in",
    )
    p.add_argument(
        "--revive-cmd", default="", help="03: command that starts it again; {node_id}"
    )
    p.add_argument(
        "--join-cmd",
        default="",
        help="05: command that joins a laptop, the code on its stdin",
    )
    p.add_argument(
        "--wait-online", action="store_true", help="05 dry run: also wait online"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    work = args.work or Path(tempfile.gettempdir()) / "meshforge-demo" / args.scenario
    work.mkdir(parents=True, exist_ok=True)
    show = Show(dry_run=args.dry_run)
    argv0 = (
        shlex.split(args.compute)
        if args.compute
        else [sys.executable, "-m", "meshforge.cli"]
    )
    compute = Compute(show, argv0, cwd=work)
    ctx = Ctx(
        show,
        compute,
        DemoState.load(work / "state.json"),
        work,
        args,
        DRY if args.dry_run else LIVE,
    )
    show.line(
        f"DEMO {args.scenario}{' (dry run)' if args.dry_run else ''} - work folder {work}"
    )
    problem = None if args.reset else usage_problem(args)
    if problem:
        show.line(f"DEMO {args.scenario}: USAGE - {problem}")
        return 2
    try:
        reset(ctx)
        if args.reset:
            show.line(f"DEMO {args.scenario}: RESET")
            return 0
        SCENARIOS[args.scenario](ctx)
    except DemoError as exc:
        show.failures.append(str(exc))
        show.say(f"FAILED {exc}")
    except KeyboardInterrupt:
        show.line(f"DEMO {args.scenario}: INTERRUPTED (run with --reset to clean up)")
        return 130
    elapsed = show.elapsed()
    show.expect(
        elapsed < BUDGET_S, f"finished within {BUDGET_S:.0f} s", f"{elapsed:.0f} s"
    )
    verdict = "FAIL" if show.failures else "PASS"
    show.line(f"\nDEMO {args.scenario}: {verdict} in {elapsed:.1f} s")
    for failure in show.failures:
        show.line(f"  - {failure}")
    return 1 if show.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
