# ADR-0020: Planner v2 — calibrated costs, risk, objectives, replayable decisions

> **Status:** Proposed (2026-10-21) — team said "start V0.14" on the Lead's §7 recommendations · **Deciders:** team + Lead agent
> Brief: `docs/briefs/V0.14-unified-planner-v2.md` (D290–D303). Builds on ADR-0007, ADR-0018, ADR-0019; scope per ADR-0012.

## Context
V0.6 and V0.10 already built a pure, cost-based planner comparing Local, Mode 1 and Mode 3 (prefix node sets, margin
test), with per-term sources and explainable decisions. What it cannot do is (1) correct its own systematic errors from
telemetry, (2) account for unreliable nodes, (3) serve objectives other than speed, or (4) be evaluated honestly: regret
vs oracle needs to re-run *other* policies on the situation the planner actually saw, and today that situation is not
stored. V0.14's criteria (within 15 % of oracle in ≥ 80 % of held-out groups; lower regret than every rule policy) and
all of V0.15 depend on (4).

## Decision
1. **Replayable decisions.** Every `plan_decision` stores a versioned, bounded (64 KiB) JSON *replay snapshot* of the
   `PlanRequest` and the planner-relevant `ClusterSnapshot`; `plan()` on it is pure and must reproduce the stored choice
   exactly (D292). Migration 0030.
2. **One candidate generator** for whole and split jobs (Local, Mode 1 × node, Mode 3 × prefix set); `AcceleratorRequest`
   is a typed "not supported in this build" until Mode 2/4 return (D290, D291).
3. **Calibration** = per-term multiplicative factors, median(actual/predicted) per (node, family) with shrinkage toward 1
   and clamping, versioned in the DB, refreshed periodically, activated only if it does not worsen held-out regret
   (D293, D301).
4. **Risk** = Beta-smoothed rate of *node-caused* failures, converted into an expected-time term that models each mode's
   real recovery cost (whole re-run for Mode 1, leased chunks only for Mode 3) (D296).
5. **Objectives** `min_time` (default), `min_network`, `prefer_local` are ranking rules over the same estimates (D295).
6. **Offline evaluator** replays labelled campaign groups for every policy; regret against measured medians; ties count 0;
   unmeasured picks reported, never counted as 0; calibration fitted on families held out from evaluation (D297–D299).
   `oracle` is evaluation-only.
7. **No new dependencies** (D303).

## Alternatives considered
- **Re-run policies against today's cluster state** instead of storing snapshots — compares against a different world;
  rejected.
- **Fit a regression model per term now** — that is V0.15's job and needs more data than V0.14 will have; median-ratio
  with shrinkage is robust with tens of runs and is explainable at evaluation.
- **Full 2^N set enumeration** — unnecessary: fastest-first prefix sets plus dominance pruning keep latency bounded and
  were validated in V0.10.
- **Weighted multi-objective score** — weights nobody can explain or tune; three named objectives instead.

## Trade-offs
+ Honest, reproducible evaluation; V0.15 gets training rows and a baseline to beat; calibration is safe by construction.
− ≤ 64 KiB extra per decision row; a snapshot schema that must be versioned forever; calibration lags behind hardware
changes by its window.

## Consequences
- `plan_decision` gains `replay`, `replay_version`, `objective`, `calibration_version`; new `planner_calibration` table.
- Planner package stays pure; the AST import test still applies.
- New CLI `compute planner calibrate|eval`; dashboard Plan tab extended and a Planner page added.
- Revisit if snapshots exceed the cap often (then store references to shared rows), or if V0.15 shows per-term factors
  are the wrong granularity.
