# ADR-0012: Ship Mode 3 (with Mode 1 as its foundation) complete and production-grade; defer Mode 2 and Mode 4

> **Status:** Proposed (2026-09-20, revised twice the same day — 5-week draft, then the secured 10-week budget) — **needs the other three team members' approval**; it changes an item `PROJECT_BRIEF` §33 calls "relatively fixed" · **Deciders:** team + Lead agent

## Context

`CLAUDE.md` §"Working model" states: *"**No deadline.** The target is completing the full vision at the quality bar — order work by dependency, never cut scope for time."* Every downstream rule inherits that premise.

On 2026-09-20 the team stated the real constraint for the first time: **one Claude account, ~5 weeks of usable agent time**, and later the same day **secured ~10 weeks**. This ADR is written against the **10-week** budget; the 5-week analysis is kept below because it is what forced the scope question, and because the plan must degrade gracefully if the extension does not hold.

**Measured throughput** (from `git log`, this repository — not an estimate):

| Phase | Wall clock | Tasks reaching reviewer-APPROVE | Rate |
|---|---|---|---|
| 2026-09-15, V0.1 scaffolding | 11 h 31 m | ~12 | 1.04 /h |
| 2026-09-20, V0.2 spikes | 7 h 34 m | ~4.5 | 0.60 /h |

**Required for the full four-mode plan at 5 weeks:** 171 open tasks ÷ (5 weeks × 9 sessions × 5 h = 225 h) = **0.76 tasks/h sustained**, on work that gets harder every version (V0.1 was `pyproject.toml` and a package skeleton; V0.12/V0.13 are Combined-GPU inference and training).

Three factors make the raw division optimistic:
1. 9 sessions/week is more than one per day for five weeks, alongside other coursework. ~75 % attainment → ~170 h.
2. The binding limit is a **token quota, not a clock.** The implementer → tester → reviewer loop cold-starts each agent, which re-derives the same context every run. Observed 2026-09-20: a ~30-line change to `_harness/report.py` cost **54 k tokens**; task 0.2.6 (S2) needed 7 agent runs (1 implementer + 2 tester + 4 reviewer rounds).
3. **24 task lines carry 🧑** and need physical multi-laptop runs — calendar and four co-located people, not agent hours.

Realistic capacity: **70–90 tasks** in 5 weeks *with* the token-efficiency changes below (~40 without them), and therefore **140–180 tasks in 10 weeks**. The full four-mode plan of 171 tasks sits at the very top of even the 10-week range with no margin, so it is still not what this ADR commits to.

**Hardware reality** (team-stated 2026-09-20, pending `lab/inventory.py` — see `docs/LAB.md` §1): Laptop 1 RTX 3050 (~4 GB, confirmed), Laptop 2 ≈ RTX 3050, Laptop 3 ≈ RTX 2050, **Laptop 4 has no GPU**. Three GPUs of ~4 GB plus one CPU-only node.

## Decision

