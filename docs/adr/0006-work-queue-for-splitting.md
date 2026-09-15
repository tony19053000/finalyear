# ADR-0006: Pull-based leased work queue for Task Splitting and Combined-GPU inference

> **Status:** Accepted (2026-09-15, team approved with roadmap v1) · **Deciders:** team + architect agent

## Context
Laptops differ in speed and may slow down or disappear. Static equal splits make everyone wait for the slowest node.

## Decision
Divisible work is cut into **chunks** placed in a **work queue**; workers **pull** chunks under **leases** (visibility timeout). Expired leases re-queue chunks; tail chunks may be speculatively duplicated (first result wins, idempotent completion). The planner chooses participants and chunk size. The **same module** serves Mode 3 (chunks = sub-tasks) and Mode 4 inference (chunks = batches).

## Alternatives considered
Static proportional shares; controller push-based assignment.

## Trade-offs
+ automatic heterogeneity balancing and straggler/failure tolerance; + one implementation for two modes. − per-chunk overhead → chunk size must be tuned (planner).

## Consequences
Mode 3 and Mode 4 inference inherit fault tolerance from one tested component.
