# Demo scenarios (roadmap 1.0.7, D420)

Five scripted, repeatable scenarios for the evaluation, one per shipped capability. Each one drives the real
`compute` command against a real fabric - the commands it runs are printed, so the audience sees exactly what a
user would type - and checks its own results. Nothing is simulated.

| Script | Shows | Laptops | Typical time |
|---|---|---|---|
| `01-local.sh` | the baseline: a job on this laptop's own agent | 1 (with an agent) | < 1 min |
| `02-mode1-offload.sh` | Mode 1: the same job sent to another laptop; only the output comes back, byte-identical | 2 | 1-2 min |
| `03-mode3-split.sh` | Mode 3: one job split over the laptops; one laptop is killed mid-way; the job still succeeds | 3+ | 2-3 min |
| `04-planner-explains.sh` | the planner choosing **Local**, then **Mode 1**, then **Mode 3** for three jobs, and saying why (`compute explain`, the dashboard's Plan tab) | 3+ | 2-4 min |
| `05-install-join.sh` | a join code for a new laptop; waits until the freshly installed Windows laptop has joined | 1 + the new one | ~5 min (the install is the recorded part) |

Every scenario must finish in under 5 minutes; the script checks it.

## Before the show

1. Run the scripts on a laptop that has the repository, `uv sync --extra controller --extra agent` done, and
   **its own enrolled agent** (that laptop is "this laptop" / Local in every scenario).
2. Log in once: `compute login --controller https://<controller>:47110 --fingerprint <CA fingerprint> -u <you>`.
3. `compute nodes` shows every laptop `online`. 03 and 04 need at least three laptops; each laptop accepts jobs
   (`meshforge-agent policy show`).
4. Rehearse with `--dry-run` (below) the day before, then run every script once with `--reset`.

## Running a scenario

```bash
scripts/demo/01-local.sh
scripts/demo/02-mode1-offload.sh [--target NODE_ID]
scripts/demo/03-mode3-split.sh [--kill-cmd "CMD {node_id}"] [--revive-cmd "CMD {node_id}"]
scripts/demo/04-planner-explains.sh --dashboard-url https://<controller>:47110
scripts/demo/05-install-join.sh
```

Live, the script pauses ("press Enter") between acts so the presenter can talk. Each check prints a line:

```text
   EXPECTED   the planner offloaded job B (Mode 1)
   UNEXPECTED the planner split the job - got offload
```

and the last line is the verdict: `DEMO 04-planner-explains: PASS in 143.2 s` (exit 0) or `FAIL` with the list of
what did not hold (exit 1). On a FAIL, switch to the fallback recording.

**Killing a laptop in 03.** Without `--kill-cmd` the script names the laptop to stop and waits for Enter: stop
its agent (`sudo systemctl kill -s KILL meshforge-agent` on Linux, `Stop-Service MeshForgeAgent -Force` in an
admin PowerShell on Windows) or pull its network cable. With `--kill-cmd`, the script runs that command with
`{node_id}` filled in (split like a shell line, never through a shell), for example a small script of your own that
maps node ids to laptops and runs the stop over SSH. `--revive-cmd` starts it again at the end.

**Joining in 05.** The script prints a single-use code (valid 15 minutes) and the Windows steps
([install-windows.md](../../docs/user/install-windows.md)), then waits up to 10 minutes until the code has been
used and the new laptop is online.

## What each scenario checks

- **01** the job succeeded, ran on this laptop (`submitter_node_id`), and `result.json` equals the value the demo
  computes itself.
- **02** 01's checks, then the job pinned to another online laptop (`--target`, else the first other one by id)
  succeeded there and its `result.json` is byte-identical to the local one.
- **03** the planner (no pinning) chose Mode 3; after the first piece is done and at least three are still waiting,
  one remote lane's laptop is killed; the job still succeeds, the killed lane is not `done`, and the combined
  output has every item once, in order, each line equal to the demo's own computation.
- **04** job A (`objective: min_network`, a busy job of 30 s) stays **Local**; job B, submitted while A holds
  this laptop's only CPU slot, goes to another laptop - **Mode 1** - and Local's row in its decision says
  `slots_full`; job C, a 48-item split job with every laptop free, is split - **Mode 3** - and its output is
  verified. `compute explain` is printed for each; with `--dashboard-url` the Plan-tab link of each job too.
- **05** the invite is listed (`pending`; its code is never shown again), is used by a node that was not in the
  fabric before, and that node comes online.

## Reset

Every scenario starts with its reset, and `--reset` runs only the reset:

```bash
scripts/demo/03-mode3-split.sh --reset --revive-cmd "CMD {node_id}"
```

It cancels every unfinished job named `demo-...` and every job the scenario recorded in its work folder's
`state.json`, revokes the scenario's invites that were not used, runs `--revive-cmd` for a laptop it killed, and
removes the old projects and outputs. Running it twice is harmless. The work folder defaults to
`$TMPDIR/meshforge-demo/<scenario>` (`--work DIR` to choose).

## Dry run (rehearsal and CI)

`--dry-run` is the unattended rehearsal: no pauses, smaller jobs (seconds, not minutes), every check still made
on real jobs. 03 needs `--kill-cmd` in a dry run (nobody is there to press Enter); 05 makes and revokes an invite
and, with `--join-cmd "CMD"` (the code on its stdin), also joins a laptop and waits until the code is used.

CI runs every scenario this way against the cluster-test fabric (a controller and three agents as real processes
on one machine): `tests/cluster/test_v10_g5_demos.py`. There the kill/revive hooks are
`tests/_support/mf_demo_ctl.py`, which asks the test harness to SIGKILL and restart the agent.

## Fallback recordings

Recorded in lab session L5 (two rehearsals, D420) with each script run live: `01-local.mp4` ... `05-install-join.mp4`
(the Windows install of 05 is screen-recorded on the new laptop). They are kept with the release assets, not in git
(the repository refuses files over 1 MB); the lab report lists their names, lengths and the timings of both
rehearsals.

## Mode 2 and Mode 4: deferred, designed, not built

There is no demo for Mode 2 (Remote GPU) or Mode 4 (Combined GPU), and none is mocked. One slide says:

> **Mode 2 Remote GPU and Mode 4 Combined GPU - deferred, designed, not built.** Version 1.0 ships Local,
> Mode 1 Job Offloading and Mode 3 Task Splitting with the AI planner, by a team decision
> ([ADR-0012](../../docs/adr/0012-scope-ship-mode3-complete.md)): one execution mode made complete and reliable
> on real laptops instead of four half-working ones. The design for both is written
> ([ARCHITECTURE §10-11](../../docs/ARCHITECTURE.md), [ADR-0005](../../docs/adr/0005-gpu-remoting-at-pytorch-level.md)):
> GPU work sent at the PyTorch level; the planner already has their place (a GPU request is answered
> `not_supported_in_build` today).