1. **Ship Mode 3 (Task Splitting) complete and production-grade.** Nothing in Mode 3 is left out or stubbed. Mode 1 (Job Offloading) ships with it, because Mode 3 is Mode 1 extended from one laptop to many and is built directly on its machinery.
2. **Keep the AI in full.** The planner's ML role is *not* optional and is not reduced to cost formulas: V0.9 (telemetry & benchmarks), a Mode-1/Mode-3-scoped V0.14 (planner v2), and V0.15 (ML planner) are all in scope. `CLAUDE.md` names "AI has a real optimization role (never a chatbot/decoration)" and "telemetry/benchmarking is first-class" as fixed; an earlier draft of this ADR violated both and was rejected by the team.
3. **Keep the dashboard (V0.8) and security (V0.7) in full**, plus productization (V0.17) and release hardening (V1.0), so the result is a finished product and not a harness. At 10 weeks **nothing is trimmed**: the earlier plan to thin the data plane (V0.5) and the dashboard was forced by the 5-week ceiling and is withdrawn.
4. **Defer Mode 2 (V0.11) and Mode 4 (V0.12, V0.13, V0.16)** to documented future work: architecture written, ADRs kept, foundations genuinely built. **Revisit once Mode 3 is complete** — if time remains, Mode 2 is the next build, because Mode 4 extends Mode 2.
5. **Do not lower the quality bar.** Scope is the lever; process and correctness are not. `CLAUDE.md` §8 stands.
6. **Cut token waste** — roughly half the fix, and it costs nothing:
   - replace "one task per implementer run" with "one *coherent group* per run";
   - front-load each brief with the standing objection checklist so reviewers approve in 1–2 passes (demonstrated 2026-09-20: S6 came back green on the first reviewer-grade brief, against S2's four passes);
   - the Lead makes doc edits and small mechanical fixes directly, with no subagent;
   - run the ~6-minute component suite once per task group, not once per agent.
7. **`CLAUDE.md`'s "No deadline" clause is superseded** for the remainder of this project and must be amended when this ADR is accepted.

**Scope in tasks (10-week plan, nothing trimmed):**

| Block | Tasks | Running |
|---|---|---|
| V0.3 fabric core (controller ⇄ agent) | 15 | 15 |
| V0.4 remote execution core (Mode 1) | 15 | 30 |
| V0.5 data plane & environments — **full** | 11 | 41 |
| V0.6 scheduling & reliability (Mode 1 complete) | 13 | 54 |
| V0.9 telemetry & benchmark system | 10 | 64 |
| V0.10 **Mode 3 task splitting** | 10 | 74 |
| V0.14 planner v2 (re-scoped: Local + M1 + M3) | 8 | 82 |
| V0.15 **ML planner** | 10 | 92 |
| V0.7 security & pairing — **full** | 11 | 103 |
| V0.8 dashboard & CLI v1 — **full** | 10 | 113 |
| V0.17 productization | 8 | 121 |
| V1.0 release hardening & final evaluation | 8 | **129** |

**129 tasks against a 140–180 capacity** — roughly 11–51 tasks of margin. The margin is deliberate: it absorbs lab-coordination delays, hardware failures, and the ML planner needing more telemetry than expected. **V0.9 is pulled forward, before V0.10**, so telemetry accumulates during Mode 3 development instead of after it.

**If the margin survives**, the next build is **V0.11 (Mode 2, 13 tasks)** — not because it is the cheapest, but because Mode 4 extends it, so it is the only step that moves the team's original goal forward. It is a stretch, never a commitment.

**If the 10 weeks collapses back to ~5**, the degradation order is fixed in advance: drop V1.0, then V0.17, then thin V0.8 to a read-only dashboard, then thin V0.5's polish (0.5.6 named inputs, 0.5.9 GC). **Mode 3, the ML planner and V0.9 are never cut** — they are the project.

## Why Mode 3 rather than the GPU modes

The team's **original motivation was Mode 4** — "my laptop is weak, let me combine other laptops' GPUs." That is recorded here so the choice is not mistaken later for indifference to it.

- **Dependency cost.** Roadmap V0.12 (Mode 4) requires **V0.11 (Mode 2) *and* V0.10 (Mode 3's shared work queue)**. Mode 4 is ~87 tasks against Mode 3's ~64; picking it means building most of the other modes first. Mode 3's only prerequisite is V0.6.
- **Hardware fit.** Mode 3 uses every machine, including the **CPU-only Laptop 4**, by giving it a smaller share. The GPU modes cannot use it at all.
- **The AI has more to do.** With two RTX 3050s, one RTX 2050 and one CPU-only node, deciding *how much* work each machine gets is a real optimization problem with a real cost to getting it wrong (the slowest node sets the makespan). That is the brief's central question, made concrete.
- **VRAM.** ~4 GB per card limits what Mode 4 could honestly demonstrate anyway.
- **Mode 3 still delivers the original wish** for batch workloads: a job finishes 2–3× faster by using the other laptops' GPUs (roadmap 0.10 criterion 3). What it cannot do is run a *single* computation too large for *one* GPU — that case genuinely needs Mode 4.

## Alternatives considered

- **Keep all four modes (5 weeks).** Arithmetically impossible; the only reachable version is four shallow modes, which `CLAUDE.md` §8 forbids ("Remote GPU and Combined GPU must be **real**, never simulated").
- **Mode 1 + Mode 2** (this ADR's first draft). Rejected by the team: it cut V0.9 and V0.15, silently removing the AI pillar.
- **Mode 1 + Mode 4** (proposed and withdrawn 2026-09-20). Strictly *more* work: V0.12 pulls in V0.11 and V0.10.
- **Use the 10 weeks to keep all four modes.** ~140–180 tasks would make all four modes plus thin Mode 4 inference *arithmetically* feasible (~166), but with zero margin and with V0.13 (Mode 4 training) and V0.16 (model splitting) — the most research-grade work in the plan — at the end, where any slippage lands. The team chose depth over breadth 2026-09-20: **one mode finished completely, then reconsider**. The margin this buys is the point, not a leftover.
- **Keep scope, cut the tester/reviewer loop.** ~30 % throughput for the loss of the evidence discipline that catches confidently-wrong generated output (the defect class the S2 and S6 reviews caught repeatedly). Rejected.

## Trade-offs

- **Gained:** a finishable project with real margin; one execution mode genuinely complete and production-grade; the AI doing real work on genuinely heterogeneous hardware; honest benchmarks; a shipped product (security, dashboard, productization, hardening) rather than a demo; a clean base for Mode 2 → Mode 4 afterwards.
- **Lost:** the four-mode headline, and — for now — the GPU-combining idea that motivated the project.
- **Risk:** V0.15 expects **several thousand telemetry runs** to train on. That is lab hours on four laptops, not agent hours. Benchmark jobs must be small, fast and automated **from V0.9 onward** so data accumulates during development; otherwise the ML planner has nothing to learn from at the end.
- **Risk:** if the evaluation rewards *breadth of demonstrated modes* over depth, this is the wrong trade. **The team has not yet answered this question** and should before work starts.
- **Risk:** deferring V0.11 means the GPU service, tensor codec and `torch.export` path are never built, so resuming Mode 2 later costs its full 13 tasks.

## Consequences

- `docs/DEVELOPMENT_ROADMAP.md` marks V0.11, V0.12, V0.13, V0.16 as **Deferred (ADR-0012)** rather than deleting them; their task text stays as the future-work record. V0.14 is **re-scoped** to Local + Mode 1 + Mode 3 candidates only.
- `docs/PROJECT_SUMMARY.md` must be rewritten — it currently presents four modes as the product.
- `docs/PROJECT_BRIEF.md` §33 and the §40 decision log get an entry pointing here. **The brief changes only with team approval; this ADR stays Proposed until then.**
- **ADR-0011 and spike S4 (0.2.8) drop off the critical path** — they gated Mode 2. **S5 (0.2.9, collectives) likewise.** torch is still needed for GPU-backed Mode 3 chunks, so ADR-0011 still matters, but it no longer blocks the plan's spine.
- `lab/inventory.py` on laptops 2–4 stays urgent: it confirms the hardware this decision rests on.
- The "one session per version" protocol in `CLAUDE.md` §4c is already invalid on arithmetic (V0.3 alone is 15 hard tasks) and is replaced by "one session per coherent task group".
