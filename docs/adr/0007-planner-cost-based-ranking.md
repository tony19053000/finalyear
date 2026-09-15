# ADR-0007: Planner = candidate generation + cost estimation + policy; ML predicts cost components

> **Status:** Proposed (2026-09-15) — pending team approval · **Deciders:** team + architect agent

## Context
The fabric must choose between Local and Modes 1–4, which nodes, and how much work each gets — explainably, with deterministic baselines and a path to ML.

## Decision
Pipeline: **candidate generator → feasibility filter → cost estimator → policy/ranking**. Costs are decomposed; physics-known parts are **analytical** (transfer = missing bytes / measured bandwidth), unknown parts are **learned** (compute time per workload × node, env time, overhead residuals). Rule policies (random, round-robin, least-loaded, strongest, resource-score, cache-affinity) stay available as baselines; `oracle` exists for evaluation. ML falls back to analytical when uncertain. Every decision stores all candidates, reasons, predictions and later the actual outcome.

## Alternatives considered
Direct classifier features → mode (kept only as an experiment); reinforcement learning (data-hungry, hard to evaluate).

## Trade-offs
+ explainable, safe, evaluable by regret; + ML has a precise job. − more modelling work than a classifier.

## Consequences
Telemetry schema must capture phase timings and snapshots from V0.4.
