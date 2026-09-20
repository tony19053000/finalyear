# ADR-0012: Five-week budget — ship Mode 1 and Mode 2; defer Mode 3 and Mode 4

> **Status:** Proposed (2026-09-20) — **needs the other three team members' approval**; this changes an item the brief calls "relatively fixed" · **Deciders:** team + Lead agent

## Context

`CLAUDE.md` §"Working model" states: *"**No deadline.** The target is completing the full vision at the quality bar — order work by dependency, never cut scope for time."* Every downstream rule inherits that premise.

On 2026-09-20 the team stated the real constraint for the first time: **one Claude account, ~5 weeks of usable agent time.** The premise is false, so the plan built on it has to be rechecked.

**Measured throughput** (from `git log`, this repository, not an estimate):

| Phase | Wall clock | Tasks reaching reviewer-APPROVE | Rate |
|---|---|---|---|
| 2026-09-15, V0.1 scaffolding | 11 h 31 m | ~12 | 1.04 /h |
| 2026-09-20, V0.2 spikes | 7 h 34 m | ~4.5 | 0.60 /h |

**Required:** 171 open tasks ÷ (5 weeks × 9 sessions × 5 h = 225 h) = **0.76 tasks/h sustained**, on work that gets harder every version (V0.1 was `pyproject.toml` and a package skeleton; V0.12/V0.13 are Combined-GPU inference and training).

Three factors make the raw division optimistic:
1. 9 sessions/week is more than one per day for five weeks, alongside other coursework. ~75 % attainment is realistic → ~170 h.
2. The binding limit is a **token quota, not a clock.** The implementer → tester → reviewer loop cold-starts each agent, which re-derives the same context every run. Observed 2026-09-20: a ~30-line change to `_harness/report.py` cost **54 k tokens**; 0.2.6 (S2) needed 7 agent runs (1 implementer + 2 tester + 4 reviewer rounds).
3. **24 task lines carry 🧑** and need physical multi-laptop runs — calendar and four co-located people, not agent hours.

Realistic capacity is **70–90 tasks** with the token-efficiency fixes below, and ~40 without them. Not 171.

## Decision

1. **Ship Mode 1 (Job Offloading) and Mode 2 (Remote GPU), both at the full quality bar.** Target versions V0.3 → V0.6 (fabric + Mode 1 complete), then V0.11 (Mode 2), plus thin slices of V0.7 (security) and V0.8 (dashboard/CLI) for a presentable product.
2. **Defer Mode 3 (V0.10) and Mode 4 (V0.12, V0.13, V0.16)** to documented future work: architecture written, ADRs kept, and their foundations genuinely built — Mode 3 extends Mode 1, Mode 4 extends Mode 2, so one of each row of the 2×2 is proven.
3. **Do not lower the quality bar** on what ships. Scope is the lever; process and correctness are not. `CLAUDE.md` §8 stands: Remote GPU is real, never simulated.
4. **Cut token waste** — this is half the fix and costs nothing:
   - replace "one task per implementer run" with "one *coherent group* per run";
   - front-load each brief with the standing objection checklist so reviewers approve in 1–2 passes (demonstrated 2026-09-20: S6 came back green on the first reviewer-grade brief, against S2's four passes);
   - the Lead makes doc edits and small mechanical fixes directly, with no subagent;
   - run the ~6-minute component suite once per task group, not once per agent.
5. **`CLAUDE.md`'s "No deadline" clause is superseded** for the remainder of this project and must be amended when this ADR is accepted.

## Alternatives considered

- **Keep all four modes.** Arithmetically impossible at 171 tasks; the only reachable version is four shallow modes, which `CLAUDE.md` §8 forbids outright ("Remote GPU and Combined GPU must be **real**, never simulated").
- **Mode 1 + Mode 3.** Cheaper than Mode 2 per task, but builds the same row of the 2×2 twice and demonstrates nothing of "the program stays, the computation leaves" — the claim that most distinguishes the project from a remote-execution wrapper.
- **Mode 1 + Mode 4** (proposed by the team 2026-09-20, withdrawn the same day). Strictly *more* work, not less: roadmap V0.12's prerequisites are **V0.11 (Mode 2) + V0.10 (Mode 3's shared queue)**, so it pulls in almost the entire plan. It is also **not known to be possible on this hardware** — `docs/LAB.md` records only Laptop 1's GPU (RTX 3050, ~4 GB); laptops 2–4 are TBD, and Mode 4 needs several real GPUs.
- **Keep scope, cut the tester/reviewer loop.** Buys perhaps 30 % throughput and costs the evidence discipline that keeps generated output from being confidently wrong — the exact defect class the S2 and S6 reviews caught. Rejected: it trades the project's credibility for its size.

## Trade-offs

- **Gained:** a finishable project; two execution modes that genuinely work on real machines; honest, demonstrable benchmarks; a credible extension story.
- **Lost:** the four-mode headline. If the evaluation rewards *breadth of demonstrated features* over *depth*, this is the wrong trade and the team must say so before work starts.
- **Risk:** Mode 2 depends on spike **S4** (task 0.2.8, `torch.export` round-trip), which is blocked on **ADR-0011**. That decision now gates the whole second half of the project and is urgent.
- **Risk:** deferring V0.10 means V0.12's shared work queue is never built, so resuming Mode 4 later costs more than the roadmap's task count suggests.

## Consequences

- `docs/DEVELOPMENT_ROADMAP.md` marks V0.10, V0.12, V0.13, V0.16 as **Deferred (ADR-0012)** rather than deleting them; the task text stays as the future-work record.
- `docs/PROJECT_SUMMARY.md` must be rewritten: it currently presents four modes as the product.
- `docs/PROJECT_BRIEF.md` §33 ("relatively fixed: all four execution modes are major targets") gets a decision-log entry pointing here. **The brief changes only with team approval — this ADR stays Proposed until then.**
- ADR-0011 becomes the critical path. `lab/inventory.py` on laptops 2–4 becomes urgent: it decides ADR-0011 *and* confirms whether deferring Mode 4 was forced by hardware anyway.
- The "one session per version" protocol in `CLAUDE.md` §4c is already invalid on arithmetic (V0.3 alone is 15 hard tasks) and is replaced by "one session per coherent task group".
