# ADR-0018: Telemetry dataset and benchmark campaigns (V0.9)

> **Status:** Accepted (2026-10-17) — team approved the V0.9 brief ("go with your recommendations") · **Deciders:** team + Lead

## Context
V0.9 must produce large, labelled, reproducible performance datasets from real runs so V0.14 (planner v2) and
V0.15 (ML planner) have something to learn from (ADR-0012: several thousand runs). The fabric already records
attempts, phase timings, plan decisions, resource buckets, network links and cache state in PostgreSQL. Laptop
clocks drift and Wi-Fi adds jitter. Details: `docs/briefs/V0.9-benchmark-telemetry.md` D211–D224.

## Decision
1. **Record vs dataset.** PostgreSQL holds versioned *run records* (migration 0021, `schema_version`); the dataset
   is a **Parquet export** (pyarrow) built by a pure function. Same campaign ID → byte-identical export (D211, D212, D222).
2. **Durations, not timestamps.** Compute and phase times are monotonic durations measured where the work runs;
   the controller stores each node's clock offset (± RTT/2) with the record (D213).
3. **Forced strategies through the planner**, not around it: pinned node + `benchmark` tag + campaign ID; the
   planner's would-have-chosen answer is recorded. V0.9 strategies = LOCAL / Mode 1 on node X; later modes add theirs (D215).
4. **Campaigns** = YAML spec, deterministic expansion from (spec, seed), seeded random interleaved order, ≥ 3
   repetitions; controller-side resumable runner with throttle and an owner guard; benchmark jobs are lowest priority (D216, D219).
5. **Verified conditions** (load, cache, queue, `tc netem` on Linux via a narrow sudoers rule, node pause); a
   condition that cannot be verified marks the run `condition_failed` (D217).
6. **Labels** per group: cost vectors per strategy; `best_strategy` only when it beats the next beyond noise, else `tie` (D218).
7. **Dependencies:** `pyarrow` (controller); `pandas` dev/report only; no scipy, no external experiment trackers (D223).

## Alternatives considered
- Write Parquet during execution — two write paths, partial files on crash; rejected.
- Bypass the planner for forced runs — loses the "planner would pick" signal and exercises a path users never use; rejected.
- MLflow / W&B — extra service, internet-oriented; rejected for an offline LAN.
- Cross-host timestamp subtraction — wrong by clock skew (D210 item 1 showed this); rejected.

## Trade-offs
+ One source of truth; reproducible, testable export; labels that do not teach noise; owners not disturbed.
− Schema versioning discipline on every later version; `netem` needs a root rule and works on Linux only (Windows
  nodes report shaping unavailable); runner adds controller state to keep resumable.

## Consequences
- New: migration 0021, `controller/telemetry/`, `meshforge.bench`, `compute bench plan|run|report`, Benchmarks page.
- Every later mode version (V0.10+) adds workload families and strategies here, not new schemas.
- Revisit if campaign volume makes Postgres the bottleneck, or V0.15 needs features the record cannot carry.
