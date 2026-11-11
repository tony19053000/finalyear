# ADR-0024: Warm lane worker — cut the fixed per-chunk cost on venv lanes

> **Status:** Proposed (2026-11-12) — team GO for round G11 (lab chat 20261112-043333-L1-to-LEAD, confirmed by the user in the BUG-FIX session) · **Deciders:** team + Lead agent

## Context
Lab round 10 (L1 REPORT 2026-11-12, controller journal granted→committed) measured a fixed cost on **every**
Mode 3 chunk of a Windows venv lane: L4 ~20 s, L2 ~10 s, L3 (CPU) ~11 s, plus 12–16 s on the first chunk; L1
(Linux, Docker) ~0. `import torch; torch.cuda.init()` alone is 3.4 s warm / 13 s cold on L4, so most of it is
other per-chunk work. Each chunk today (`agent/execution/lane_chunk.py`, `subprocess_runtime.py`,
`venv_runtime.py`) repeats: the env check (`ensure_venv` → `env_state` → one `lstat` per venv file — tens of
thousands of files for CUDA torch, scanned by Defender), the cap baselines (a walk of the workdir), a process
spawn, the interpreter start and the user's imports / model load. With 2-item chunks this overhead dominates
the job and wipes out the Mode 3 speed-up the final evaluation must show. The planner also uses one shared
`chunk_overhead` per job, so it cannot see that L4 chunks cost 20 s more than L1's.

## Decision
Two steps, both inside the existing lane runtime (no new process boundary on the controller):

**Step A — transparent, for every job (no script change).**
1. The lane resolves and leases its env once; `LaneState` keeps the resolved `EnvUse`; chunks 2+ skip
   `env_state` (re-checked after any chunk failure).
2. Cap baselines are measured once per lane and carried forward.
3. Per-chunk phase times (`staging_s`, `env_check_s`, `spawn_s`, `run_s`, `upload_s`, `warm`) go to the
   controller in `ChunkResult.phases`, into `chunk_record`, `compute jobs show --json` and the planner.

**Step B — opt-in warm worker.** Manifest `split.warm: {module, setup, run}`. One long-lived Python worker per
lane, started through the same `agent/process.py` path (same job object, limits, interpreter, `direct_python`),
imports the module and calls `setup()` once, then `run(items_file, out, chunk_index)` per chunk over
length-prefixed JSON frames on a dedicated pipe. Before each chunk it resets CWD, `os.environ`, the temp dir
and the RNG seeds (derived from `chunk_index`). Output/stderr keep the normal pump; `ChunkResult` is unchanged
plus `warm`. **Fallback:** a crash, timeout, stall (D451 watch on the worker pid, reset per chunk), cap breach
or memory kill respawns the worker and runs that chunk cold; two worker failures in one job make the lane cold
for that job. CancelChunk kills the worker (respawned lazily); NoWork/drain closes it cleanly. Docker lanes are
out of scope (already ~0).

**Planner.** Per-node fixed chunk cost and first-chunk cost learned from `chunk_record` phases (replacing the
single shared `chunk_overhead` where per-node history exists; the assumption stays the fallback), separately
for warm and cold chunks.

## Alternatives
1. **Transparent warm mode** (pre-import, `runpy.run_path` per chunk): no user change, but module-level code
   re-runs, `sys.exit`/argparse/global state break, results may differ. Rejected as the default.
2. **Zygote / fork a warm template per chunk:** full isolation, but Windows has no `fork` — and Windows is where
   the cost is. Rejected.
3. **Bigger chunks on slow-start nodes only (planner):** cheap and kept (the planner change does this
   implicitly), but it does not remove the cost and hurts load balance and the job tail.
4. **Installer Defender exclusion for the env dir:** may cut the cold cost; a security trade-off for the team
   to decide separately, not part of this ADR.

## Trade-offs
Step A helps every job at low risk but saves only the env check and cap walks (~40–50 % of the fixed cost).
Step B saves the interpreter start and imports but needs a hook in the user's script and weakens isolation
between chunks of one lane (memory, CUDA cache, globals from `setup` carry over — documented as the user's
contract; speculative copies with `deterministic: true` still catch divergence). One warm worker holds GPU
memory between chunks.

## Consequences
- New user-facing manifest field `split.warm` and docs in `docs/user`.
- Stall watch, caps and the stderr tail are keyed per chunk, not per process.
- New phase columns in `chunk_record` (operational) and in `jobs show --json`.
- Lab acceptance: split time before/after on L2 and L4 (same job, v0.17.2 vs v0.17.3), recorded in
  `docs/BENCHMARKS.md`.
