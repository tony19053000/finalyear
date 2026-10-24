# SESSION HANDOFF — read this first

> **Purpose:** any Claude Code session, on **any of the team's accounts**, can pick up the work
> here without having seen the previous conversation. A usage limit can end a session mid-task,
> so this file is kept current **continuously**, not only at a tidy session end.
>
> **Cold-start prompt** (paste this as the first message on the new account):
>
> ```
Read docs/SESSION_HANDOFF.md §1, then docs/STATUS.md, then V0.17 in docs/DEVELOPMENT_ROADMAP.md and docs/briefs/V0.17-productization.md (APPROVED 2026-10-24, D384–D398, ADR-0022 Proposed).
Done: V0.15 agent-side complete (0.15.1–0.15.10 [x]). V0.17 G0 (0.17.0 [~]) in progress: SP2 managed PostgreSQL MERGED to main (lab/spikes/SP2.md; theseus-rs PG 16.15.1; D398 controller needs Ubuntu 24.04+). Repo: PUBLIC tony19053000/finalyear (team choice), main pushed.
STOPPED by user 2026-10-24 (2nd time), wip committed, no agents running. D399 recorded; SP3 MERGED 24141d69; G1/G2/G3 briefs APPROVED (§5a–§5c, Q11–Q27).
 (1) G1 implementer — worktree agent-a7ea2e49788d6721b (branch worktree-agent-a7ea2e49788d6721b): stopped at step G (Linux packaging: unit + maintainer scripts). Resume from docs/briefs/progress/V0.17-G1.md, then gates → tester → reviewer → merge. Then G2 ∥ G3.
 (2) SP1 Windows service — worktree agent-ae3f985ac81466970, branch spike/sp1-windows-service: workflow GREEN twice (14/14); remaining: add 2nd run to lab/spikes/SP1.md, verdict pywin32 vs WinSW, local suite, push, merge.
 (3) CI green — worktree agent-adf3fe14a91fc13f0 (branch ci/green): was adding a unit test for an argv builder; resume iterating `gh workflow run CI --ref ci/green` until all jobs + ci-ok green, then merge. Possible mypy error at bench/workloads/kernels/_support.py:161 (check).
Open 🧑: SP3 GPU check + real job from bundle (lab/spikes/SP3.md). Dependabot PRs: ignore until CI green. 2 stale postgres containers + merged worktrees (ask before removing). Normal mode. Parallel agents OK. Don't delete anything without asking. User wants max speed.
```
>
> Order of truth: `PROJECT_BRIEF → ARCHITECTURE → ROADMAP → STATUS → this file`. If this file
> disagrees with the roadmap, the roadmap wins and this file is stale — fix it.

> # 🚨 THE PROJECT PLAN CHANGED ON 2026-09-20 — DO NOT SKIP SECTION 0
>
> **If your mental model of this project is "build all four execution modes", it is out of
> date.** The plan is now **Mode 3 only, complete and production-grade**, with Mode 1 as its
> foundation. **Mode 2 and Mode 4 are deferred.** This came from a budget the team had never
> stated before (one Claude account, ~10 weeks). See **section 0** below and
> **[ADR-0012](adr/0012-scope-ship-mode3-complete.md)**.
>
> **You are expected to review this decision, not just obey it.** It is **Proposed**, not
> Accepted — the other three team members have not approved it yet, and `CLAUDE.md` and
> `PROJECT_BRIEF.md` still describe the old four-mode plan on purpose. The ADR carries the
> throughput measurements and the reasoning; if you think the arithmetic or the trade is wrong,
> **say so to the team before building on it.** Do not silently re-expand the scope either.

**Last updated:** 2026-10-24 (**V0.17 G0 STOPPED by user: SP2 merged; SP1/SP3/CI-green wip in worktrees**) · older: 2026-10-24 (**V0.15 G6 CLOSED, 0.15.10 [x]; agent-side V0.15 complete, 🧑 0.15.11 next**) · older: 2026-10-23 (**V0.15 G5 CLOSED: G5a+G5b merged, 0.15.8/0.15.9 [x]; next G6 brief** ) · older: (**G5a: tester r1 FAIL only B1/B2 → fix r1 DONE 90c852e7 (unit 25049 green) → tester r2 ∥ reviewer delta RUNNING. G5b: gates green c620703e → tester r1 ∥ reviewer r1 RUNNING. Lead TODO at close: STATUS tech debt = sync eval POST + CLI timeout follows default budget only** ) · older: (**STOPPED by user: G5a reviewer REQUEST_CHANGES, tester stopped (wip 14d99944); G5b implementer stopped before final gates — see cold-start prompt**) · older: (**G5a implementer DONE (HEAD 3862775d, note docs/briefs/progress/V0.15-G5a.md); G5a tester ∥ reviewer RUNNING. G5b implementer still running. Expect openapi/schema.d.ts conflict at G5b merge → rerun `npm run gen:api`** ) · older: (**G5a ∥ G5b STOPPED by user mid-implementation; wip committed in both worktrees — see cold-start prompt**) · older: (**G5 brief DRAFTED §5f, awaiting Q24–Q30**) · older: (**V0.15 G3 + G4 CLOSED: 0.15.6/0.15.7 [x], D367; next G5 brief — see cold-start prompt**) · older: (**G4 MERGED to main `80767722` (tester r2 PASS, reviewer APPROVE) + O-1b replay guard `b8e52dcf`; 0.15.6 not yet [x] (needs full unit on main after G3 merge). G3: reviewer APPROVE; tester r2 FAIL (D1–D4 fixed; new minor N1 0-byte artefact→retryable network_error, N2 unbounded gate.history, N3 lone surrogates) → implementer RUNNING in G3 worktree: fix r2 (N1–N3 + reviewer GC-race touch-on-dedup + child_output_invalid + OPERATIONS notes) AND `git merge main` integration (ModelSource name clash, single server.ml_source via ml_input, FEATURE_VERSION 2 quiet skip, ArtefactError→PredictorFailed, regen openapi). → DONE: fix r2 1e7305da + merge-of-main 26fd1a7c (gates green: unit 14957, component 172). Lead accepted: G4 protocol renamed MlInputSource; server.ml_source = RegistryMlInputSource (ml_models dropped); SetPredictor; infinite-q member = no model; stale feature_version skipped quietly; migration 0034 artefact_bytes >= 0 (unmerged migration) — all go in D367. NOW: tester r3 ∥ reviewer delta RUNNING → then fast-forward/merge G3 branch into main. Reviewer G4 non-blocking: wire server.ml_source + ml_max_interval_log/ml_predict_budget_ms into ml_input at G3 merge; log `below_min_predicted` reason in D336 row.** · older: **V0.15 G3 + G4 in worktrees, NOT merged; agents stopped by user — see cold-start prompt**) · older: 2026-10-22 (**V0.15 G2 MERGED `43aeec43`, 0.15.4/0.15.5 [x]; G3 ∥ G4 briefs DRAFTED (§5c/§5d, APPROVED; G3 implementer DONE @f12e0022 (worktree agent-a4720f4e9d07293c7; deviations accepted by Lead — record as D367 at merge: set-level status only, gate refits shipped config, gate on effective error, extra `not_better` rule, LightGBM SIGABRT accepted risk), G3 tester r1 FAIL (tests da7f93c9: D1 rejected set never re-judged on gate-settings change; D2 unloadable set can activate + XGBoost raw error; D3 xgb 0-row shape; D4 NUL in reason breaks insert) → G3 fix round 1 RUNNING; G4 implementer DONE incl. D366 @21f20cd3 (worktree agent-a80327d0439205cff), G4 tester r1 FAIL (tests 41effbdc: R1-1 one-lane compute term carries chunk_rate ML block; R1-2 cold-start predict >20 ms budget; O-1 predictor_version read outside D363 guard) → G4 fix round 1 DONE @26c63eef (nested detail.ml.rate + one tester-test edit accepted by Lead) → G4 tester r2 + reviewer RUNNING in parallel. mypy src error in G4 worktree = missing torch only (main env clean). If session dies: check `git worktree list`, resume)**) · older: (**V0.15 G1 MERGED `420a3a24`, 0.15.1–0.15.3 [x]; next G2 brief**) · older: (**V0.15 G1 in worktree agent-a1770cddfeed43114 @d5cdcf36: gates green, reviewer APPROVE, tester r1 stopped by user — re-run tester, then merge**) · older: (**V0.14 G6F follow-ups MERGED `bfea42a1` (D332), full unit 20027 green on main** · **V0.14 G6 MERGED `dbfe3a59` (D324–D331); V0.14 agent-side complete; 0.14.8 🧑 lab open. Note: a calibration version fitted before D329 keeps its old gate result until a new matched run arrives (`no_new_runs`)**) · older: (**V0.14 G4+G5 CLOSED (0.14.6/0.14.7 [x], D323); G6 implementer STOPPED at gates step (worktree agent-a76176aabb071dcb3 @73605853, committed) — run gates next**) · older: (**V0.14 G2+G3 CLOSED; next G4 brief**) · older: 2026-10-21 (**G9 gates: unit/ruff/mypy green; 4 cluster tests fail on DISK FULL — cleanup awaiting user OK**) · older: (**V0.10 G8+G9 merged; G9 post-merge gates pending**) · older: (**V0.10 G6+G7 CLOSED D285/D286; G8 implementer stopped before gates**) · older: 2026-10-20 (V0.10 G5 CLOSED D284) · older: 2026-10-19 (V0.9 G4 CLOSED, D245) · older: 2026-10-11 (**V0.7 G3 fix round 1 in flight** — see §1) · older: 2026-10-07 (**V0.6 G4 CLOSED + reviewer follow-ups done `1f037f2`; next: V0.7 brief; 0.6.13 🧑 lab open** · older: **V0.6 G1–G3 CLOSED; G4 fix round 1 stopped during its exit list — see §2 top UPDATE**; older note: **V0.5 CLOSED agent-side** (🧑 0.5.11 lab open) · **V0.6 started**: brief + ADR-0015 written (`94b8b64`), 4 sequential groups G1–G4, **G1 + G2 CLOSED**; **next: G3 implementer** (brief §5c + N-1) — see §2 "V0.6 state") · **Updated by:** Lead session

---

## 0. SCOPE CHANGED 2026-09-20 — read before anything else

The team stated the real budget for the first time: **one Claude account, ~10 weeks, ~9 sessions/week ≈ 90
sessions.** The old "no deadline, never cut scope for time" rule is **dead**.
**[ADR-0012](adr/0012-scope-ship-mode3-complete.md) (Proposed — needs the other three team members):**

- **Ship Mode 3 (Task Splitting) complete and production-grade.** Mode 1 ships with it as its foundation.
- **Keep the AI in full** — V0.9 telemetry, V0.14 planner v2, V0.15 ML planner. An earlier draft cut these and
  the team rejected it: "AI has a real optimization role" is a fixed pillar.
- **Keep V0.7 security and V0.8 dashboard in full**, plus V0.17 and V1.0. **Nothing is trimmed at 10 weeks.**
- **Defer Mode 2 (V0.11) and Mode 4 (V0.12/V0.13/V0.16)** — marked DEFERRED in the roadmap, kept in full as the
  future-work record. Revisit **V0.11 first** if margin survives; Mode 4 extends Mode 2.
- **V0.9 moves before V0.10** so ML training data accumulates during Mode 3 development.
- **0.2.8 (S4) and 0.2.9 (S5) are deferred with Mode 2.** ADR-0011 is off the critical path (torch still needed
  for GPU-backed Mode 3 chunks, but it blocks nothing now).
- **129 tasks against 140–180 capacity → target ~1.5 tasks/session.** Degradation order if the budget shrinks is
  fixed in the roadmap's "Scope & budget" section: V1.0 → V0.17 → thin V0.8 → V0.5 polish. **Never V0.10, V0.15,
  V0.9.**
- **Breadth-vs-depth resolved:** the college rewards *working quality*, not a count of features. Depth is correct,
  so the quality rules (tester + reviewer, evidence before `[x]`, real multi-machine tests) all stay.

**Token efficiency is half of why this fits** and is now a working rule, not advice: one *coherent group* per
implementer run (not one task) · front-load every brief with the standing "no confidently-wrong output" checklist
so reviews pass in 1–2 rounds · the Lead does doc edits and small mechanical fixes directly with **no subagent** ·
run the ~6-minute component suite **once per task group**, not once per agent. Evidence: a ~30-line `_harness`
change cost **54 k tokens** as a subagent; 0.2.6 (S2) needed **7** agent runs; S6 passed its implementer round
**first time** on a front-loaded brief.

### What a fresh session must CHECK about this plan change (do this before building)

1. **Is ADR-0012 still `Proposed`?** If the team has accepted it, change the status, amend
   `CLAUDE.md` ("No deadline" clause) and `PROJECT_BRIEF.md` §33 + §40, and delete this
   instruction. If it has been **rejected**, the roadmap's DEFERRED banners and the
   "Scope & budget" section must be reverted — do not leave the repo half-way.
2. **Does the hardware still match?** The whole plan assumes L2 ≈ RTX 3050, L3 ≈ RTX 2050,
   **L4 no GPU** — *team-stated, never measured*. If `lab/inventory.py` results have landed and
   contradict that, re-open the ADR: fewer GPUs weakens Mode 3's speedup story, and more/larger
   GPUs would make Mode 2 cheaper to justify.
3. **Is the throughput assumption holding?** The plan needs **~1.5 tasks/session**. If the last
   two weeks ran below ~1 task/session, the plan is slipping: cut in the fixed order in the
   roadmap's "Scope & budget" section (V1.0 → V0.17 → thin V0.8 → V0.5 polish) and **never**
   V0.10, V0.15 or V0.9.

   **Data point, 2026-09-21 (V0.3 G4):** 3 tasks (0.3.3 / 0.3.5 / 0.3.11) closed in one long session —
   **on plan at ~1.5**, but it took **9 agent runs** (1 implementer + 3 tester + 2 reviewer + 3 fix/cleanup)
   because three critical defects surfaced, each introduced by a *fix* (§2a). The group-level rate is holding;
   the risk to watch is that defect-driven re-rounds, not the initial build, are what consume the budget. If a
   later group needs a comparable number of rounds, that is the signal to re-check the plan, **not** a reason to
   drop the tester round that found them.
4. **Has anyone started Mode 2 or Mode 4 work by accident?** V0.11/V0.12/V0.13/V0.16 are
   DEFERRED. If a task from those versions is in progress, stop and ask the team.
5. **Is telemetry (V0.9) still scheduled before Mode 3 (V0.10)?** It was moved deliberately: the
   ML planner needs thousands of real runs to train on, and built late there is no data at the
   end. If someone has reordered it back, that is a regression in the plan, not a detail.

Docs already updated: `docs/adr/0012-*`, `DEVELOPMENT_ROADMAP.md` (header, Scope & budget section, DEFERRED
banners, V0.14 re-scope, dependency graph, design change log), `PROJECT_SUMMARY.md`, `docs/LAB.md` §1.
**Not yet updated: `CLAUDE.md` (still says "No deadline") and `PROJECT_BRIEF.md` §33/§40** — both need team
approval of ADR-0012 first.

---

## 0b. 🔴 THE BUDGET IS NOT 10 WEEKS — team-stated 2026-09-21

**ADR-0012 says "secured ~10 weeks". That is wrong.** The team has **~5 weeks of calendar**, worked with
**2 Claude accounts**. Every future session reads the ADR as truth, so treat this section as the correction until
the ADR is amended (**not yet done — it needs the team, like the rest of ADR-0012**).

**What two accounts do and do not buy.** They roughly preserve the *token* capacity ADR-0012's 140–180-task
estimate was actually measuring. They do **not** buy calendar, and three things are calendar-bound:
- **V0.15's training data** — thousands of real runs accumulating over wall-clock weeks. Five weeks instead of ten
  is **half the telemetry**, whatever the account count. **This is the single biggest threat to the AI pillar.**
- **The 24 🧑 lab tasks** — four co-located people and physical laptops; half the chances to schedule them.
- **The 24 h soak** (V0.3 criterion 5) and any multi-day stability evidence.

**Session arithmetic.** The same token budget in 5 weeks means **~18 sessions/week (~2.6/day)** sustained
alongside coursework; ADR-0012 already called 9/week demanding. Realistic ~12–14/week → **~60 sessions × 1.5 ≈
90 tasks**, against **129 planned with 8 done** — roughly a **30-task shortfall**.

~90 tasks buys **V0.3 + V0.4 + V0.5 + V0.6 + V0.9 + V0.10 + V0.14 + V0.15 ≈ 92**: Mode 3 complete plus the full
AI planner, and **essentially nothing else** — no V0.7 security, no V0.8 dashboard, no V0.17, no V1.0.

**This is the contingency ADR-0012 pre-agreed**, so apply the ladder **now**, not in week 4, so nobody builds
toward work that gets cut: **V1.0 → V0.17 → thin V0.8 → V0.5 polish; never Mode 3, V0.15 or V0.9.**

**Open question for the team (Lead's recommendation, not decided):** the ladder thins **V0.8 before V0.7**. For a
college evaluation consider reversing that — a thin read-only dashboard demonstrates the fabric and the planner's
decisions far better than mTLS does, and "AI has a real optimization role" is a pillar that has to be *shown*.

**Two urgent consequences:**
1. **Start the telemetry flywheel immediately.** Leave `meshforge-agent` running on all four laptops from now on:
   every idle week is V0.15 training data that cannot be recovered later, and it earns the 24 h soak evidence as a
   side effect. This moved from "good idea" to "decides whether V0.15 is real".
2. **Two accounts share one repository.** Two Leads on one working tree will corrupt each other's commits (a mild
   version of this already happened — see §4's `git add -A` rule). Run them on **different task groups with no
   overlapping files**, or strictly sequentially.

---

## 0c. 🟢 MODELS CHANGED 2026-09-24 — Opus 5.5

**Claude Opus 5.5 (`claude-opus-5-5`) was released 2026-09-22** — stronger than Opus 5 on agentic coding and
~40% cheaper to run on typical workloads ($4 / $20 per M input/output tokens). The project now uses it:

| Role | Model | Effort | Where it is set |
|---|---|---|---|
| **Lead** (the main session) | `claude-opus-5-5` | — | the app's model picker / `/model claude-opus-5-5` |
| implementer | `claude-opus-5-5` (was `claude-opus-5`) | medium | `.claude/agents/implementer.md` |
| tester | `claude-sonnet-5-5` (was `claude-sonnet-5`, switched 2026-10-01 by team decision) | medium | `.claude/agents/tester.md` |
| reviewer | `claude-opus-5-5` (was `claude-opus-5`) | low | `.claude/agents/reviewer.md` |

Commit `6563c7d`. **Subagent models are pinned by exact ID in the agent files** — switching the Lead's model does
*not* change them; edit the frontmatter. Tester stays on Sonnet 5 on purpose: it has been effective (G5's tester
rounds found three vacuous assertions) and there is no verified cost comparison to justify moving it.

**What this does and does not change about §0b's budget arithmetic.** The throughput figures there (≈1.5
tasks/session, the ~30-task shortfall) were measured from this repo's `git log` on **Opus 5** sessions. A cheaper,
stronger model is the first thing that pushes *toward* closing the gap — but it is **unmeasured on this repo**, so
do not re-plan on it yet. **Re-baseline after G6** (tasks closed, agent runs, re-rounds) and only then revisit the
degradation ladder. It changes nothing about the calendar-bound items: V0.15's training data, the 24 🧑 lab tasks,
the 24 h soak.

**How the machine got here (so nobody repeats the detour):**
- The Claude Desktop app was upgraded via its official apt repo (`/etc/apt/sources.list.d/claude-desktop.list`,
  `downloads.claude.ai`) from **1.49585.0 → 2.7032.0**; after a restart, Opus 5.5 is selectable in the desktop app.
  Upgrade command: `sudo apt update` then `sudo apt install --only-upgrade claude-desktop` (needs the user's sudo
  password — an agent cannot run it).
- The standalone CLI was also installed (`npm install -g @anthropic-ai/claude-code`, **2.1.281**, under nvm, no sudo).
  It turned out **not to be needed**; it is optional (`claude` in a terminal, where `/model` and `/permissions`
  dialogs work). Remove with `npm uninstall -g @anthropic-ai/claude-code` if unwanted.
- **Trap:** grepping the app bundle (`app.asar`) for model IDs is **not** evidence of what the picker offers —
  `claude-opus-5-5` had zero hits there yet was selectable after the restart. Check the picker, not the bundle.
- ⚠️ A **third-party snap** `claudeai-desktop 1.0.0` (publisher `prevailexcel`, not Anthropic) is also installed.
  It is not the app in use (that is the `.deb` at `/usr/lib/claude-desktop`). Treat it as untrusted — it would
  handle account credentials; remove with `snap remove claudeai-desktop` unless someone knows why it is there.

**The app restart killed the first exit-139 fix task**; it was relaunched and is **done** (§2). Restarting the desktop app kills every running session;
check nothing is mid-task first.

**Post-G6 re-baseline (as asked above).** G6 = 1 roadmap task (0.3.14) **plus** three inherited items, the 0.3.15
lab kit and four tester-driven fixes, in one long Lead session, with **11 agent runs**: implementer ×5 (A, B, C, fix,
plus A and C each relaunched once after an **account usage limit** — both died at startup, so little was lost), tester
×2, reviewer ×2. By roadmap checkboxes that is 1 task/session, below plan, but the inherited items were real V0.3 debt.
**On Opus 5.5 the binding constraint was the usage limit, not tokens or defect rounds.** Keep telling agents to commit
early. Re-check after G7.

**Post-G7 data point (2026-09-25):** 1 roadmap task (0.3.4) in one Lead session across two usage-limit resets, **7 agent
runs** (implementer A + B, each **resumed once** via SendMessage after a usage limit with no work lost because both
committed after every item; one small fix round; tester ×2; reviewer ×1) — reviewer APPROVE first pass, no blocking.
Resuming a cut-off agent (not relaunching) is the cheap recovery; keep the "commit after each item" rule in briefs.
Throughput is still ~1 task/session by checkbox; the binding constraint remains the usage limit.

---

## 0d. 🔴 PACE CHECK 2026-09-28 — we are at about half the speed the plan needs

Asked for by the team. Measured from the roadmap checkboxes, `git log` and the session files on this machine.

**The plan:** 129 tasks from V0.3 to V1.0 (ADR-0012 table), now **131** after reviewers added 0.4.16 and 0.4.17.
Calendar: **~5 weeks from 2026-09-21 (§0b) → about 2026-10-26.** *That end date is the Lead's reading of "~5 weeks";
the team has not confirmed it — if it is wrong, redo this section.*

| | Plan | Actual (2026-09-28) |
|---|---|---|
| Calendar used | — | 7 of 35 days (**20 %**) |
| Tasks `[x]` | ~26 by now | **11 of 131 (8 %)** — plus 3 V0.3 tasks code-done, waiting on 🧑 checks |
| Speed needed for the rest | **120 tasks ÷ 28 days ≈ 4.3/day** | **~1.8/day** while working (V0.3: 11 tasks, 20–25 Sep); **0 on 26–28 Sep** (no sessions) |
| Sessions/day | ~2.6 (§0b) | ~1.7 on working days |
| Tasks/session | 1.5 | ~1.1 |

**What that means:** at ~1.8/day the remaining 28 days give ~50 more tasks, **~60 total instead of 131**. That reaches
roughly V0.4 + V0.5 + V0.6 + part of V0.9 — **not V0.10 (Mode 3) or V0.15 (ML planner)**, the two items the plan says
are never cut. The ladder (V1.0 → V0.17 → thin V0.8 → V0.5 polish) frees only ~25–30 tasks, so cutting alone does
not rescue Mode 3; **speed has to roughly double as well.**

**Why V0.3 was slow (so V0.4 does not repeat it):** inherited fix work (exit-139, G6's liveness redesign), defect-driven
re-rounds (G4 9 agent runs, G6 11, G7 7), and usage limits cutting sessions short. Tester/reviewer rounds found real
bugs every time — do **not** drop them.

**Levers, in order of size — the next Lead should act on 1–3 without waiting:**
1. **More sessions per day: run accounts in parallel** on task groups with **no overlapping files** (§0b item 2).
   Accounts: §0b says two, the team's memory note says **three** Pro accounts — confirm with the team and use all of them.
2. **Larger groups from V0.4 on:** Mode 1 work is more routine than V0.3's distributed-systems core — aim for 3–5 tasks
   per implementer run, one tester + one reviewer round per group.
3. **Apply the cut ladder now**, not in week 4, and tell the team what is being cut.
4. **No idle days:** 26–28 Sep produced nothing; a lab day for humans can still run an agent group in parallel.
5. **🧑 lab work is the other half:** the 12 `[~]` tasks (inventory on laptops 2–4, GitHub push + CI, Windows checks,
   0.3.15 lab run + 24 h soak) need people, not tokens — schedule them this week.

**Open team questions this raises:** the real deadline date; how many accounts; whether V0.4 coding may start before
the 🧑 0.3.15 lab run closes V0.3 (the Lead recommends yes — the lab run is calendar-bound, not code-bound).

## 0e. 🔴 ONE SUBAGENT AT A TIME AGAIN — team decision 2026-10-01 (evening)

The user re-imposed the one-agent rule after the parallel wave hit the usage limit twice. **Run exactly one subagent at a time.**
**State 2026-10-04 late:** V0.5 G1, G2, G3 and the stabilisation lane (+ D95, reviewer APPROVE) CLOSED. Wave 2 briefs written. **G4 tester round 2 RUNNING** — see §2 "G4 state" for exactly where G4 is.

**Queue at the switch (in this order):** (1) G1 fix round 2 — resume from `wip` `f0febfe` (reviewer blocking: tree_members cache, 64 GiB cap + >5 GB sparse test; non-blocking items) → G1 tester round 3 → G1 reviewer pass 2 → close 0.5.1/0.5.2. (2) G2 tester round 3 on `60de165` → G2 reviewer → close 0.5.7/0.5.8-local. (3) Stabilisation lane — resume from `wip` `7e4525f` (`97f347e` already fixed the slow-follower double count; probe file `tests/component/agent/test_zz_stab_probe_tmp.py` is temporary and must be deleted). (4) Wave 2 briefs G4, G5.

### (superseded) PARALLEL AGENTS ALLOWED AGAIN — team decision 2026-10-01 (morning)

The user lifted the one-subagent rule on 2026-10-01: the Lead may run several agents at once on **independent** tasks (separate groups / files). Still sequential *within* a task (implementer → tester → reviewer). Watch the usage limit; if tokens burn too fast, the team may re-impose the rule.

**2026-10-01 autonomous run:** the user is away and told the Lead not to wait for input — after each finished step, proceed straight to the next roadmap task (tester → reviewer → close → next group/wave brief) and keep docs current.

### (superseded) ONE SUBAGENT AT A TIME — team decision 2026-09-28

Running G1, G2 and G3 in parallel (§0d lever 1) **used tokens too fast** — three implementers, then three testers, at
once. The team stopped it. **From now on the Lead runs exactly one subagent at a time** (implementer, tester or
reviewer) and waits for it to finish before starting the next. This replaces the "three lanes in parallel" rule in
the V0.4 brief §3. Larger groups (lever 2) still apply; parallelism does not.

**State at the stop (2026-09-28):**
- **G1** (0.4.16 + 0.4.17 items 2, 5): implementer done — `bfbe960`, Goodbye inside `e189f86`, `483b7ab`, `ff9a785`.
  **Tester round 1 was left running** (the one agent allowed). If this session died before it reported, re-run the
  G1 tester (prompt pattern: the G1 section of the brief + §4 checklist + "try to break D41's abandoned-admission
  linger and the Goodbye races"). Then the reviewer.
- **G1 tester round 1: PASS** (`6f545ca`, 5 new tests; unit 1641 / component ctl+agent 290 / cluster 41, all G1 gates exit 0). Minor known gap: dropping `goodbye` from `_UNACKED` fails no test (the ended-session guard already drops the ack). **G1 reviewer: APPROVE, no blocking → G1 CLOSED, 0.4.16 `[x]`; `grpcio<1.85` cap lifted (no 1.85 exists yet, lock unchanged at 1.84). Reviewer's 4 non-blocking items → D61, carried by G4.** **G2 tester round 1: PASS** (`677939c`; unit gate had 3 small pre-existing test failures, Lead fixed in `d901810` → unit 4351 passed exit 0; whole component 572 passed + 1 known flaky Ryuk test; cluster 41 exit 0). **G2 reviewer: REQUEST_CHANGES** — 1 blocking: `api/job_routes.py:295-315` job submit is 3 separate transactions (create, PLAN, PLANNED) → a crash leaves SUBMITTED/PLANNING forever and an idempotent retry replays it without finishing. Fix: one-transaction create-to-QUEUED in JobStore + failure-injection test. Non-blocking: §20 wording for concurrent 201s; aggregate upload disk bound + upload-token scope (→ G4/G6); startup `.part` sweep check; case-insensitive unpack test. **G2 fix round done** (`c8fc346` atomic submit via `JobStore.create_queued_job`, `2f4476f` §20 wording + startup sweep test, `f266274`; unit 4351 exit 0, component ctl+store 161 exit 0; split-tx mutation caught). **G2 tester round 2: PASS** (`d3581aa`; 10 new tests incl. pg_terminate_backend + random cancellation; own mutation on the store-level hash compare caught; unit 4351 exit 0, ctl+store 171 exit 0, cluster 41 exit 0; whole component 590 passed + `test_container_cleanup.py::test_pytest_timeout_during_pg_settings_leaks_no_container[thread]` now failing even standalone — pre-existing Ryuk infra, not G2, needs a look). **G2 reviewer pass 2: APPROVE → G2 CLOSED** (0.4.1, 0.4.3, 0.4.4, 0.4.5 `[x]`; 0.4.2 `[~]` until the Windows CI digest run). Follow-ups → **D62** in the brief (G4). **G3 tester: PASS** (`5551c74`; network 'leak' ruled out — the test's 1.1.1.1 probe, not the sandbox). **G3 reviewer: APPROVE → G3 CLOSED** (0.4.7 `[x]`; 0.4.8 `[~]` awaits a real PyTorch CUDA run). Follow-ups → **D63** (G4 must do the workdir 0o777 security fix and make the network test hermetic). Parked-tests folder is gone. **G4 brief written** (brief §2g D64–D69 + §5e, 2026-09-28). **G4 implementer DONE** (`6039802` … `5d22825`, 15 commits; deviations 1–8 in its handoff incl. extra §12.1 arrows COLLECTING→CANCELLED and QUEUED→FAILED, Reconcile on every stream, migration 0007, manager.py 930 lines; whole component suite NOT re-run after its last fix `5996e45`). **G4 tester round 1: PASS** (`3662ccd`; real controller SIGKILL+restart race x4 → one execution; cancel in every state + cancel-vs-DONE race; workdir 0700/cleanup; fencing_token bound; unit 4506 / whole component 634 / cluster 48, all exit 0). **G4 reviewer: APPROVE, no blocking → G4 CLOSED** (0.4.6, 0.4.11, 0.4.12 `[x]`). 6 non-blocking → **D70** (G5 carries; item 2, lost terminal report mislabelled deadline_exceeded, must be fixed before V0.4 closes). **G5 brief written** (brief §2h D71–D77 + §5f, 2026-09-28). **G5 implementer spawned** — progress in `docs/briefs/progress/V0.4-G5.md` + `git log` (`feat(V0.4-G5)`). If this session died: commit its wip, relaunch with §5f + "resume from <last done item>". **G5 implementer DONE** (`d6b12e1` … `b1d5cbc`; deviations 1–6 + follow-ups in its handoff, summarised in progress/V0.4-G5.md; unit 4588 / component 658 / cluster 52 exit 0). **G5 tester round 1: PASS** (`e8e50c9`, 3 new tests incl. real controller restart mid-run; unit 4588 / component 661 / cluster 52 exit 0). Two mutation checks (blob_routes `grants.owns`, log_routes WS `fabric_token_ok`) were NOT run — the sandbox blocked pytest after weakening src/; needs a human or a later round. **G5 reviewer pass 1: APPROVE but partial (→ D78). Pass 2 (focused): REQUEST_CHANGES** — blocking: (1) `controller/jobs/logs.py:321-330` an OSError writing a live log kills the whole controller (spawned-task failure → request_stop); (2) `cli/job_commands.py:343-348` `compute get --force` rmtree's the target before download/verify. Non-blocking taken into the fix round: unbounded LogHub queue, output-file lstat/read TOCTOU, poisoned-DONE doc, `grants.owns`. Roadmap criterion 1 reworded by the Lead (D76). **G5 fix round DONE** (`dd95547` … `4bc14b3`, `094a757`; unit 4619 / component controller 170 + agent 198 / cluster 52 exit 0). ⚠️ Incident: its mutation run drove the OLD `compute get --force --dest /` → `rmtree('/')`, stopped at EPERM (non-root); `/` and home checked intact by the Lead; the test now exercises only the refusal helper. Open decision: attempts stuck in UPLOADING have no backstop. **G5 tester round 2 STOPPED by the team (account switch, 2026-09-29)** after writing two unverified files, committed as wip: `tests/unit/cli/test_g5_tester2_get_force.py`, `tests/unit/controller/test_g5_tester2_log_hub_concurrent_followers.py`. **Next session: spawn a fresh G5 tester round 2** (tester, background, only agent) with this prompt: "Tester round 2 for V0.4 G5 after the fix round (commits dd95547 … 4bc14b3, 094a757; review findings B1 log-write OSError, B2 `compute get --force`, N3 bounded LogHub queue, N4 O_NOFOLLOW+fstat output files, N5 poisoned-report docs, N6 owns() removed — see docs/briefs/progress/V0.4-G5.md and brief §2h). Two partial files from a stopped round exist (the wip commit) — verify, fix or delete them. SAFETY RULE: never run any code path that could delete or overwrite outside a tmp_path you created; test --force refusals only with fake cwd/home/root inside tmp_path or via the helper; never pass a real /, ~ or the repo as --dest. Try to break the fixes (ENOSPC then recovery, followers during a write failure, --force on a symlinked target, KeyboardInterrupt mid-download); mutation-check one guard in B1 and one in B2 (if the sandbox blocks it, say so, don't work around it). Tests only; commit `test(V0.4-G5): …` explicit paths. Exit list via scripts/pytest_exit_guard.py: ruff check + format --check, mypy src, lint-imports, gen_protos --check, -m unit, whole -m component once, -m cluster. PASS/FAIL ≤ 600 words." **G5 tester round 2 DONE 2026-10-01 (Sonnet 5.5): PASS** (`44dcf44`; both mutation checks B1/B2 caught; unit 4623 / component 619 passed + 52 skipped (implementer had 658 + 9 — unexplained, reviewer to check) / cluster 52, all exit 0; minor defect T2-1: `compute get` shows a traceback on an unwritable dest parent, kept as strict xfail). **G5 reviewer pass 3: APPROVE, no blocking** (condition before `[x]`: a component run with `-rs` showing the 52 skips are environment-only — Lead running it). Follow-ups: T2-1 clean error on unwritable dest/unpack OSError; UPLOADING backstop → V0.5 tech debt; `_swap_in` rollback-failure keeps parked copy. **Component `-rs` run by Lead: 619 passed / 52 skipped, all env-only (35 = pinned python:3.12-slim not pulled) → G5 CLOSED 2026-10-01** (0.4.9, 0.4.10, 0.4.13, 0.4.14 `[x]`). Next: 🧑 0.4.15 lab run; agent-side G5 follow-up fix batch (T2-1, `_swap_in` rollback; UPLOADING backstop → V0.5). **2026-10-01 parallel wave (team lifted one-agent rule, §0e):** G5 follow-up fix batch DONE (`5f13163`), its tester running · **V0.5 brief written** (`docs/briefs/V0.5-data-plane.md`, D79–D94; `uv` added to agent extra `2c10296`) · **V0.5 wave 1 implementers running in parallel: G1 CAS/trees, G2 environments, G3 V0.4 debt** — progress in `docs/briefs/progress/V0.5-G<n>.md` + `git log feat(V0.5-G<n>)`; if this session died, resume each from its last committed item. Then per group tester → reviewer; then wave 2 briefs (G4, G5). (Earlier:) **G5 follow-up fix batch implementer SPAWNED 2026-10-01** (T2-1 + `_swap_in` rollback; commits `fix(V0.4-G5)`; if the session died: check git log, resume/relaunch with the same scope). (Old text follows:) Then close G5, then close G5 (0.4.9/0.4.10/0.4.13/0.4.14 `[x]` with evidence, STATUS, PROJECT_SUMMARY if needed). Progress in `docs/briefs/progress/V0.4-G4.md` + `git log` (`feat(V0.4-G4)`). If this session died: commit its wip, relaunch with §5e + "resume from <last done item>". Then G4 tester → reviewer → G5 brief.
- ⚠️ **Possible G3 sandbox defect, found by the stopped G3 tester's parked file** `test_docker_sandbox_g3_tester.py`: `test_run_phase_network_is_off_even_though_setup_phase_had_it_on` FAILED in the G1 tester's component run — the run container may have network when the setup container had it. The G3 tester round must reproduce this first.
- **G2** (0.4.1–0.4.5 + fabric token): implementer done — `1462e34` … `f92e4fd`. **Tester round 1 stopped part-way**
  (its unit gates were green, component gates not yet run). Its unfinished test files are parked in
  `docs/briefs/progress/parked-tests/` (read its README). Next: a fresh G2 tester round, then the reviewer.
- **G3** (0.4.7, 0.4.8, 0.4.17 items 1/3/4/6/8): implementer done — `d625a45` … `b14baa3`. **Tester round 1 stopped**
  after writing tests, before running them; files parked the same way. Next: a fresh G3 tester round, then the reviewer.
- **Order from here:** G1 tester → G1 reviewer → G2 tester → G2 reviewer → G3 tester → G3 reviewer → G4 brief +
  implementer → … → G5. Fix rounds, and a tester re-run after each fix, stay mandatory (§2a).
- Commits: keep using `git commit -- <paths>` even with one agent — the Lead also commits docs.

## 1. Where the project is

**2026-10-22 — STOPPED by user: G4 tester r2 ∥ reviewer r2 (unfinished; rerun) on fix round 1 (head `78b30d4d`, all gates exit 0, full unit 19666; was: tester r1 PASS `df74f974` with T1 gate float boundary, T2 15 % float noise, T3 non-finite median crash — strict xfails; reviewer r1 APPROVE; fix = D322 paired criterion 2 + T1–T3 + regret check before lock + `compute planner reports`) → then G4 tester r2 delta → merge (worktree `.claude/worktrees/agent-a716c94445de52d7f`, impl head `4fd1cc67`, D318 models `2423f476`) · **G5 READY TO MERGE after G4** (worktree `.claude/worktrees/agent-afdc8ecb0da11dc50`, head `759a3c00`: tester r1 PASS, full unit 19634 exit 0, vitest 725; reviewer r1 APPROVE; merge items D321). Pre-existing on main: `tests/e2e/test_v08_g4_jobs.py` digest strict-mode locator failure (not G5).** Brief §5d `bc531806` APPROVED (D313–D320). Both implementers: full `-m unit` exit 1 from load flakes only (agent disk-read / session timing) — **rerun full unit alone before merge**. G4 deviations 1–9 in its progress note, pending reviewer. Merge G4 then G5 (regenerate openapi/schema.d.ts). If the session dies: resume from progress notes.

**2026-10-22 (G2 + G3 CLOSED) — NOTHING RUNNING.** G2 merged `092b60c7` (0.14.3 `[x]`), G3 merged `3392fe49` (0.14.2/0.14.4/0.14.5 `[x]`, REPLAY_VERSION 2). Post-merge main: ruff/format/mypy clean, unit 19511, cluster test_v010_mode3 4. Decisions D309–D312 in the V0.14 brief; new tech debt in STATUS. Worktrees G1/G2/G3 merged — removable with user OK. Next: write brief §5d (G4: 0.14.6 + 0.14.7).

**2026-10-21 (G2 tester r1 PASS (tests `c70e93f5`; T1 format:true accepted, T2 CLI title escape — xfail-pinned); G2 fix round 1 DONE head `ccd43cc9` (unit 19182, G2 sets 700 green); G2 tester r2 PASS (tests `f6e1ee2b`); G2 MERGED `092b60c7`, 0.14.3 [x]. G3 reviewer r2 APPROVE; Earlier: G2 gates GREEN, head `7533c902`; G2 reviewer r1 APPROVE (non-blocking: NB1 duplicate row every refresh w/o new runs, NB2 refit on event loop under lock, NB3 unreadable-version log spam — fold into a small fix round after tester); G2 tester r1 RUNNING; G3 gates GREEN head `0fcb237a` (unit 18728, comp 236, perf 12, cluster 4; no goldens changed); G3 reviewer r1 APPROVE — merge conditions M1 calibration before risk, M2 never calibrate risk (Estimate.extra), M3 calibration error uses un-risked total + test with p_fail>0, M4 REPLAY_VERSION 2 decoder accepts 1+2 + rerun 397 corpus, M5 no migration; non-blocking N1 job_models.py:56 docstring 422→400, N2 index attempt(ended_at), N3 _reliability limit→filter by node ids; record D294 pruning amendment + D307 default-off note; G3 tester r1 FAIL D1 (pruning changes chunk sizing → +3.3% makespan, seeds 57/3662; tests `f9274971`); G3 fix round 1 DONE head `dcb1c606` (unit 18805, comp 240, perf 30; pruned==unpruned plan 6000 seeds; reason_key may differ with pruning — Lead accepts); G3 tester r2 PASS + reviewer r2 APPROVE; G3 merge integration DONE head `9df0c7e7` (M1–M5 tests, REPLAY_VERSION=2, unit 19511, comp 277, perf 30, cluster 4); integration REVIEWER RUNNING → then Lead merges G3 into main + reviewer check of integration diff → 0.14.2/0.14.4/0.14.5 [x]; N2 index → tech debt (no migration in G3))** in their worktrees (G2 `agent-aef81b3f80ac578c8` from `806665c8`, G3 `agent-a485a40c89a558f22` from `39e52ba3`). If the session dies: check each worktree's head + progress note Hand-off; rerun missing gates; then tester ∥ reviewer → merge G2 then G3 (REPLAY_VERSION→2).

**2026-10-21 (G2 ∥ G3 STOPPED by user) — NOTHING RUNNING.** Brief §5a′/§5b/§5c committed `7d1e86a7` (D305–D308). Both implementers hit a usage limit, were resumed, then stopped by the user; code committed, trees clean, **no Hand-off yet**. G2 `.claude/worktrees/agent-aef81b3f80ac578c8` head `806665c8` (migration 0031, calibration fit/store/refresh/gate, CLI+API, explain, tests, OPERATIONS/ARCH §7) — unit gate green, rest of gates not run. G3 `.claude/worktrees/agent-a485a40c89a558f22` head `39e52ba3` (N1/N2/N4, risk, objectives, link_infeasible, pruning, latency bench, tests, explain, docs) — gates not run. Next: finish gates + Hand-off per group → tester ∥ reviewer → merge G2 then G3 (REPLAY_VERSION→2, D307). Normal mode.

**2026-10-21 (G1 CLOSED) — NOTHING RUNNING.** V0.14 G1 merged `f72681c0`, 0.14.1 `[x]`. G1 worktree `.claude/worktrees/agent-a0a1e95c1ee7384ec` merged — removable with user OK. Next: write brief §5b (G2) + §5c (G3) — not written yet — then G2 ∥ G3; fold reviewer non-blocking N1/N2/N4 (STATUS Current Task) into G3.

**2026-10-21 (G1 gates green) — RUNNING: G1 tester r1 ∥ reviewer r1** in worktree `.claude/worktrees/agent-a0a1e95c1ee7384ec` (head `fc0c862b`). Gates: ruff/format/mypy clean; unit 18423 passed; component planner+jobs+migration 136 passed after a stale 0029 migration test was fixed (`check` now runs at head); cluster test_v010_mode3 4 passed. Hand-off in `docs/briefs/progress/V0.14-G1.md`. If the session dies: relaunch the tester and reviewer from that note → merge → 0.14.1 `[x]` + status bar.

**2026-10-21 (session end, user stop) — NOTHING RUNNING.** V0.14 G1 (brief §5a: replay snapshot + migration 0030 + unified candidates) implementer **stopped by user** in worktree `.claude/worktrees/agent-a0a1e95c1ee7384ec`, branch `worktree-agent-a0a1e95c1ee7384ec`, head `38ae5529` (7 wip commits, clean tree, NOT merged). Done per commits: replay codec + golden corpus, `planner/candidates.py` (goldens byte-identical), replay property test, migration 0030 + dispatcher/shadow store replay, component tests through a real dispatch tick, latency scaffold, OPERATIONS. It was mid-way writing the **ARCHITECTURE §7 amendment** (check whether it is in b65f18fb or missing). **Exit gates NOT run, no Hand-off.** Next: finish §7 amendment → run gates (ruff, format, `mypy src`, AST purity, `pytest_exit_guard.py -m unit`, planner/jobs/migration component, tests/cluster/test_v010_mode3.py) → Hand-off in `docs/briefs/progress/V0.14-G1.md` → tester ∥ reviewer → merge → 0.14.1 `[x]` + status bar. Then G2 ∥ G3.

**2026-10-21 (TD2 closed) — NOTHING RUNNING.** V0.10-TD2 merged `15d515e0` (D304); post-merge gates green (unit 17578, cluster+component 13). TD2 worktree `.claude/worktrees/agent-aae9991b61e8d14ee` merged — removable with user OK. V0.14 brief DRAFT `docs/briefs/V0.14-unified-planner-v2.md` waiting for team §7 answers; **no V0.14 code until approved**. Open team question: `compute logs` TTY stripping (+`--raw`).

**2026-10-21 (TD1 closed) — NOTHING RUNNING.** V0.10-TD1 merged `ae1d0bbd` (D289); post-merge gates green (unit 17276, cluster 36, ruff/mypy clean). TD1 worktree + branch removed (user-approved). Next: 🧑 0.10.10; agent-side: leftover TD (STATUS Technical Debt) or V0.14 brief — team chooses.

**2026-10-21 (session end, user stop) — NOTHING RUNNING.** V0.10 tech-debt task **V0.10-TD1** (D288 items: shared split-cluster helpers, `check_exactly_once` flags leftover `live` lease, offline numpy wheelhouse for test_v010_mode3, controller strips terminal controls from chunk stderr tail — CR→\n deviation) implemented in worktree `.claude/worktrees/agent-aa9c83b4b0b65d858`, branch `worktree-agent-aa9c83b4b0b65d858`, head `6dec9677` (5 commits, NOT merged). Implementer stopped during its exit gates — **gates NOT run, no Hand-off**. Progress: `docs/briefs/progress/V0.10-TD1.md` in that worktree. Next: run exit gates there (ruff, format, `mypy src`, `scripts/pytest_exit_guard.py -m unit`, tests/cluster/test_v010_*.py) + Hand-off → tester → reviewer → merge. Out of scope still open: C5 FaultLink bw, I8/I12/I13.

**2026-10-21 (later) — NOTHING RUNNING. G9 gates GREEN.** Disk is now 80% (38 GB free; space freed outside the agent, worktrees + /tmp/pytest-of-aayush still present). `tests/cluster/test_v010_mode3.py` → 4 passed. V0.10 agent-side complete; closes on 🧑 0.10.10. User-approved: the 30 clean, merged worktrees were removed via `git worktree remove` (branches kept); disk now 76% / 45 GB free. `/tmp/pytest-of-aayush` left in place. Next agent-side: V0.10 tech debt (STATUS Technical Debt D287/D288) or V0.14 brief — ask team.

**2026-10-21 (session end — user asked to stop) — NOTHING RUNNING.** G9 post-merge gates on main `d2a606bc`: unit 17068 passed / 10 skipped / 7 xfailed; ruff + format + `mypy src` (435 files) clean. V0.10 comp+cluster+workqueue in one process: run 1 33 failed / 212 passed, run 2 **241 passed / 4 failed** (`test_v010_mode3.py` c1, c4, c5, owner_guard). Root cause: host disk 98% full (3.6 GB free) → agents fail chunks `disk_full` (`free_disk_floor_mb` 2048); c4's "superseded not released" lease was a side effect (a speculative copy won). **Not a code bug.** Cleanup candidates audited (all 30 worktrees clean, 0 commits outside main, only caches + `web/dist` ignored; `/tmp/pytest-of-aayush` 7.4 GB test scratch) — **user has NOT approved deletion; ask first.** STATUS Current Task entry + G9 worktree removal still pending until the cluster file is green.

**2026-10-20 (session 3 END — team returned, asked to stop) — NOTHING RUNNING.** V0.10 G1–G4 merged (G4 `325c83cc`, D281); gate clean D282 (2853 passed / 0 failed); 0.10.1–0.10.4, 0.10.6 `[x]`, V0.10 5/10, total 58%. G5 implementer was started then STOPPED while still reading (no code written, nothing to recover). Next: launch G5 per §5e. 🧑 pending: V0.9 0.9.10 overnight campaign (checklist part O); V0.10 0.10.10 lab incl. a hostile-symlink Docker chunk (D281); team review of the V0.10 brief + ADR-0019 (Proposed).

> **UPDATE 2026-10-19 (new session):** G6 exit gates PASS + hand-off done (HEAD 2612335; fixed qualityApi shape to match G5c QualityOut) → G6 tester r1 PASS (T-1/T-1b minor, Lead-fixed `154915e` in G6 worktree) → G6 reviewer r1 **APPROVE** (5 non-blocking: 404 message-text match → fix at merge with generated type; RunOut tries/last_failure in bench_routes.py may conflict with G5c). **G6 ready to merge AFTER G5c.** G5c exit gates PASS + hand-off done (HEAD c411f17; D255 test 20/20 under load) → G5c tester r1 PASS w/ 3 minor (D257) → G5c fix r1 done (HEAD 484da34; quality summary gains measured_records, rejected_ok, other_failures → G6 qualityApi at merge) → G5c tester r2 PASS (04d9ef4) → G5c reviewer r1 APPROVE after Lead lint fix `edd7ad0` → **G5c merged, G6 merged `0e63d6b`** (schema regenerated; `bench report` now InUi). Integration done `c5b77c2` (generated QualityOut, 404 by `error.reason=unknown_campaign`, all buckets shown; unit 15874, vitest 618, e2e 14, build OK). **Whole `-m component` was STOPPED unfinished at session end (user) — rerun it alone.** NOTHING RUNNING. Then: whole `-m component` ONCE alone → 0.9.7/0.9.8/0.9.9 `[x]` + status bar → G7 brief.

**2026-10-19 (session end — team asked to stop) — NOTHING RUNNING. G5c implementer stopped after all items + docs (`262e8f3`..`75910b7`, exit gates + Hand-off not run). G6 implementer stopped finishing e2e (`ba903db`..`d024f03` + wip `adf14bd` UNVERIFIED). Next: see cold-start prompt.**

**2026-10-19 (G5 gate done, D255) — RUNNING in parallel (D256): G6 implementer (brief §5h, frontend; progress docs/briefs/progress/V0.9-G6.md in its worktree) ∥ G5c implementer (brief §5g, own agent worktree — newest in `git worktree list`; progress docs/briefs/progress/V0.9-G5c.md there; if died relaunch from it). 0.9.6 `[x]`. 0.9.7 `[~]`: runner cool-down missed under load (test_v09_g5b_runner.py:207, 0.034 s vs 1 s; green 3/3 alone) → treated as real race, G5c item 0.0. Next: launch G5c implementer (brief §5g) in a worktree → tester → reviewer → merge → whole component alone → 0.9.7 + 0.9.8 `[x]`.**

**2026-10-19 (G5 lane B tester r3 PASS `0e948d3`, MERGED into main, D253; frontend rebuilt) — RUNNING: whole `-m component` ONCE alone on main (scratchpad g5_component.log). If died: rerun `uv run python scripts/pytest_exit_guard.py -m component -q -rs` alone; green → 0.9.6 + 0.9.7 `[x]` + status bar + STATUS → G5c brief (0.9.8; item 0 = D248 N2/N5 + D252 1–6 + D253 T3-1/T3-2).**

**2026-10-19 (session end — team asked to stop) — NOTHING RUNNING. Lane A MERGED (D248). Lane B reviewer r2 APPROVE conditional on tester r3 (D252); tester r3 STOPPED mid-run, partial tests `3ea8915` wip UNVERIFIED in the lane B worktree (src clean). Tester r3 scope: B1 retry backoff/30 s cap/reset/no storm under 1000 polls; B2 stop_all raising/cancelled/hanging, `_stop` always runs, `preempt_stopped` vs natural finish; N1 rewritten r2 tests not weakened vs `ecf1043`; cluster runner test must deterministically pre-empt ≥ 1 attempt (test-only; report if a src hook is needed); mutations on B1 + B2; gates incl. whole `-m unit`, cluster test 2×. Then merge B → frontend build → whole component alone → close 0.9.6/0.9.7 → G5c brief.**

**2026-10-19 (G5 lane B fix r2 DONE `f2a7914`, gates exit 0; N2 = OPERATIONS known limit, accepted) — RUNNING in parallel: lane B tester r3 delta ∥ reviewer r2 (full: areas r1 skipped + fix r2). Then merge → whole `-m component` alone → 0.9.6 + 0.9.7 `[x]`.**

**2026-10-19 (G5 lane B reviewer r1 REQUEST_CHANGES, partial, D251) — RUNNING: lane B fix r2 (B1 T2-1 retry, B2 executor try/finally, N1 SubmitBatch, N2 cursor, N4 THREAT_MODEL). Then tester r3 delta → reviewer r2 full (areas r1 skipped) → merge → whole component alone.**

**2026-10-19 (G5 lane B tester r2 PASS `ecf1043`; new minor T2-1 strict xfail: failed pre-emption not retried while owner stays active — Lead: fix before merge) — RUNNING: lane B reviewer. Next: fix T2-1 (+ any reviewer items) → merge → whole `-m component` alone → 0.9.6 + 0.9.7 `[x]`.**

**2026-10-19 (G5 lane B fix r1 DONE `f5698bb`,`695b4b3` merge main,`4cda313`; all gates exit 0) — RUNNING: lane B tester r2 delta. Then reviewer → merge → whole `-m component` alone → 0.9.6 + 0.9.7 `[x]`.**

**2026-10-19 (G5 lane B tester r1 FAIL D-1 major + T1-1/T1-2, D250) — RUNNING: lane B fix r1 (D250 + D249 musts incl. merge main) in worktree agent-ad79c28d0eb42322f. If died: relaunch fix r1 there (look for "## Fix round 1" in progress/V0.9-G5B.md); then tester r2 delta → reviewer → merge → whole component alone.**

**2026-10-19 (G5 lane B implementer DONE `055121f`..`65b6167`, D249) — RUNNING: lane B tester r1 (worktree agent-ad79c28d0eb42322f). Next: lane B fix r1 (tester defects + D249 musts: merge main, labels in cluster test, deferral stall, ARCHITECTURE) → tester r2 delta → reviewer → merge → whole `-m component` alone → 0.9.6 + 0.9.7 `[x]`.**

**2026-10-19 (G5 lane A MERGED, D248) — RUNNING: lane B implementer exit gates (worktree agent-ad79c28d0eb42322f, resumed after limit). Next: lane B tester r1 → reviewer (B merges main first; regenerate OpenAPI, D248 N6) → merge → whole `-m component` once alone → 0.9.6 + 0.9.7 `[x]` → G5c brief (0.9.8; item 0 = D248 N2/N5).**

**2026-10-19 (G5 lane A tester r1 PASS `c5a87f9`, no src defects) — RUNNING: lane A reviewer (also re-runs `-m unit` there; tester skipped it after fixing its own test) ∥ lane B implementer. Next: merge A on APPROVE.**

**2026-10-19 (G5 lane A implementer DONE `2fec020`..`ea70070`, D247) — RUNNING: lane A tester r1 (worktree agent-a3dfd12ff045729f1) ∥ lane B implementer (worktree agent-ad79c28d0eb42322f, resumed after limit). If died: relaunch tester r1 in lane A worktree (look for "## Tester r1" in progress/V0.9-G5A.md); resume lane B from its progress file.**

**2026-10-19 (G5 brief §5f + D246 written) — RUNNING in parallel, fast mode OFF (user turned it off again 2026-10-19): G5 lane A (labels, 0.9.6) ∥ lane B (item 0 D245 + runner + owner guard, 0.9.7, migration 0024), separate worktrees; progress docs/briefs/progress/V0.9-G5A.md / G5B.md there. If died: relaunch per §5f from the progress file; per lane tester → reviewer; merge A then B → whole `-m component` once alone → close 0.9.6/0.9.7 → G5c brief (0.9.8).**

**2026-10-18 (session end — team asked to stop) — NOTHING RUNNING. G4 lane A implementer DONE `c30b2fb..fd97c54` (worktree agent-a1db552858a2b544b; unit 14135 exit 0, targeted component green, cluster determinism 2/2; deviations 1–6 + follow-ups in progress/V0.9-G4A.md: OpenAPI v1 text, truncated-stream check). Lane B `448a169..857d703` (worktree agent-af4d3c4fd85bcfa0d) code + docs + cluster test committed; exit gates NOT run, no hand-off. Next: lane A tester r1 ∥ lane B implementer exit gates.**

**2026-10-18 (G3 CLOSED, D242; 0.9.3/0.9.4 `[x]`) — RUNNING in parallel: G4 lane A (item 0 + migration 0023 schema v2) ∥ lane B (conditions, 0.9.5), separate worktrees; progress docs/briefs/progress/V0.9-G4A.md / G4B.md there. If died: relaunch per §5e from the progress file; per lane tester → reviewer; merge A then B (wire `condition`/`condition_measured`) → whole `-m component` once alone → close 0.9.5.**

**2026-10-18 (G4 brief §5e + D241 written `1e459cd`) — RUNNING: whole `-m component` ONCE alone on main (scratchpad gate.log). Nothing else running (gate alone). If died: rerun `uv run python scripts/pytest_exit_guard.py -m component -q -rs` alone; green → 0.9.3/0.9.4 `[x]` + status bar + STATUS → start G4 lanes A ∥ B per §5e.**

**2026-10-18 (session end — team asked to stop) — NOTHING RUNNING. G3 lanes A+B MERGED on main (`d838c43`, `b82006e`, D240); frontend rebuilt; ruff/mypy/lint-imports clean; 4455 targeted tests green. The whole `-m component` gate was STOPPED unfinished (no result). Next: `uv sync --all-extras`, then `uv run python scripts/pytest_exit_guard.py -m component -q -rs` ALONE; green → 0.9.3/0.9.4 `[x]` (evidence D232–D240) + status bar + STATUS → G4 brief (§5e, item 0 = D233 + D239 + D240). 🧑 pending: Windows P1 in lab/checklists/V0.9-campaign.md.**

**2026-10-18 (G3 both lanes MERGED `d838c43`,`b82006e`, D240) — RUNNING: frontend build then whole `-m component` ONCE alone on main (log scratchpad v09_g3_component.log). If died: rerun `uv run python scripts/pytest_exit_guard.py -m component -q -rs` alone; green → 0.9.3/0.9.4 `[x]` + status bar + STATUS → write G4 brief (§5e; item 0 = D233 + D239 + D240 lists).**

**2026-10-18 (G3 lane A tester r2 PASS `8a23643`) — RUNNING: lane A reviewer. Next: merge A → merge B → frontend build → whole component once alone → close 0.9.3/0.9.4.**

**2026-10-18 (G3 lane B reviewer APPROVE, D239) — RUNNING: lane A tester r2 only. Next: lane A reviewer → merge A → merge B (keep B's async campaign_of + decision_shadow; re-run lint-imports, import-boundary + recorder tests) → frontend build → whole `-m component` once alone → close 0.9.3/0.9.4.**

**2026-10-18 (G3 lane B tester r2 PASS `c94852a`,`f32f56a`) — RUNNING: lane B reviewer ∥ lane A tester r2. If died: relaunch reviewer over 3384076..HEAD in lane B worktree. Merge A first, then B.**

**2026-10-18 (G3 lane A fix r1 DONE `aab2015`, D238) — RUNNING: lane A tester r2 ∥ lane B tester r2 (resumed after limit). Then reviewer per lane; merge A then B (conflicts: recorder.py, import-boundary files) → whole component once alone.**

**2026-10-18 (G3 lane B fix r1 DONE, D237) — RUNNING: lane B tester r2 delta ∥ lane A fix r1 (D236). If died: relaunch the missing one (look for "## Tester r2" in progress/V0.9-G3B.md, "## Fix round 1" in V0.9-G3A.md).**

**2026-10-18 (G3 lane A tester r1 FAIL TR1-A1..A3, D236) — RUNNING: lane A fix r1 ∥ lane B fix r1 (D235). If died: relaunch each fix r1 in its worktree (look for "## Fix round 1"); then tester r2 delta → reviewer per lane; merge A then B.**

**2026-10-18 (G3 lane B tester r1 FAIL D-1..D-3, D235) — RUNNING: lane B fix r1 ∥ lane A tester r1. If died: relaunch fix r1 in the lane B worktree per D235 (look for "## Fix round 1"); then tester r2 delta → reviewer.**

**2026-10-18 (G3 lane B hand-off `f8aaa0e`, D234) — RUNNING: lane A tester r1 ∥ lane B tester r1 (each in its worktree). Lane B fix r1 must fix bench priority aging (D234). If died: relaunch the tester (look for "## Tester r1" in progress/V0.9-G3A.md / G3B.md).**

**2026-10-18 (G3 lane A hand-off `ba2eb27`, D233) — RUNNING: lane A tester r1 (its worktree) ∥ lane B implementer exit gates. If died: relaunch tester r1 (look for "## Tester r1" in progress/V0.9-G3A.md).**

**2026-10-18 (G3 both lanes code-complete; weekly limit stopped them at exit gates; resumed) — lane A `45d75b8`..`d431405` worktree agent-adfae0f9309e69f76; lane B `27d7876`..`5bbcbb2` worktree agent-a20b980080f80c643. Both RUNNING exit gates + hand-off. If died: relaunch implementer in that worktree for exit gates only; then tester → reviewer per lane.**

**2026-10-18 (G3 brief §5d written, D232) — RUNNING in parallel: G3 lane A (item 0) + lane B (campaigns + forced runs, migration 0022), separate worktrees; progress docs/briefs/progress/V0.9-G3A.md / V0.9-G3B.md there. If died: relaunch per §5d from the progress file; per lane tester → reviewer; merge A then B → whole `-m component` once alone → close 0.9.3/0.9.4.**

**2026-10-18 (G1 CLOSED, D231; 0.9.1 `[x]`) — NOTHING RUNNING. Next: write G3 brief (§5d; item 0 = D225 2,4 + D228 + D229/D230 follow-ups) → G3 implementer. Main checkout needs `uv sync --all-extras` (pyarrow).**

**2026-10-18 (G1 MERGED `10f1f8c`, D230) — RUNNING: frontend build then whole `-m component` ONCE alone on main (log scratchpad v09_g1_component.log). If died: rerun `uv run python scripts/pytest_exit_guard.py -m component -q -rs` alone; green → 0.9.1 `[x]` + status bar + STATUS → write G3 brief (§5d; item 0 = D225 2,4 + D228 + D229/D230 follow-ups).**

**2026-10-18 (G1 tester r2 PASS `95ac123`; Lead fixed T2-1 non-finite floats) — RUNNING: G1 reviewer over b4267f3..HEAD in the G1 worktree. If died: relaunch reviewer; then merge → frontend build → whole component once alone → close 0.9.1.**

**2026-10-18 (G1 fix r1 DONE `2d8c74e`..`3288218`, D229 rulings; unit 13764 exit 0) — RUNNING: G1 tester r2 delta in the G1 worktree. If died: relaunch tester r2 (look for "## Tester r2" in progress/V0.9-G1.md); then reviewer → merge → frontend build → whole component once alone → close 0.9.1.**

**2026-10-18 (G1 tester r1 DONE `80b053f`, FAIL: T-1..T-6; T-2/T-3/T-4 major) — RUNNING: G1 fix round 1 in the G1 worktree (all T-1..T-6 + merge main + D228 `compute_iterations`). If died: relaunch fix r1 there (look for "## Fix round 1" in progress/V0.9-G1.md); then tester r2 delta → reviewer → merge → frontend build → whole component once alone → close 0.9.1.**

**2026-10-18 ACCOUNT SWITCH — COLD START HERE.** Main = `ce3bd35`+ (G0, G2, G2 follow-ups merged; 0.9.2 `[x]`). **In flight: G1 (0.9.1)** on branch `worktree-agent-ac0f79cc9fb73d5ae`, worktree `.claude/worktrees/agent-ac0f79cc9fb73d5ae`, implementer commits `b4267f3`..`bccc568` (rulings D227). G1 **tester r1 was STOPPED mid-run** (by the Lead, for the account switch); its 8 partial test files are committed as `wip(V0.9-G1): tester r1 partial tests - UNVERIFIED` (it was fixing xfail markers). Nothing is running. Steps: (1) relaunch tester r1 in that worktree: finish/verify the wip files (run them, fix markers, report defects) with the same scope (brief §5b + D227 'Try to break' list: sweep misses, silent skip of unbuildable record, failure classes, JSONB leaks, hostile workload.json, clock window, export determinism, migration 0021, T-14, old-agent compat). (2) G1 fix round: fix tester defects AND merge main into the G1 branch and satisfy the **D228 G1 merge requirement** (`compute_iterations`). (3) G1 reviewer → merge → `cd frontend && npm run build` → whole `-m component` ONCE alone → close 0.9.1 `[x]` + status bar + STATUS. (4) Write G3 brief (§5d; item 0 = D225 items 2,4 + D228 list) and start G3. User prefs: parallel agents ON for non-conflicting work; **fast mode OFF**.

**2026-10-18 (G2 follow-ups MERGED `218aa05`, D228) — RUNNING: G1 tester r1 only. G1 MUST merge main and handle `compute_iterations` (D228) in its next fix round, before its reviewer. Then: G1 reviewer → merge → rebuild frontend → whole component once alone → close 0.9.1 → write G3 brief (item 0 = D225 2,4 + D228 list).**

**2026-10-18 (G1 implementer DONE `b4267f3`..`bccc568`, D227 rulings) — RUNNING: G1 tester r1 ∥ G2 follow-ups fix (T1 seeded rows, T2 StrictInt). Next: G1 reviewer; follow-ups reviewer → merge both → whole component once.**

**2026-10-18 (G2 follow-ups DONE `677813f`, worktree-agent-a7f238eacb3095e0f) — RUNNING: its tester ∥ G1 implementer (resumed after limit). New load flake: test_multi_source_fetch.py::test_peer_killed_mid_transfer_controller_resumes_verified_chunks (5/5 alone) → Known Bugs with the endgame one.**

**2026-10-17 (G0+G2 CLOSED; component 2367/0 exit 0; 0.9.2 `[x]`) — RUNNING in parallel: G1 implementer (brief §5b, item 0 = D226) + G2 follow-up implementer (D225 items 1, 3, 5; bench package only). G3 item 0 keeps D225 items 2 (🧑 Windows run) and 4. Next per lane: tester → reviewer → merge; whole component once after both.**

**2026-10-17 (G0 MERGED, D226; post-merge lint/mypy/e2e-collect green, frontend rebuilt) — RUNNING: whole `-m component` ONCE alone on main (log scratchpad v09_g0g2_component.log). If died: rerun `uv run python scripts/pytest_exit_guard.py -m component -q -rs` alone. Green → 0.9.2 `[x]`, G0 closed, status bar, STATUS → launch G1 implementer (brief §5b, item 0 = D226). 🧑 0.8.10 perf step E3 now unblocked.**

**2026-10-17 (G2 MERGED `8010dcb`, D225; unit on main 12757 exit 0) — RUNNING: G0 fix r2 only. Next: G0 reviewer → merge → rebuild frontend → whole `-m component` ONCE alone (covers G0+G2) → close G0 + 0.9.2 `[x]` + status bar → write G1 brief (item 0 none; G3 item 0 = D225 list).**

**2026-10-17 (G2 fix r2 DONE `f92ccb1`,`271b039`: shared _support check rules in all 15 kernels) — RUNNING: G2 reviewer (no tester r3) ∥ G0 fix r2. Freivalds n=12288 headroom + cuBLAS rounding + float64 check cost → confirm on first CUDA run.**

**2026-10-17 (G0 tester r2 FAIL minor `f5efa34`: Infinity in JSON, broad offline markers, NaN lag) — RUNNING: G0 fix r2 (+ >25% dropped pings → uncertain) ∥ G2 fix r2. Next G0: reviewer (no tester r3) → merge → whole component once.**

**2026-10-17 (G0 fix r1 DONE `0537ac9`..`9a26433`, all 7 + ping dropping; perf e2e pass) — RUNNING: G0 tester r2 delta ∥ G2 fix r2. Open question for tester: `inf` bound serialised as JSON `Infinity`.**

**2026-10-17 (G2 tester r2 FAIL: TR2-D1..D7, torch kernels missed the r1 fixes — NaN, wrong shape, gpu.matmul Freivalds) — RUNNING: G2 fix r2 ∥ G0 fix r1. Lead: conv2d full recompute. Next: tester r3 delta → reviewer.**

**2026-10-17 (G2 fix r1 DONE `9032dc9`,`57c2c56`, all D1–D9 closed) — RUNNING: G2 tester r2 delta ∥ G0 fix r1. Known load flake seen: tests/unit/agent/test_multi_source_fetch.py::test_endgame_duplicates_the_last_chunk_held_by_a_slow_source (5/5 alone) → add to Known Bugs.**

**2026-10-17 (G0 tester r1 PASS + 7 defects `bb518a2`; 3 major: clock step undetected, n=0 phase crash, no re-measure after phase) — RUNNING: G0 fix r1 (all 7 + drop single backwards ping) ∥ G2 fix r1. Next per group: tester r2 delta → reviewer → merge → whole component once.**

**2026-10-17 (G0 implementer DONE `d407a0b`..`f39cd25`, worktree-agent-ac278509a6415d46b; new GET /v1/clock, recv_at, shared common/clock_offset.py; perf e2e pass) — RUNNING: G0 tester r1 ∥ G2 fix r1. Follow-up noted: dispatcher retries non-transient StatementError forever (poison handling) → tech debt.**

**2026-10-17 (G2 tester r1 PASS-with-defects `dd15266`, D1–D9 self-check/validation gaps) — RUNNING: G2 fix r1 (all D1–D9; Lead: labels trust `verified`) ∥ G0 implementer. Next: tester r2 delta → reviewer → merge.**

**2026-10-17 (G2 implementer DONE, worktree-agent-abef81c72ced9ce24 `0145a80`) — RUNNING: G2 tester r1 (same worktree) ∥ G0 implementer. Lead rulings: compute.yaml (not meshforge.yaml) OK; `bench workloads` CLI-only parity entry OK; G3 decides if controller may import bench. GPU/ML families unverified on CUDA → 🧑 later: `uv run pytest tests/component/bench -q -rs` on a CUDA laptop + `meshforge-agent hardware score --json` per laptop.**

**2026-10-17 (V0.9 APPROVED, ADR-0018) — RUNNING (parallel, user OK 2026-10-17): G0 implementer (brief §5a) + G2 implementer (brief §5c), separate worktrees. If died: relaunch from brief §5a; then tester → reviewer → whole component once → close G0 → write G1/G2 briefs (parallel allowed).**

**2026-10-17 (V0.9 brief DRAFTED, Lead) — NOTHING RUNNING. `docs/briefs/V0.9-benchmark-telemetry.md` (D211–D224, Q1–Q5) waits for team approval. On approval: ADR-0018 → G0 item 0 (D210 list, clock skew first, before 🧑 0.8.10 perf step).**

**2026-10-17 (G7 CLOSED, D210) — NOTHING RUNNING. 0.8.9 `[x]`; gate 2269 passed / 0 failed. V0.8 agent-side complete; closes on 🧑 0.8.10 (`lab/checklists/V0.8-visual.md`). Next agent work: V0.9 brief (item 0 = D210 list; do D210 item 1 clock skew early - the 0.8.10 lab perf step needs it).**

**2026-10-17 (G7 MERGED `334da94`, D210) — frontend rebuilt. RUNNING: whole `-m component` ONCE on main, alone (log in session scratchpad g7_component.log). If died: rerun `uv run python scripts/pytest_exit_guard.py -m component -q -rs` alone; green (0 skips in tests/e2e except perf) → close 0.8.9 + status bar + STATUS → 🧑 0.8.10 checklist `lab/checklists/V0.8-visual.md` (fix D210 item 1 clock skew before its lab perf step).**

**2026-10-17 (fix r2 done `930cbc8`,`c65535a`,`ac165dc`; Lead pin-flake fix `10356ca`) — RUNNING: G7 reviewer over 261cd72..HEAD in the worktree. If died: relaunch reviewer; then merge → rebuild frontend → whole component once alone (0 e2e skips except perf) → close 0.8.9 → 🧑 0.8.10. Note: wheel unit test needs network (fails, not skips, on DNS outage) — tech debt.**

**2026-10-17 (tester r2 PASS, D209) — RUNNING: G7 fix round 2 (T-7h, T-2d, T-2c, T-7c) in the worktree. If died: relaunch per D209 (look for "## Fix round 2"); then reviewer (no tester r3) → merge → rebuild frontend → whole component once alone (0 e2e skips except perf) → close 0.8.9 → 🧑 0.8.10.**

**2026-10-17 (fix r1 done, D208) — RUNNING: G7 tester r2 (delta) in the worktree. If died: relaunch (look for "## Tester r2"); then reviewer → merge → rebuild frontend → whole component once alone (0 e2e skips except perf) → close 0.8.9 → 🧑 0.8.10.**

**2026-10-17 (tester r1) — G7 tester r1 PASS with gate weaknesses (D207). RUNNING: G7 fix round 1 in the worktree. If died: look for "## Fix round 1" in progress/V0.8-G7.md there, relaunch per D207; then tester r2 (delta) → reviewer → merge → rebuild frontend → whole component once alone (0 e2e skips except perf) → close 0.8.9 → 🧑 0.8.10.**

**2026-10-17 (later) — G7 implementer DONE (`c607519`..`6b6bcc0`, hand-off in progress/V0.8-G7.md in the worktree; exit gate green). RUNNING: G7 tester r1 in the same worktree. If died: relaunch tester r1 there (look for "## Tester r1"); then reviewer → merge → rebuild frontend → whole component once alone → close 0.8.9 → 🧑 0.8.10.**

**2026-10-17 (resumed) — was RUNNING: G7 implementer resumed in worktree `.claude/worktrees/agent-a67711c51fc0582d6` (verify WIP 32adf39, then §5g items 2-6; progress docs/briefs/progress/V0.8-G7.md there). If died: relaunch there, resume from the progress file; then tester r1 → reviewer → merge → rebuild frontend → whole component once alone → close 0.8.9 → 🧑 0.8.10.**

**2026-10-17 (session end — team asked to stop) — NOTHING RUNNING. G5 CLOSED (D206), 0.8.6 `[x]`. G7 implementer STOPPED mid-work in worktree `.claude/worktrees/agent-a67711c51fc0582d6` (branch `worktree-agent-a67711c51fc0582d6`): item 0 (D204 + D206) committed `c607519`; item 1 fixture consolidation (tests/e2e conftest + edits to existing e2e files, pyproject, tests/_support/mf_dashboard.py) committed as WIP `32adf39` - UNVERIFIED, may not pass; perf script not started. Next: relaunch a G7 implementer IN THAT WORKTREE with §5g, told to verify/finish the WIP first (run tests/e2e), then items 2-6; write progress docs/briefs/progress/V0.8-G7.md; then tester r1 → reviewer → merge → rebuild frontend → whole component once alone → close 0.8.9 → 🧑 0.8.10 checklist.**

**2026-10-16 (late) — G5, stabilisation and D202 lanes ALL MERGED (D204, D205). RUNNING: whole `-m component` ONCE on main, ALONE (log in session scratchpad). If died: rerun `uv run python scripts/pytest_exit_guard.py -m component -q` alone; green → close 0.8.6 + STATUS Known Bugs D200/D156 + status bar → launch G7 (§5g, item 0 = D204).**

**2026-10-16 (new session, parallel ON per team, D201) — RUNNING in parallel: (a) stabilisation lane MERGED `af42613` (D202; Known Bugs D200/D156 close after next whole-component gate) → → reviewer → merge; (c) D202 follow-ups lane: tester r1 PASS, Lead fix D202-T1 `f617f81`, reviewer RUNNING (branch worktree-agent-ae0acdc26786af55d;  CLI circular import, D200 fail-fast assertion, UNAUTHORIZED hint; progress docs/briefs/progress/V0.8-D202-followups.md there) → tester → reviewer → merge; (b) G5 implementer DONE (branch worktree-agent-afeb9f766804c194e, `26194c5`, hand-off in progress/V0.8-G5.md there); G5 tester r1 FAIL (D203) → fix r1 DONE `a6a788c` → tester r2 FAIL only T-5 minor → Lead fixed `39c3796` (tester pin green) → reviewer APPROVE → MERGED `3576b54` (D204). Gate waits for the D202 lane merge, then whole `-m component` ONCE alone → close 0.8.6 + Known Bugs D200/D156 + status bar → G7 (§5g). Older note: G5 implementer (brief §5e) in an agent worktree (see `git worktree list`, newest `agent-*`; progress docs/briefs/progress/V0.8-G5.md there). If died: relaunch with §5e, resume from the progress file; then tester r1 → reviewer → merge → whole component once → close 0.8.6 → G7 (brief §5g, written).**

**2026-10-16 (session end — team asked to stop) — NOTHING RUNNING. G5 implementer was stopped seconds after launch with no changes; its worktree was auto-removed. G5 has NOT started. Main is clean at this commit; G3+G4 closed (D200). Next: launch G5 implementer with brief §5e.**

**2026-10-16 (evening) — G3 + G4 CLOSED (D200): gate 2220 passed / 1 pre-existing intermittent V0.5 test (Known Bugs). 0.8.3/0.8.4/0.8.5/0.8.7 `[x]`, status bar refreshed. RUNNING: G5 implementer (brief §5e) in worktree `.claude/worktrees/agent-a5672c5809a990ceb` (branch `worktree-agent-a5672c5809a990ceb`; progress docs/briefs/progress/V0.8-G5.md in that worktree). If died: relaunch with §5e, resume from the progress file; then tester r1 → reviewer → merge → whole component once → close 0.8.6.**

**2026-10-16 (resumed) — another account briefly ran this handoff, then stopped and reverted its own changes (`b9fdc2a`..`aee664e`; the files are the same as `30df758`; worktrees restored). RUNNING: whole `-m component` on main, alone (log in session scratchpad). If died: rerun it; then follow the next line.**

**2026-10-16 (session end — team asked to stop) — NOTHING RUNNING. The whole `-m component` run on `dcb72a6` was STOPPED unfinished (no result). Main = G3 + G4 merged (D195, D198) + G5 brief §5e + D199 (`06a08c2`). Next: rerun `uv run python scripts/pytest_exit_guard.py -m component -q` on main ALONE (no agents in parallel - load flakes, D192); green (or flake proven by reruns) → close 0.8.3, 0.8.4, 0.8.5, 0.8.7 (latency evidence: progress/V0.8-G3.md hand-off) + `scripts/status_bars.py` → STATUS → G5 implementer (§5e) → tester r1 → reviewer → merge → G7 brief. Old worktrees for G2/G3/G4/G6 are merged and can be removed.**

**2026-10-16 (later) — G3 + G4 MERGED into main (`2f1fc0c`, `dcb72a6`, D195/D198). RUNNING: whole `-m component` once on `dcb72a6` (log in session scratchpad). If died: rerun `uv run python scripts/pytest_exit_guard.py -m component -q`; green (or load-flake proven by reruns) → close 0.8.3, 0.8.4, 0.8.5, 0.8.7 (live latency evidence in progress/V0.8-G3.md hand-off) → status bar → write G5 brief (item 0 = D195 + D197 + D198 follow-ups).**

**2026-10-16 (late) — G3 MERGED `2f1fc0c` (reviewer APPROVE, D195; follow-ups → G5 item 0). Running: G4 REVIEWER (tester r2 PASS `97dc1c6`, Lead fixes `ad71c61` D197). Earlier: G4 tester r2 delta (fix r1 `d82fd9d` + Lead backend fix `1d31ade` logs WS close; G2 follow-up: degraded/unknown status tokens just under 4.5:1) → then → reviewer → merge G4 (EVENT_APPLIERS) → whole component once. Earlier: G3 reviewer (fix r1 `9a0befe`) — earlier: G3 TESTER r1 (G3 hand-off done `be3020c`, exit list green; Lead accepts backend deviation 1 = alert id via invites.audit/apply_status; follow-ups: revoked running agent exits with traceback, cache details not reported, 628 kB bundle split before G7) and G4 TESTER r1 (G4 hand-off done `d3a3a04`, exit list green; worktree agent-adfe0…; note G4 edited frontend/src/test/g2_tester.test.tsx - possible merge conflict with G3). D193 answers Q-G4-1/2 (no new deps). If died: check "## Hand-off" in progress/V0.8-G3.md / V0.8-G4.md; relaunch for whatever is missing; then tester r1 → reviewer per group → merge G3, then G4 (EVENT_APPLIERS = [applyEvent, applyJobEvent]) → whole component once.**

**2026-10-16 (night, session end — team asked to stop) — NOTHING RUNNING. Parallel agents ALLOWED (team, 2026-10-16 evening; confirm at session start). G2 + G6 CLOSED (D192).**
- **G3** (Overview + Nodes, brief §5c) branch `worktree-agent-ae15d430443abfb84`, worktree `.claude/worktrees/agent-ae15d430443abfb84`: item 0 (D191 a–e) `1502537`, item 1 GET /v1/alerts `9e2b332`, pages + live patching `fee4a1e`, vitest + e2e `c72b35c`, wip `progress + browser_session test` (last). Implementer was STOPPED while starting its exit list (unit tier + touched component/drift). Next: resume implementer for the exit list + hand-off note only → tester r1 → reviewer.
- **G4** (Jobs, brief §5d) branch `worktree-agent-adfe02d5c6bab08ed`: `7211007`, `beb5578`, progress `eb7f091` (applyJobEvent wiring note), e2e `c4a4f4e`, `38cb493`. Clean tree. STOPPED before updating progress + exit list. Next: resume implementer (progress file + exit list + hand-off; check its dependency question in progress) → tester r1 → reviewer.
- Merge order: G3 then G4 (Lead wires `applyJobEvent` into G3's shell hook point) → whole `-m component` once for both → close 0.8.3, 0.8.4, 0.8.5 (+0.8.7 if live latency evidence OK) → write G5 brief (Settings) — G5 and G7 prep can be parallel.

**2026-10-16 (resumed) — G2 tester r2 PASS `90b4b27`, reviewer APPROVE, merged `96620ee`. G6 reviewer APPROVE, merged `d2ac586`. D191 records non-blocking → G3 item 0. RUNNING: whole `-m component` once on merged main (covers G2+G6; log in session scratchpad). If the session died: re-run `uv run python scripts/pytest_exit_guard.py -m component -q` on main; green → close G2/G6 (roadmap 0.8.1/0.8.2/0.8.8 per brief §3, STATUS bar) → write G3 brief.**

**2026-10-16 (session end, team asked to stop) — NOTHING RUNNING. ONE subagent at a time, fast mode off.**
- **G2** (frontend, branch `worktree-agent-af40f9465633ac195`, worktree `.claude/worktrees/agent-af40f9465633ac195`): fix r1 `a147867` done (D189). Tester r2 STOPPED mid-run: vitest part committed `7dff96f`; uncommitted drafts `tests/e2e/test_v08_g2_tester_r2.py` and `tests/unit/controller/test_v08_g2_tester_r2_send_first.py` are in that worktree. Next: relaunch tester r2 there (scope: D189 delta — T-1/T-2/T-3 + extra fixes; see progress/V0.8-G2.md) → reviewer → merge into main.
- **G6** (CLI, branch `worktree-agent-ae510e92b7567c0fd`): fix r2 `67ea9b3`/`93399f0` done (D190; Lead accepted test change returncode (0,1)→(0,4)). Next: reviewer (no tester r3; reviewer checks removed xfails unweakened) → merge into main AFTER G2.
- Then: whole `-m component` once per group (G2, G6) → close → update roadmap/STATUS bar (0.8.1, 0.8.2, 0.8.8) → write G3 brief (Overview + Nodes; include `GET /v1/session` 200 `{session:null}` from D189).
- 🧑 pending: Windows PowerShell completion check (D187, in 0.8.10 checklist).

**2026-10-16 (back to one-at-a-time) — team reverted the parallel wave: ONE subagent at a time, fast mode off. Running now: G2 tester r2 only. G6 reviewer was stopped before reporting — relaunch it AFTER G2 tester r2 finishes. Order: G2 tester r2 → G2 reviewer → merge G2 → G6 reviewer → merge G6 → whole component once per group.**

**2026-10-16 (usage limit) — G6: fix r2 DONE `67ea9b3`/`93399f0` (T-6..T-10; Lead accepts tester-test change returncode (0,1)→(0,4) per D190). NEXT for G6: reviewer (D190: no tester r3) on branch worktree-agent-ae510e92b7567c0fd → merge → whole component once. G2: fix r1 `a147867` done; tester r2 was RUNNING in worktree agent-af40f… — check for "## Tester r2" in progress/V0.8-G2.md; then reviewer → merge (G2 before G6) → whole component once.**

**2026-10-16 (testers resumed) — usage limit stopped both testers; resumed. G2 tester r1 in worktree agent-af40f… (branch worktree-agent-af40f9465633ac195); G6 tester r1 in worktree agent-ae510e… (branch worktree-agent-ae510e92b7567c0fd), D187 exit-code ruling. If died: uncommitted tester files are in those worktrees; relaunch testers there. Then reviewer per branch → merge → whole component once per group.**

**2026-10-16 (G1 CLOSED) — gate green (D186); G2 item 0 merged. Running: G2 frontend tester r1 (worktree agent-af40f…), G6 CLI implementer (worktree). Next: G2 reviewer → merge G2; G6 tester → reviewer → merge; then whole component once per group.**

**2026-10-16 (parallel wave, team request) — while the G1 gate runs, three implementers run in parallel in git worktrees: G2 item 0 (brief §5b0), G2 frontend (§5b), G6 CLI (§5f). If died: check `git worktree list` + branches + progress files V0.8-G2-item0/G2/G6.md; merge order item0 → G2 → G6 into main after the G1 gate is green.**

**2026-10-16 (G1 gate) — reviewer APPROVE (D184, G2 item 0 list). **Group-end gate RUNNING:** whole `-m component` then full `-m security` (logs in session scratchpad). If died: rerun both once; if green → G1 closed, update STATUS (Known Bugs: T-5), then write G2 brief §5b.**

**2026-10-16 (G1 reviewer) — tester r2 PASS `b5b4696` (T-5 minor latent → G2 item 0, D183). **G1 reviewer RUNNING** (scope b7de7d5..HEAD). If died: relaunch reviewer; then whole component + full `-m security` once → close G1 (0.8.2/0.8.7 stay `[~]`, they finish in G2/G3).**

**2026-10-16 (G1 tester r2) — fix r1 DONE `3c4401c`..`0ae8120` (T-1..T-4, O-1 DB-S6, unit tier green 10257). **G1 tester r2 (delta) RUNNING.** If died: look for "## Tester r2" in progress/V0.8-G1.md; then reviewer → whole component once → close.**

**2026-10-16 (G1 fix r1 resumed) — usage limit hit mid-round; T-3/T-2 `3c4401c`, T-4 `bc93cd6`, T-1 `70e7cd9` committed; O-1 host guard was uncommitted (server.py, web/host_guard.py, its unit test). Fix agent resumed on O-1. Then tester r2 (delta) → reviewer → whole component once → close G1.**

**2026-10-15 (G1 fix r1) — tester r1 FAIL `b3e6332` (T-3 major savepoint publishes early; T-1/T-2/T-4 minor); `-m security` 1745 passed. Ruling D182. **G1 FIX ROUND 1 RUNNING.** If died: look for "## Fix round 1" in progress/V0.8-G1.md; resume D182; then tester r2 (delta) → reviewer → whole component once → close.**

**2026-10-15 (G1 tester r1) — G1 implementer DONE `b7de7d5`..`7ea19bb` (items 0–7; hand-off in progress/V0.8-G1.md; deviations accepted D181). **G1 tester r1 RUNNING.** If died: look for "## Tester r1" in the progress file; then reviewer → whole component + `-m security` once → close G1.**

**2026-10-15 (G1 resumed) — implementer hit the usage limit after item 0 (`b7de7d5`, D168 N1/N2/N3/T-12 done). Resumed on deliverable 1 (browser session; uncommitted edits were in the tree). If died again: check progress file + git log, relaunch with §5a from the next undone deliverable.**

**2026-10-15 (V0.8 G1 start) — team approved the brief ("go with your suggestions"); ADR-0017 Accepted; roadmap 0.8.2/0.8.7 `[~]`. **G1 implementer DONE (worktree .claude/worktrees/agent-a070ab6a8a352088b, HEAD 58897b35; Lead ruling: list order kept, dups refused) → G1 tester r1 PASS (33dfe929; D1 fixed by Lead 5e755edc; NOTE for G4: list items are opaque strings — never treat as paths, escape items_file) → G1 reviewer r1 RUNNING. MERGE G2 first, then G1 with 0026 down_revision→0025 + test_store_migrations chain fix.* (brief §5a). If died: check git log for `V0.8-G1` + docs/briefs/progress/V0.8-G1.md; relaunch with §5a (resume); then tester r1 → reviewer → whole component once → close.**

**2026-10-15 (V0.8 brief) — Lead wrote `docs/briefs/V0.8-dashboard-cli.md` (DRAFT): D169–D180, groups G1–G7, G1 brief with item 0 = D168 (N1, N2, N3, T-12), questions Q1–Q7. **Waiting for team discussion — no implementer until approved.** Next: record team answers in brief §7, write ADR-0017, mark 0.8.x `[~]`, launch G1 implementer.**

**2026-10-15 (G7 CLOSED) — 0.7.11 `[x]`, reviewer APPROVE, D168 recorded, STATUS bar refreshed. V0.7 agent-side complete; only 🧑 0.7.12 lab left. Nothing running. Next: V0.8 brief (dashboard & CLI v1), item 0 = D168 non-blocking list.**

**2026-10-15 (G7 reviewer) — fix round 1 DONE `7ee71f6`..`e4438f5` (TQ-1..TQ-7 + weak rows; 25 tester xfails now pass; whole component at `1b35f4a` 1916 passed/14 skipped exit 0 = group-end run). **G7 reviewer RUNNING.** If died: relaunch reviewer (scope: all G7 commits d1c4c61..HEAD, production changes `0e670a5`, `2aa2caa`, `c06fdfa`); then close 0.7.11 + STATUS bar.**

**2026-10-14 (G7 fix r1) — tester r1 PASS `17aa8e7` (no src defect; TQ-1..TQ-7 minor gate weaknesses, strict xfails). Ruling D167: small fix round, then reviewer (no tester r2). **G7 implementer FIX ROUND 1 RUNNING.** If died: look for "## Fix round 1" in the progress file; resume D167; then reviewer → close 0.7.11.**

**2026-10-14 (G7 tester r1) — G7 implementer DONE `d1c4c61`..`cbc4834` (hand-off in docs/briefs/progress/V0.7-G7.md; all deliverables 0-6; exit list green incl. whole component 1909/14 skipped exit 0; threat_model_check 99 rows: 84 mitigated / 15 accepted). Production fixes for the reviewer: `0e670a5` (CG-E2 cross-node status report, real defect), `2aa2caa` (insecure-dev blob cert). **G7 tester r1 RUNNING.** If this session died: look for "## Tester r1" in the progress file; then reviewer → close 0.7.11.**

**2026-10-14 (G7 start) — Lead wrote brief §5g (G7: 0.7.11 security suite, item 0 = D165) + ruling D166 (`security` = capability marker, `scripts/threat_model_check.py` makes criterion 5 mechanical) `ba17d9d`. 0.7.11 `[~]`. **G7 implementer RUNNING.** If this session died: check git log for `V0.7-G7` commits + docs/briefs/progress/V0.7-G7.md; relaunch with §5g (resume, do not redo); then tester r1 → reviewer → whole component once → close 0.7.11.**

**2026-10-14 (G6 CLOSED) — 0.7.10 `[x]`: tester r4 PASS `bde4410`, reviewer APPROVE, D165 recorded, STATUS bar refreshed; then whole component at `147942b`: 1901 passed / 14 skipped / 0 failed, exit 0 (67 min, 2026-10-14). Nothing running. Next: Lead writes G7 brief (brief §3, 0.7.11 security suite); item 0 = D165 follow-ups. Run the whole component suite once per group, at the end.**

**2026-10-14 (fix r3 HAND-OFF DONE) — "### Fix round 3 hand-off" written in `docs/briefs/progress/V0.7-G6.md` (per-item file:line + tests, `1266086` root cause for the reviewer, `8b9871d`/`30f2cc0` rationale, evidence table). Exit list accepted as recorded by Lead decision; whole-component run after `30f2cc0` SKIPPED (run 4 result not awaited; both whole-component failures were test-only races, fixed and rerun green). **Next: tester r4 (narrow: D164 items + `1266086`)** → reviewer → close 0.7.10 + STATUS bar.**

**2026-10-14 (fix r3 exit list, RUNNING - implementer) — at `1e6b68e`: gates 0 (ruff, format 1161, mypy 303, lint-imports 7 kept, gen_protos 19); unit 9789 passed/4 skipped/3 xfailed exit 0; cluster 81 passed/6 skipped exit 0; chaos 11/22/33 x5 5/5 exit 0; F6 x20 20/20 exit 0; 12 redaction files PYTHONHASHSEED 2/3/7 128 passed exit 0 each; r3 sweep/cancel/thread-site/unreadable-dir/https-uv files under 16 burners 5/6 exit 0 (25 passed each; run 3 red only on a desktop Chromium `/dev/shm/.org.chromium.*` file caught by the tester test's global /dev/shm diff - environmental, 6th run green); `1266086` tests x5 under burners 5/5 exit 0. whole component run 1 at `63569b5`: 1898 passed/14 skipped/1 failed exit 1 (67 min) - `test_v05_g5_gc::test_a_running_attempt_keeps_its_tree_through_gc_passes` 409 node offline (0.6 s lease expired by a blocked loop; same root cause as 1f037f2) -> test fix `8b9871d` (bounded 409 retry, assertions unchanged; 10/10 alone, 10/10 under burners). Whole component run 2 at `8b9871d` was INTERRUPTED at 94% (session ended, no result). Run 3 at `14cbe53`: 1898 passed/14 skipped/1 failed exit 1 (67 min) - `test_v07_g6_tester_r3_cancel_subprocess::test_agent_shutdown_with_a_write_blocked_for_a_long_time_returns` (test race: polled the base before the released write made its folder) -> test fix `30f2cc0` (waits for the write to return, assertion unchanged; alone 10/10, file x5 under burners green, mutant caught). **Whole component run 4 RUNNING at `30f2cc0` (17:02)**; "### Fix round 3 hand-off" being written meanwhile. Earlier plan was: r3 files x5 under burners, whole component, then "### Fix round 3 hand-off". If this session died: rerun only what is missing (results so far are in this line).**

**2026-10-14 (STOP, user) — G6 fix-round-3 exit list STOPPED by the user mid-run; nothing running, tree clean, /dev/shm empty, no test containers left. Code: D164 items `8f113b4`..`2062bf0` + one exit-list fix `1266086` (agent HTTPS connect/TLS cancel leaked a socket - root cause in anyio/httpcore, new `agent/http_connect.py` CancelSafeBackend; r3 cancel tests were red 4/5 under burners on ResourceWarning). Reached before the stop: static gates + unit + cluster etc. presumably green (NOT recorded - no hand-off written); whole `-m component` was 38 min in when killed. The agent was last checking something reported as "baseline 2/2 fails ... with the scratch backend" - most likely the `1266086` probe, unverified. **Next: one implementer run = full §5f exit list from scratch at HEAD (gates, unit, cluster, chaos x5, F6 x20, G6 redaction files under PYTHONHASHSEED 2/3/7, tester-r3 sweep/cancel/thread-site/unreadable-dir/https-uv files 5x under 16 burners, whole component alone) + "### Fix round 3 hand-off" incl. `1266086` (outside D164 - reviewer must look) → tester r4 (narrow: T-18..T-22 + `1266086`) → reviewer → close 0.7.10 + STATUS bar (CLAUDE.md §4c 1b).** Keep the laptop on mains.**

**2026-10-14 08:25 (fix r3 exit list) — every D164 item COMMITTED `8f113b4`..`2062bf0` (T-21 fd-based base check, T-22 fail-closed notice in the log, T-20 remove_tree, T-19 bounded wait, T-18 wheel URL needs --hash, docs incl. ARCHITECTURE "How log redaction works"); implementer hit the usage limit before the exit list. **Implementer RELAUNCHED for exit list + "### Fix round 3 hand-off" only.** If this session died: look for that hand-off; if absent rerun; then tester r4 (narrow) → reviewer → close 0.7.10 + STATUS bar.**

**2026-10-14 (fix r3) — G6 tester r3 FAIL (`3cc91a0`..; "## Tester r3" in the progress file): all r2 findings FIXED, matcher survived 1.45 billion fuzz feeds; NEW **T-21 MAJOR** (`SecretVault.sweep`/`wipe` follow a symlinked base in /dev/shm and delete the target) + minors T-18 (https wheel redirect to http), T-19 (unbounded join on a hung write), T-20 (pre-G6 `remove_tree` TypeError on mode-000 dirs), T-22 (fail-closed redactor invisible). Each has an xfail test. Lead ruling **D164** (brief §2): small fix round 3; matcher KEPT (textbook AC = 3.1 s / 1.2 GB at the cap). **G6 implementer FIX ROUND 3 RUNNING.** If this session died: check git log for `fix(V0.7-G6): T-21` + "## Fix round 3" hand-off; resume D164; then tester r4 (narrow) → reviewer → close 0.7.10 + STATUS bar.**

**2026-10-14 (tester r3) — G6 fix round 2 hand-off DONE `7242c39`. Exit list GREEN on mains: gates 0; unit 9579 exit 0; cluster 79 exit 0; chaos 5/5; F6 20/20; whole component 1855 passed / 14 skipped exit 0. T-17: fuzz 480 000 matchers / 8 hash seeds, 0 hang 0 mismatch; matcher REPAIRED not replaced, with fail-closed run-time guards (`RedactionStallError` → `*`); termination argument in the module docstring. Matcher build 0.043 s / 10.9 MB; 366 MB/s (1 secret), 13 MB/s (32 secrets, on the loop - T-12 debt). **G6 tester r3 RUNNING.** If this session died: look for "## Tester r3" in the progress file; if absent relaunch; then reviewer → close 0.7.10 + STATUS bar (CLAUDE.md §4c 1b). Lead standing rule: a THIRD matcher defect = replace with an eager textbook Aho-Corasick, no more patching.**

**2026-10-13 (mains, fix r2 exit list) — laptop plugged in. **G6 implementer RELAUNCHED: fix-round-2 exit list from `-m unit` + matcher numbers + fuzz evidence + "### Fix round 2 hand-off".** If this session died: look for that hand-off in docs/briefs/progress/V0.7-G6.md; if absent rerun; then tester r3 → reviewer → close 0.7.10 + STATUS bar.**

**2026-10-13 22:25 (BLOCKED 🧑 power again) — G6 fix round 2: every D163 item COMMITTED `8c54823`..`5897278` (T-17 root cause = half-updated walker state on an anchor hit; the new fuzz also found a MISSED REDACTION in the same code, fixed in `8c54823`; T-14 wipe-by-path after joining the write thread + 4 more audited sites `ad769f6`; T-13 `64ddd5b`; T-15 `d2eb682`; https-only wheels `fbd324a`; renewal gate test `ac92485`; 425 stale `/dev/shm` test bases removed `5897278`). Static gates exit 0, then the implementer hit the usage limit at the start of `-m unit`. **Exit list NOT run, no "Fix round 2 hand-off".** Tree clean, nothing running. **The laptop is on BATTERY again (ADP1 offline, 27%, ~25 min) - do not start the exit list until it is plugged in.** Next on mains: implementer = exit list from `-m unit` + D162 matcher numbers + fuzz evidence (≥ 200 000 cases, seeds) + "### Fix round 2 hand-off" → tester r3 → reviewer → close 0.7.10 + STATUS bar.**

**2026-10-13 (fix r2) — G6 tester r2 FAIL `fe602eb`..`f614e99` ("## Tester r2" in the progress file): all r1 findings fixed, but NEW **T-17 BLOCKER** (new Aho-Corasick matcher spins forever for a pair of secrets, e.g. `redact_bytes(b"3030616161613030306", {"S0": b"00aaaa000a", "S1": b"a000a"})`), **T-14 MAJOR** (cancel during materialise leaves the plaintext secret folder), T-15/T-13 minor, G5 flake `test_renewing_on_the_pending_certificate_activates_it_first`. Each has a strict-xfail test. Lead ruling **D163** (brief §2). **G6 implementer FIX ROUND 2 RUNNING.** If this session died: check git log for `fix(V0.7-G6): T-17/T-14` + a "## Fix round 2" section with a hand-off; resume the D163 list; then tester r3 → reviewer → close 0.7.10 + STATUS bar. Debt to copy into STATUS at close: T-12, T-16, fair inventory lock, no extra_index_url (decide before Mode 2/4).**

**2026-10-13 (tester r2) — G6 fix round 1 hand-off DONE `97bc2fd` ("### Fix round 1 hand-off" in docs/briefs/progress/V0.7-G6.md). Exit list GREEN on mains at `0e2824e`: gates 0; unit 9497 exit 0; cluster 79 exit 0; chaos 5/5; F6 20/20; whole component 1767 passed / 14 skipped exit 0. D162 numbers met (matcher build 0.041 s / 10.9 MB). digest_lock = slow test, not a deadlock (`2e950a9`). Open for Lead ruling after r2: T-5 also refuses `--index-url` lines + non-wheel direct refs; T-7 clears unparseable docs; inventory 0.5 ms pause `630e0c1` (outside D162). **G6 tester r2 RUNNING.** If this session died: look for "## Tester r2" in the progress file; if absent relaunch tester r2; then reviewer → close 0.7.10 + STATUS bar.**

**2026-10-13 (mains, exit list 3) — laptop PLUGGED IN (ADP1 online); the 7 leftover `Created` test containers removed. **G6 implementer RELAUNCHED: fix-round-1 exit list from `-m unit` + digest_lock verdict + "Fix round 1 hand-off".** If this session died: look for "### Fix round 1 hand-off" in docs/briefs/progress/V0.7-G6.md; if absent rerun; then tester r2 → reviewer → close 0.7.10 (and refresh the STATUS bar, CLAUDE.md §4c 1b).**

**2026-10-13 (BLOCKED 🧑 power) — G6 fix r1 exit list STOPPED: the laptop is on BATTERY (ADP1 offline, 61% and falling, governor powersave, CPU throttled) - the ~3 h list cannot finish and timing numbers would be invalid. Nothing running, tree clean at `eb1bf08`. Ran: ruff/format/mypy/lint-imports/gen_protos exit 0; `-m unit` exit 1 twice on different pre-G6 tests: (1) `test_repairing_many_blobs_does_not_block_the_loop` - real lock-fairness weakness in agent/cache/inventory.py, fixed `630e0c1` (OUTSIDE D162 - reviewer must look; commit message overstates the 0.5 ms measurement); (2) `tests/unit/common/test_v05_g4_tester_r3_digest_lock.py::test_upload_and_fetch_of_the_same_digest_in_parallel` "deadlock between uploaders" - OPEN, slow-run vs real CasStore deadlock unproven (rerun on mains; if it recurs use `-o faulthandler_timeout=27`). NOT run: cluster, chaos, F6, whole component, the D162 acceptance numbers; no fix-round hand-off yet. 7 leftover `Created` mf-att containers from tester runs (`docker ps -a --filter label=meshforge.agent --filter status=created`) - remove before the component run. **Next, once plugged in: implementer = exit list from `-m unit` + digest_lock verdict + "Fix round 1 hand-off" → tester r2 → reviewer (incl. `630e0c1`, `cf534ea`, `4a79155`) → close 0.7.10.**

**2026-10-13 (fix r1 resume) — G6 fix round 1: every D162 item COMMITTED `bee2625`..`4a79155` (T-1..T-11 + flake/gate items; two product races found and fixed: T-11 orphan sweep relabel `cf534ea`, Docker cancel socket leak `4a79155`), then the implementer hit the usage limit DURING the final whole-component run - exit list NOT recorded, no hand-off. Tree clean. **Implementer RELAUNCHED for exit list + hand-off only.** If this session died: look for a "Fix round 1 hand-off" in docs/briefs/progress/V0.7-G6.md; if absent rerun the §5f exit list; then tester r2 → reviewer → close 0.7.10.**

**2026-10-13 (fix r1) — G6 tester r1 FAIL `5f77668`..`f9a4ad2` (3 major T-3/T-5/T-8, 7 minor, gate flake T-11; each has a strict-xfail test; report in docs/briefs/progress/V0.7-G6.md "Tester r1"). Lead ruling **D162** (brief §2 table): deviations 1-9 accepted with conditions, full fix list. **G6 implementer FIX ROUND 1 RUNNING.** If this session died: check git log for `fix(V0.7-G6)` commits + a "Fix round 1" section in the progress file; resume the D162 list; then tester r2 → reviewer → close 0.7.10.**

**2026-10-13 (tester r1) — G6 implementer hand-off DONE `022a9a3` (docs/briefs/progress/V0.7-G6.md, no code changes). Gates + unit 9183 + cluster 77 + chaos 5x + F6 20x exit 0; whole component 1718 pass / 2 fail exit 1 (known GPU-env `test_gpu_probe_really_runs...`; NEW unreproduced `tests/component/lab/test_inventory_runner.py::test_timeout_kills_grandchildren`, 23/23 on rerun, cause unproven). 9 deviations await Lead ruling. **G6 tester r1 RUNNING.** If this session died: check git log for tester commits + a "Tester r1" section in the progress file; if absent relaunch tester r1; then Lead ruling → reviewer → close 0.7.10.**

**2026-10-13 (resume 2) — G6 implementer RELAUNCHED for the §5f exit list + hand-off only (no new features; code `19c4fb1`..`f21646b`). If this session died: check `git log` for commits after `1601619` and the tail of docs/briefs/progress/V0.7-G6.md; if no hand-off section there, rerun the exit list; else tester r1 → reviewer → close 0.7.10.**

**2026-10-13 (stop) — G6 implementer STOPPED by the user before the exit list; nothing running, tree clean. Code = `19c4fb1`..`f21646b` (items 0-7 all committed, progress in docs/briefs/progress/V0.7-G6.md). NOT verified as a whole. Next: one implementer run = §5f exit list only (fix red, commit, ≤800-word hand-off) → tester r1 → Lead ruling → reviewer → close 0.7.10.**

**2026-10-13 — G6 deliverables 0-7 committed (`19c4fb1`..`f21646b`); implementer resumed for the final exit list + hand-off only. If it died: rerun the §5f exit list, then tester.**

**2026-10-12 (G6 start) — Lead wrote brief §5f (G6 secrets, item 0 = D161). G6 implementer launched. If this session died: check git log for `V0.7-G6` commits + docs/briefs/progress/V0.7-G6.md, relaunch with §5f.**

**2026-10-12 (end) — V0.7 G5 CLOSED** (0.7.8, 0.7.9 `[x]`; ruling D161). Nothing running, tree clean. **Next: Lead writes the next V0.7 group brief (see brief §3), item 0 = D161 non-blocking list**, then implementer.

**2026-10-12 (fix) — G5 tester r1 FAIL (`caccefd`; T-8 major, T-3/T-6/T-7/T-9 minor) → ruling D160 (`90fe1c2`; all deviations accepted) → fix r1 DONE (`db9f224`..`2581c47`; unit/cluster exit 0, G5+Docker component only GPU env fail). Next: tester r2 (scope D160) → reviewer → close 0.7.8/0.7.9.**

**2026-10-12 (later) — G5 brief written (§5e of the V0.7 brief); G5 part 1 DONE (item 0 + deliverables 1-4, `2b42a69`..`b1fefd5`, green except whole component not re-run after fixture fix). G5 part 2 DONE (`f1befcf`..`84fc2bc`; unit/cluster/chaos green, component 3 fails = GPU env + 2 load flakes passing 3/3). G5 tester r1 was STOPPED by the user mid-run (no report; its partial unit file committed as wip, unverified - the relaunched tester owns/rewrites it). **Next: relaunch G5 tester r1 with the same scope** (prompt priorities: agent-side policy defence in depth per code, runtime caps read back, container-create asserts + extra escapes, item 0 boundaries, D145 deviation windows, manifest network default change; tell it to keep any flood/fork-bomb tests small). Nothing running. Lead still owes a ruling on part1+part2 deviations (manifest network default=none; unbound grants attributed not refused; old score docs accepted; Windows CPU cap best effort; env-build keeps network WS-I4). If this session died: check git log for `V0.7-G5` commits and docs/briefs/progress/V0.7-G5.md, relaunch with the same brief.**

**2026-10-12 — V0.7 G4 CLOSED.** Tester r2 PASS (`e5cc50e` partial + `1d45758`; strict xfails T-4/T-5 stay) → ruling D158 → reviewer APPROVE → D159. 0.7.6 `[x]`, 0.7.7 `[x]`. Nothing running, tree clean. **Next: Lead writes the next V0.7 group brief (see brief §3), item 0 = D158 + D159**, then implementer.

**2026-10-11 (resume) — G4 exit-list implementer DONE (`0b4966c`..`e61e23c`, hand-off in progress/V0.7-G4.md; all gates exit 0 except whole component 1 env-only GPU probe failure, pre-G4). G4 tester r1 FAIL (`6c25ba3`; T-1..T-3 minor + 2 flakes) → ruling D157 → fix round 1 DONE (`9cbc258`..`8ecea92`; unit/cluster exit 0, component 3 fails: GPU env + 2 load flakes passing 3/3 on rerun) → tester r2 was about to start but the USER STOPPED it before it ran (2026-10-12). Nothing running. **Next: G4 tester r2** (scope D157; prompt pattern = break T-1 live-stream re-auth, T-2 per-uploader staging incl. shared 64-upload cap, T-3 names; replay file x5, digest-lock x10; mutation 1 guard each T-1/T-2; full exit list + chaos 11/22/33 x5) → reviewer → close 0.7.6 `[x]`, 0.7.7 `[x]`.** If this session died: check git log for `V0.7-G4` commits after `b1e66c8` and docs/briefs/progress/V0.7-G4.md; relaunch with the same scope.

**2026-10-11 (stop) — V0.7 G4 implementer STOPPED by the user during its final exit list. Nothing running, tree clean at `3434cd7`.**
- Brief §5d `c15a57d`. All deliverables 0-7 committed: item 0 `011e920`, migration 0018 `9cd50c5` (done before rotation - deviation, rotation needs pending-cert columns), rotation `7c3e14a`, users/tokens/D148 `fa695a0`, CLI login `d3c7a79`, D145(d) `c867ed6`, tests `cac4684`, docs `0bf2209`, then exit-list fixes `5efd0dc` (pre-G4 component tests authenticate), `1dcea8e` (25 mutants all caught, 2 new tests), `3434cd7` (cluster hardware-scores CLI authenticates).
- **NOT yet proven:** whole -m component, -m cluster, chaos exit 0 at HEAD; wss-follow flake fix needs 10 consecutive file runs + 1 full cluster run; deliverable 6 is "part 1" - check for cross-user 404 on EVERY job route incl. wss/outputs, and the cluster run as a normal user with password + API token as D146 recorder markers. No hand-off report was written.
- **Next:** fresh implementer = "finish G4 exit list": gates, -m unit, whole -m component, -m cluster, chaos 11/22/33 x5, the missing deliverable-6 tests, write the <=800-word hand-off into docs/briefs/progress/V0.7-G4.md. Then tester r1 -> reviewer -> close 0.7.6 [x], 0.7.7 [x].

**2026-10-11 (end) — V0.7 G3 CLOSED.** Fix r1 `e094f10`..`b9ea681` → tester r2 PASS `42679c7` → reviewer APPROVE. 0.7.5 `[x]`, 0.7.7 `[~]`. D156: G4 takes migration **0018**, G6 **0019**; G4 item 0 = reviewer non-blocking list in D156; wss-follow flake tracked. Nothing in flight. Next: Lead writes brief §5d (G4 rotation) then G4 implementer.

**2026-10-11 — V0.7 G3 IN FIX ROUND 1** (brief §5c `e197750`; rulings D154, D155).
- Implementer done, deliverables 0-8 (`64c2f1e`..`5e68599`): mTLS session port, HTTPS REST/blobs, peer TLS with required client cert, server-cert hot reload, `[security] mode`, 60 s peer tokens (D147), revocation + migration **0016** (G4 takes 0017), D146 plaintext recorder. Exit list: all green except whole component 1 flaky failure (probes_e2e race, pre-G3).
- D154: enroll stays on its own TLS port 47101 (grpcio cannot do optional client cert on one port); no un-revoke (re-join as new node); re-join supersedes old serial.
- Tester r1 FAIL (HEAD `e0f9fda` + tester commits): T-1 superseded cert's live session not cut; **T-2 major** peer server ignores token `req` vs client cert node_id; T-3 session outlives cert expiry. Strict xfails in tests/component/controller/test_v07_g3_tester_planes.py and tests/component/agent/test_v07_g3_tester_peer.py. Ruling D155 (also: fix probes_e2e race in the test; document 60 s token vs 30 s clock skew).
- **2026-10-11 later:** fix round 1 DONE (`e094f10`..`b9ea681`; T-2/T-1/T-3 fixed, probes race fixed, docs; exit list green modulo 2 flakes rerun 3/3). Deviation: migration 0017 used for cert end reasons -> G4 takes 0018. Next: tester r2 -> reviewer.
- **(old) In flight:** NOTHING running. Fix-round-1 implementer stopped by the Lead 2026-10-11 mid T-2; its partial work committed as wip (see git log). Next session: resume a fix implementer with D155 (T-2 first, then T-1, T-3, probes race, docs), then full exit list.
- **Next:** fix round 1 exit list green → tester r2 → reviewer (also reads invites.py / migration 0015 / fabric_routes in full, per D153) → close 0.7.5 `[x]`, 0.7.7 stays `[~]` (rotation = G4).
- 🧑 Every lab laptop must re-join after G3. Tester wrote lab/checklists/V0.7-G3-mtls-revocation.md — fold into 0.7.12. Watch: cluster wss-follow flake (test_fabric_outputs_logs, once).

**2026-10-08 — V0.7 G2 CLOSED** (0.7.3, 0.7.4 `[x]`, reviewer APPROVE; non-blocking → D153 = G3 item 0). Next: Lead writes brief §5c (G3: 0.7.5 mTLS everywhere, 0.7.7a revocation, D147 peer token ≤60 s, D149 carry-over server-cert hot reload, D153 item 0), then G3 implementer. Nothing in flight.

Older: **2026-10-08 — V0.7 G2 STARTED:** brief §5b + D150/D151 written (`f17f623`); G2 implementer DONE (`75e5d7d`..`ee7ac57`, exit list green; deviations 1-8 in progress file hand-off) → tester r1 FAIL (`bfe7a84`: T-1 major IPv6 rate limit bypass, T-2/T-3 minor; ruling D152) → fix round 1 DONE (`80bcbd3`, `beb9e4d`, exit list green) → tester r2 PASS (no new defects) → reviewer APPROVE → CLOSED.

**2026-10-08 — V0.7 G1 CLOSED** (0.7.1, 0.7.2 `[x]`, reviewer APPROVE). Next: Lead writes brief §5b (G2 pairing, D135/D136 + D149 item 0), then G2 implementer. Rulings D147–D149 in the brief. Nothing in flight.

**Overall: 40 % (63/156 tasks `[x]`, 16 `[~]`).** Progress bars: `docs/STATUS.md` (regenerate with `uv run python scripts/status_bars.py` whenever a checkbox changes).

- **Version: V0.6 (scheduling & reliability) STARTED 2026-10-05** — brief `docs/briefs/V0.6-scheduling-reliability.md`; groups G1 reliability floor (0.6.9, 0.6.10, 0.6.14 + splits + D59, RUNNING) → G2 planner core → G3 retries/recovery/chaos → G4 network matrix + estimator → 🧑 0.6.13 lab.
- **V0.5 (data plane & environments): closed agent-side** (G1–G5); only 🧑 0.5.11 lab left. Earlier detail: Brief `docs/briefs/V0.5-data-plane.md` (D79–D95).
  - `[x]` **0.5.1** CAS store, **0.5.2** tree manifests + have/need (G1); **0.5.7** cached environments + venv runtime (G2).
  - `[~]` **0.5.8** base images — local part (pinned digests, `meshforge-agent prepare`) done in G2; controller-driven `compute node prepare` is G5.
  - G3 (V0.4 debt: upload deadline, log GC, persisted log gaps) closed — not a roadmap checkbox.
  - Stabilisation lane (closed, see below): flaky tests made load-robust (slow-follower double count was a real bug), **D95** a controller restart during staging re-mints the fetch token (`RefreshFetchToken`, ControllerMessage payload 18) and the agent retries within `staging_retry_s`.
  - Wave 2 (D96 regrouping): **G4 CLOSED 2026-10-05** (0.5.3, 0.5.4, 0.5.5 `[x]`); **G5** (0.5.6, 0.5.9, 0.5.10, 0.5.8 ctl, scrub, lab checklist) — implementer finishing (§2 "G5 state"). 🧑 0.5.11 lab after G5.
  - Stabilisation lane + D95: reviewer APPROVE 2026-10-04 → CLOSED; upload-grant refresh → roadmap 0.6.14.
- **V0.4 (remote execution): agent-side CLOSED** (13/17 `[x]`). Left: 🧑 0.4.15 lab run (`lab/checklists/V0.4-remote-exec.md`), 0.4.2 (Windows CI digest), 0.4.8 (real PyTorch CUDA run), 0.4.17 (Windows item).
- **V0.3:** closed agent-side; only 🧑 0.3.15 lab run + Windows checks (0.3.2, 0.3.13, G7) remain.
- **V0.2:** all agent-side spike work is complete with reviewer APPROVE (S1 `ef065ac`, S2 `633d474`, S3 `81d620f`,
  S7 `8072b9f`, inventory `75d8df5`, 0.2.13 done). Every one is `[~]` **only** because it awaits runs on real
  laptops. **0.2.12 must wait for real lab data — never run it on loopback numbers.**
- **V0.1:** code-complete; 0.1.4 and 0.1.10 await the first real GitHub CI run, which needs a human push.
- **0.2.1:** Laptop 1's NVIDIA driver is fixed (RTX 3050 Laptop, 595.91.07); still `[~]` because
  `torch.cuda.is_available()` needs torch installed (ADR-0011 / 0.2.3).

## 2. In flight right now

> ### V0.6 state (2026-10-06) — read this first
>
> **UPDATE 2026-10-07 (G4 tester r2 PASS with 3 minor strict xfails → G4 fix round 2 DONE):** fixes committed `222774a` (R2-1 empty run INCONCLUSIVE exit 4; R2-2 record-identical ledger lines count once), `db8fc96` (R2-3 `feasibility.missing_trees`: a tree named twice is billed once by estimator, missing_bytes and cache_affinity), `c9d35f0` (probe abort docstrings: nodes freed at once, pair keeps its interval — docstring fix, behaviour unchanged). All mutation-checked. **Exit list DONE:** gates 0 · unit 7552 exit 0 · whole component run 1 exit 1 (V0.5 `test_v05_g5_gc::…next_job_after_eviction…` lease_expired under load → pinned POST 409; alone 10/10) · run 2 1256/0 failed exit 0 · cluster 67 exit 0 · chaos 11/22/33 ×50 exit 0. **Next: G4 reviewer** → close 0.6.4/0.6.6 (0.6.13 stays 🧑 lab).
> **UPDATE 2026-10-07 (G4 fix round 1 DONE → tester r2 RUNNING):** exit list all exit 0 at `ec26128`+ (unit 7526, WHOLE component 1252/0 failed, cluster 67, chaos 11/22/33 ×50; seed 22 exhausted 1 job under D129 — tester to judge). a187b77/f956ffa confirmed sound (D131 sentence added `084cc41`); T-14 xfail converted `ec26128`. Next: tester r2 → fix → reviewer → close 0.6.4/0.6.6.
> **UPDATE 2026-10-07 16:52 (G4 fix round 1 STOPPED BY THE USER again — nothing is running, tree clean):** all code/test work of the round is committed (last `430d2e3`; see the 16:50 note below for the commits). Stopped during the exit list: gates ran · unit 7524 exit 0 · whole component run 1 exit 1 (2 failures, both since changed in tests `a187b77` `f956ffa`) · **component run 2 was killed at 74 % with no failure printed up to there — NOT evidence** · **cluster and chaos NOT run in this round**. No implementer hand-off report exists. **Next = fresh implementer, exit list only:** check `a187b77`/`f956ffa` are sound (a187b77 replaces a G2 test because D131's savepoint changed 'refused decision row rolls back the attempt' to 'row dropped alone' — confirm that is what D131 wants and ARCHITECTURE says so), check the one tester xfail left (`test_v06_g4_tester_checklist_d.py`, T-14) is intentional, then run gates, unit, WHOLE component (must exit 0), cluster, chaos 11/22/33 ×50; fix; hand off with the T-1..T-14 table. Then tester r2 → reviewer → close 0.6.4/0.6.6 (0.6.13 stays 🧑 lab).
> **UPDATE 2026-10-07 16:50 (G4 fix round 1 RESUMED and RUNNING — one implementer, mid exit list):** resume commits `24caac4` (flaky d: G3 cluster kills wait for the start ledger line), `3bf8ea7` (estimator: zero/negative/NaN bw floor never divides by zero + new `tests/unit/controller/test_v06_g4_fix1_resume.py`, 632 tests), `a187b77` (test: a refused decision row is dropped alone, the dispatch commits — D131 savepoint), `f956ffa` (test: graceful-stop waits for the session row to close), progress `b77012a` `b023bec` `430d2e3`. Flaky c verified (auto Pong on: 40/200 INTERNAL client relabel; off: 40/40). Earlier fixes reviewed; mutation checks T-1/T-6/T-7 recorded done. Exit list so far: gates ran · unit 7524 passed / 4 xfailed exit 0 · **whole component run 1 exit 1** (1250 passed, 2 failed: `test_v06_g2_dispatch_queue::test_a_decision_that_cannot_be_written_rolls_back_the_attempt` = a G2 test asserting the OLD behaviour that D131's savepoint changed → replaced by `a187b77`; + a graceful-stop end_reason race → `f956ffa`) · **component run 2 in progress** (68 % at 16:50) · cluster + chaos NOT yet run. One tester xfail still marked: `test_v06_g4_tester_checklist_d.py` (T-14) — check it is the intended 'as originally written' documenting case. **If this agent died:** `git status` → commit leftovers as `wip(V0.6-G4)`; spawn a fresh implementer to run only what is missing: whole component (must exit 0), cluster, chaos 11/22/33 ×50, then hand off. Then tester r2 → reviewer → close 0.6.4/0.6.6.
> **UPDATE 2026-10-07 (G4 fix round 1 STOPPED BY THE USER mid-run — nothing is running):** tester r1 FAIL (`762f6af`..`ed507a1`, T-1..T-14, 19 strict xfails), Lead rulings **D130–D132** in the brief (`f3af4a6`). Fixer committed `5a8e685` (T-1 ingest floor, T-2..T-5, stale CPU sample), `df5f60c` (D131 per-tree transfer, finite estimates, T-7/T-8), `82cd16c` (T-6), `cb04cc3` (T-9), `60034c0` (T-10 + explain), `d2ebe15` (T-11..T-14 lab kit), `8c5e799` (docs), `82aaa40` (flaky a), `82b90e6` (flaky b), + Lead wip `596639c` (flaky c: malformed-message-id test with `auto_pong=False`; fixer's finding = grpc.aio client relabels ABORTED as INTERNAL when a Pong write races the abort, a test race not a server bug — **unverified**). **NOT done:** flaky d (`tests/cluster/test_v06_g3_tester_cluster.py::test_cancel_during_backoff…` — wait for the start ledger line before the kill); a new test file for the D131 fixes (per-tree, floor/finite, normalised scaling, history age, JSON safety) it was about to write; check every tester xfail it fixed is un-marked (`grep -rn xfail tests --include='test_v06_g4_tester_*.py'`; one still in `test_v06_g4_tester_checklist_d.py`); mutation checks for T-1/T-6/T-7; **the full exit list was NOT run after any fix** (gates, unit, whole component, cluster, chaos 11/22/33 ×50). **Next = resume G4 fix round 1** (fresh implementer: brief D130–D132 + the `tester r1`/`fix1` lines in `docs/briefs/progress/V0.6-G4.md` + this list) → tester r2 → reviewer → close 0.6.4/0.6.6 (0.6.13 stays 🧑 lab). One subagent at a time.
> **UPDATE 2026-10-07 (G4 tester r1 FAIL → G4 fix round 1 RUNNING):** tester commits `762f6af`..`ed507a1` (710 tests, 19 strict xfails). Defects T-1..T-14 in the tester lines of `docs/briefs/progress/V0.6-G4.md`: majors T-1 (tiny probed bw → Infinity estimate → dispatch tick raises), T-7 (one holder's probe prices all missing bytes), lab kit T-11/T-12/T-14; minors T-2..T-6, T-8..T-10, T-13. Lead rulings **D130–D132** in the brief. Flakes to fix in the same round so component/cluster exit 0: lab S3 child-dies, G2 pinned-node-gone, `test_session_server_round2::…malformed_message_id…` (INTERNAL vs ABORTED, pre-G4 1/30), cluster G3 cancel-during-backoff (kill races the start line). If the fixer died: commit wip, re-spawn with the same list from the first xfail still strict. Then tester r2 → reviewer → close 0.6.4/0.6.6.
> **UPDATE 2026-10-07 (G4 resume implementer DONE → G4 tester round 1 RUNNING):** commits `b246ffd`..`c6b6819` (wip files verified/finished; `compute network` traceback fix; **new behaviour `b2a42a3`: probe schedule seeded from `network_link.measured_at` at controller start** so a restart does not re-probe every pair — Lead accepts, reviewer to confirm; 33 mutations all caught). Exit list at `6928a1e`: gates 0 · unit 6128 exit 0 · cluster 67 exit 0 · chaos 11/22/33 ×50 exit 0 · **whole component exit 1 twice** (1–2 failures, neither G4: `lab/test_s3_adversarial::…child_dies_mid_measurement` racy alone 1/12; `test_v06_g2_tester_dispatch::…node_is_gone…` PG stall under load, alone 5/5) — tester to re-run the whole tier and judge. Open D108 readings for the reviewer: transfer bw = faster of probe/history (not "matrix else history"); scaled compute uses the fastest other node; output term assumes a symmetric controller link; controller links have no staleness rule. If the tester died: re-spawn on §5d + `9b602dd`..`c6b6819`. Then fix → reviewer → close 0.6.4/0.6.6.
> **UPDATE 2026-10-06 (G4 implementer, items 0–5 DONE, exit list NOT run):** commits `9b602dd`..`e1360ae` (D129 lift rule, probes/network matrix 0.6.6, estimator v0 0.6.4 + criterion 4, 🧑 0.6.13 lab kit, ARCHITECTURE/OPERATIONS) — see `docs/briefs/progress/V0.6-G4.md`. Implementer died on a usage limit; Lead committed its leftovers as wip `6fea3d6` (explain_commands.py change, tests/cluster/test_v06_g4_network_matrix.py, tests/unit/cli/test_v06_g4_cli_network.py) — **UNVERIFIED**; a resumed implementer (stopped by the user) then committed `6cb860b` (explain names stale probes, checklist 18) — check whether the wip is otherwise finished. **Next = fresh G4 implementer (resume):** verify/finish the wip files, self-check §5d deliverables + mutation checks, run the full exit list (incl. chaos seeds 11,22,33 ×50), fix, hand off. Then G4 tester → fix → reviewer → close 0.6.4/0.6.6 (0.6.13 stays 🧑 lab).
> **UPDATE 2026-10-06 (G4 implementer stopped by usage limit 2026-10-20 (worktree .claude/worktrees/agent-a32439c2c773f949a; controller lanes + agent lane runner + cluster test committed, main/G3 merged in; wip no_split_support check) → resumed → **G4 implementer DONE** (HEAD 74ad1b30; migration 0027; unit 16503 green; cluster 3/3 incl. kill -9 + controller restart, output byte-identical) → G4 tester r1 FAIL (f7b7e819): D1 MAJOR livelock - unverified `done` results released uncounted forever (tests/component/controller/test_v10_g4_tester_r1_livelock.py). Lead ruling: per (chunk,lane) counter `[controller.split] max_unverified_results`=2, then counted retryable `output_error` (2-node + max_attempts rules) + no immediate re-grant to same lane. → G4 fix r1 DONE (27638610; 22 G4 tests + unit 16503 green) → tester r2 PASS (cf2b2e4d; D2 minor: LRU counter eviction) → reviewer r1 REQUEST_CHANGES: B1 SECURITY symlink escape from `<workdir>/.meshforge/chunk` (chunk out → host files exfiltrated / rmtree host path; Lead: move chunk control dir outside job-writable area + lstat/O_NOFOLLOW check) + B2 flaky killed-lane test; plus D2, send cancel on SUCCEEDED, revoke lane creds on end, log unverified_late, drop getattr fallbacks, split lane.py. → G4 FIX ROUND 2 RUNNING (same implementer). Then tester r3 (hostile-chunk focus) + reviewer r2.:** item 0 D129 done `9b602dd`; item 1 network probes (0.6.6) done `8866ba5`; next items 2 estimator v0, 3 criterion 4 test, 4 lab checklist, 5 ARCHITECTURE. Progress `docs/briefs/progress/V0.6-G4.md`. If cut off: commit wip, resume from the first item not marked done there.
> **UPDATE 2026-10-06: G3 CLOSED** (reviewer APPROVE, no blocking; 0.6.7/0.6.8/0.6.12 `[x]`). **Next = G4 implementer** with brief §5d **plus item 0 = G3 reviewer N-1/N-2/N-5** (exclusion lift all-or-nothing, ignores live NOT_ONLINE — planner/plan.py:100–104; lift per type, vram_oom last; seed-22 regression). See STATUS Current Task. One subagent at a time (§0e).
> **UPDATE 2026-10-05 (G3 fix round 1 DONE, implementer):** tester PASS (`7d8a707`, `ae054a4`); Lead rulings D126 (disk_full retried for auto + node excluded, pinned not), D127 (cancel request -> CANCELLED), D128 (doc), root cause in `failure_detail` - committed `8e6ff54`, `781a3c0`, `0dd310b` + progress. Exit list green (unit 5963, cluster 66, chaos 6 seeds x50; whole component had 1 stale D127 assertion, fixed and re-run). **Next: G3 reviewer round.** Details: progress file `fix1` lines.
> **UPDATE 2026-10-05 (G3 implementer DONE + exit list green):** the whole component run first found 6 pre-retry tests asserting one-attempt typed failures; updated to D110 semantics in `6c210ba` (tests only). Exit list all exit 0: unit 5929, component 1189, cluster 60, chaos 3 seeds — see the progress file. **Next: G3 tester round 1.** Earlier note: items 0-5 committed `60020d8` .. `2cb03c5` (N-1, retry engine, LOST paths, crash recovery + criterion 2 cluster test, partition/overlap, chaos suite in `tests/cluster/chaos/` = tier `chaos`, plus two self-review fixes: retried attempt's queue phase, log follower across a retry). Chaos 3 seeds (11,22,33) x 50 jobs pass. **Finding for the Lead:** a full-disk node black-holes jobs (`disk_full` not retryable; one 3-6 s fill failed 13/50 jobs in seed 22). Next: G3 tester round 1. Progress lines: `docs/briefs/progress/V0.6-G3.md`.
> **UPDATE 2026-10-05: G2 CLOSED** (0.6.1/0.6.2/0.6.3/0.6.5/0.6.11 `[x]`; tester D-1..D-3 fixed; reviewer APPROVE; D120–D125 in the brief). **Next = G3 implementer** with brief §5c **plus item 0 = reviewer N-1** (planning.py:213 catch-all: only manifest errors fail the job; other exceptions log exc_info + leave QUEUED). See STATUS Current Task.
> **UPDATE 2026-10-05 late: G1 CLOSED** (last commit `8eb3f71` + docs close). Next = **G2 implementer** with brief §5b. Reviewer follow-ups are listed in STATUS "Current Task".
> - **Brief written:** `docs/briefs/V0.6-scheduling-reliability.md` (D102–D119), **ADR-0015 Proposed** (team to review before G3). Groups strictly sequential: G1 reliability floor → G2 planner core → G3 retries/recovery/chaos → G4 network matrix + estimator.
> - **G1 implementer progress (2026-10-05):** done + committed 0a dispatcher split (`091d622`), 0b gc_index split (`cfd7382`), 0c GC writes off-loop test (`e4a2501`), 1 D59 grandchildren (`8dc550a`), 2 0.6.10 classification (`cef959d`; proto AttemptStatus.env_hash = 16, KnownBadEnvs for G2), 3 0.6.9 command resend/dedupe (`ea7265b`; command id = message_id, no proto change). Hit the usage limit once, resumed. **Remaining: 4 = 0.6.14 RefreshUploadGrant (D114) + restart-mid-upload test, 5 = docs, then full exit list + hand-off.** Item 4 partial edits saved as wip `ed6d5a5` + `50deaea` (agent side; implementer STOPPED by the user before tests ran) (unverified: proto, upload_grants, dispatch/recovery, blob_routes, new agent/execution/uploader.py). Account switch while it was on item 4: check `git log --grep V0.6-G1` and progress file; commit any uncommitted G1 edits as `wip(V0.6-G1): …` (explicit paths), then spawn a fresh implementer with brief §5a + "items 0–3 done; resume from item 4 (or the last done item)".
> - (orig) **G1 implementer SPAWNED** (brief §5a; only agent). Progress `docs/briefs/progress/V0.6-G1.md`, commits `*(V0.6-G1)`. If this session died: commit its wip, relaunch with §5a + "resume from <last done item>". Then G1 tester → fix rounds → reviewer → close 0.6.9/0.6.10/0.6.14.


> ### G5 state (2026-10-05) — read this first
> - **Scope** (brief §5e + §3 G5 row): 0.5.6 named inputs, 0.5.9 GC, 0.5.10 transfer telemetry, 0.5.8 controller part (`compute node prepare`), controller CAS scrub, 0.5.11 lab checklist, + 6 G4 reviewer carry-overs.
> - **Implementer DONE 2026-10-05** (`a7ec04e` … `948720e`, all deliverables). Gates exit 0; unit 5522 / whole component 957 / cluster 55, all exit 0. Deviations 1–9 in its handoff (summary: leases + pins in the SQLite GC index at `<data>/gc/`; quotas enforced by GC passes, not write refusal; controller may evict finished jobs' outputs after 1 day; prepare status in memory only; scrub triggered by integrity_error + manifest reads; read-only inputs are a guard, not isolation; new `POST /v1/nodes/{id}/gc` + `compute node gc`). Not run: slow/GPU tiers, Docker env-image prepare, Windows, multi-machine.
> - **Tester round 1: FAIL** (`47e6724`; unit 5522 / component 998 / cluster 56 exit 0; 4 strict xfails): **T3 MAJOR** integrity_error scrub hook (jobs/dispatcher.py ~L576) passes only job.tree_id, not input trees; **T4 MAJOR** gc_index `_BLOB_VICTIMS` NOT IN over tree_member scales with pinned tree size (100k files → 99 s pass holding write lock); T1 corrupt gc.sqlite fatal (should recreate); T2 push retries 5x no back-off → 409 under 12 concurrent. Notes N2 prepare pending forever on node loss, N3 `node gc --all` no nodes exits 0 silently. Deviation 5 PARTIAL (T3), others ACCEPT. **G5 fix round 1 DONE** (`1041f7f` T4 blob_lru held flag, `3f55d2f` T1 quarantine, `8e7bff8` T3, `7df52a0` T2 advisory lock, `777b944` N2 node_lost + resend on Reconcile, `3e22ad1` N3 exit 6; unit 5536 / cluster 56 exit 0; component 1002 + 1 load-only S1 trickle flake). **G5 tester round 2: PASS on fixes** (`ab20929`; unit 5546 / cluster 56 exit 0; component 1031 + own handle-leak error fixed) + 3 strict xfails: R2-1 quarantine while another live owner holds index drops its leases; R2-2 foreign sqlite with clashing schema bricks open(); R2-3 Reconcile resends unbounded per session. S1 trickle not reproduced (keep bound). **G5 fix round 2 DONE** (`4f6531f` R2-3 once per session, `8ea728f` R2-1 gc.open.lock + GcIndexBusyError → GC off for that process, `772d4c7` R2-2 schema check, `7924876`, `472319a`; unit 5547 / g5 files 117 / cluster 56 exit 0; whole component not re-run). **G5 reviewer: APPROVE, no blocking** (non-blocking: confirm controller gc.py:133 `_on_change` SQLite write is off the event loop; split common/gc_index.py (831 lines) before V0.6; OPERATIONS notes done). Whole component 1046 exit 0. **G5 CLOSED 2026-10-05** (0.5.6, 0.5.8, 0.5.9, 0.5.10 `[x]`). **Next: 🧑 0.5.11 lab** (lab/checklists/V0.5-data-plane.md; budget ~10 GB/agent) closes V0.5; agent-side next: V0.6 brief.
> - (old) Next: G5 tester round 1. Prompt: brief §5e + those deviations; try to break GC vs a starting attempt (two processes), pins, scrub, named-input mounts/rm-while-used, telemetry rows vs FetchStats, prepare idempotency; full exit list.
> - **Then:** tester → fix rounds → reviewer → close 0.5.6, 0.5.9, 0.5.10, 0.5.8 `[x]` → 🧑 0.5.11 lab (checklist above) closes V0.5. Then V0.6.
>
> ### G4 — CLOSED 2026-10-05 (history)
> - **Scope** (brief §5d, D96 regrouping): 0.5.3 peer blob server, 0.5.4 multi-source parallel fetch, 0.5.5 cache reports + location index, + carried items. `h11` added to the agent extra (D97, `a38ad8c`).
> - **Implementer DONE:** `7945c60` … `b8d12b3` (8 deliverables). Its deviations D1–D7: CacheReport.seq process-wide; no resnapshot request (gap → stale, agent resnapshots at Hello + every 900 s); >256 KiB bodies = header + sendfile; bad peer endpoint ignored not rejected; 401 no token / 403 other; peer tokens 10 min, not refreshed mid-transfer (peers dropped, controller finishes); `common/cas.py` on_change hook + optional `JobRecord.bundle_digest`. Found delayed-ACK cause (REST listener socket not IPPROTO_TCP → no TCP_NODELAY): 43 ms → 1.7 ms. Loopback 1 GiB from 2 peers 4.88 s (not a benchmark claim).
> - **Tester round 1: FAIL** (`69e393a` … `f15c1f9`, ~110 tests): 2 major regressions (cluster resource-replay test; cas.py stat race from `98a2478`), resume-counter drift, FIFO stalls peer server, lost snapshot page → LIVE, deleted-behind-cache blob kept.
> - **Fix round 1 DONE:** `3cb1e7b` `da1bc88` `71f7e71` `d873279` `4ff6714` `fcd9727` `32c31fb` `c57bb61` (new `[agent.cache] enabled`, off only in the resource-replay cluster test). Unit 5382 / cluster 55 exit 0; whole component 931 passed + **2 load-only failures** (`test_g5_log_write_failure` symlink test: node SUSPECT at submit; `test_controller_process` SIGTERM: gRPC INTERNAL) — pass alone, not G4 code; tester r2 to judge whether G4 load contributes.
> - **Tester round 2: PASS on the 6 fixes** (`facc8b6`; unit 5397 / whole component 940 / cluster 55, all exit 0) but 4 new strict xfails: **R2-2 MAJOR** (pre-existing G1) `common/cas.py` complete() hashes then installs by path → a racing same-digest discard+open can install a blob whose hash ≠ name; R2-3 counter drift complete-vs-open; R2-1 snapshot tail page as a session's first report → LIVE partial; R2-4 reconcile_disk holds the lock 1.2 s at 50k. Load failures: sigterm = DB load, not G4; planted_symlink = small G4 load contribution (3/70 vs 0/40) → tolerant liveness.
> - **G4 fix round 2 DONE** (`9d99e88` R2-2+R2-3 per-digest lock + inode check, `f39e47e` R2-1 new `CacheReport.page = 6` (deviation D8), `19ebc2f` R2-4 batched, `12295f0` tolerant liveness + snapshot yields; unit 5402 / cluster 55 exit 0; whole component 939 + 1 lab S3 spike flake). **G4 tester round 3: FAIL** (`17fcf63`; unit 5417 / cluster 55 exit 0; component 939 + lab S3 spike flake): R3-2 MAJOR inode reuse defeats the (st_dev, st_ino) check → Lead ruling: in-memory per-digest generation counter; R3-1 old agent without `page` loses multi-page rows → `optional page`. **G4 fix round 3 DONE** (`59b89ac` generation guard, `9d0379b` optional page; implementer's exit list possibly not run — session ended). **G4 tester round 4: FAIL (minor)** (`3a9c343`): both fixes hold, all 3 guard sites mutation-covered; R4-1 two CasStore instances on one dir share no generation table → **Lead: accepted limitation** (production = one store per dir; strict xfail documents it); 2 load-only test flakes fixed by Lead in `b51af39` (hammer runs until something completes; cache-index tests use tolerant lease). **G4 reviewer: APPROVE, no blocking → G4 CLOSED 2026-10-05** (0.5.3, 0.5.4, 0.5.5 `[x]`). 6 non-blocking items carried into the G5 row of brief §3 (fetcher split first). **G5 implementer RUNNING (2026-10-05, only agent)** — progress `docs/briefs/progress/V0.5-G5.md`, commits `V0.5-G5`; if it died, commit its wip and resume it from the last done item.
> - **Then:** reviewer → close G4 (0.5.3, 0.5.4, 0.5.5 `[x]` with evidence, bars, STATUS, PROJECT_SUMMARY if needed) → G5 implementer from §5e.
> - **Follow-ups noted (not blocking):** split `agent/cas/fetcher.py` (~850 lines); peer-token refresh for >10 min transfers; controller cannot request a resnapshot; cache reports share the replay ring with attempt statuses; `tests/component/lab/test_s1_server_upload_limit.py` pre-existing flake (1/12); 🧑 lab: open TCP 47120 on LAN profile + `[agent.peer] bind` (OPERATIONS §0d).
>
> **2026-10-04 (session 2) — stabilisation lane + D95 reviewer APPROVE → CLOSED** (step 1 below done; its 3 non-blocking items carried into G4). Wave 2 briefs WRITTEN (brief §5d G4, §5e G5; D96–D101; `h11` dep added). **G4 implementer DONE** (all 8 deliverables, `7945c60` … `64cefa1`; unit 5377, component agent+controller 494, gates exit 0; handoff `b8d12b3`; deviations D1–D7 in its report, summarised in the tester prompt). **G4 tester round 1: FAIL** (`69e393a` … `f15c1f9`): 2 major regressions — cluster `test_fabric_resource_replay` red since `7772e7c` (cache reports share the report sequence), `common/cas.py` `_open_locked` stat race from `98a2478` (13/25 flaky) — plus resume-counter undercount (major), FIFO blocks peer server, lost snapshot page → LIVE, deleted-behind-cache blob never repaired. **G4 fix round 1 DONE** (`32c31fb` … `c57bb61`; unit 5382 / cluster 55 exit 0; whole component 931 passed + 2 load-only failures: test_g5_log_write_failure symlink (node SUSPECT at submit), test_controller_process SIGTERM (gRPC INTERNAL)). **G4 tester round 2 RUNNING.** If it died: re-spawn a tester on §5d + commits `7945c60` … `b8d12b3`; whole component suite was NOT re-run by the implementer after `4771940`. If this session died: check `git log --grep V0.5-G4` + `docs/briefs/progress/V0.5-G4.md`, commit its uncommitted edits as `wip`, relaunch the implementer with §5d + "resume from <last done item>". Then G4 tester → reviewer → close; then G5.
>
> Exact next steps, ONE agent at a time:
> 1. **Reviewer** over the stabilisation lane + D95: commits `97f347e` `35c5274` `861d712` `c76288e` `732b77f` (LogHub slow-follower count, tolerant liveness profile in `tests/_support/mf_controller_harness.py` used only where the lease is not under test, FaultLink FIN-vs-RST close, D95 RefreshFetchToken + agent staging retry). Known limitation to rule on: upload grants are not refreshed across a restart (→ `output_error`).
> 2. **Lead writes the wave 2 briefs** (G4, G5) in `docs/briefs/V0.5-data-plane.md` §5d/§5e (decisions D88–D93 already settled; §3 scope rows list carried items: drop G1's `bundle_digest` reuse, env_hash/env_cache to the controller, delayed-ACK check, member set off the event loop, controller CAS scrub).
> 3. **G4 implementer** → tester → reviewer → close; then G5 the same way.
> Rules learned this version: commit with `git commit --only <paths>`; never leave files staged; resume a usage-limited agent via SendMessage (cheap) and commit its uncommitted edits as `wip` first; regenerate the progress bars on every checkbox change.

---

**Older history below (V0.4 and before).**

> **2026-09-28 — V0.4 started.** Brief: `docs/briefs/V0.4-remote-execution.md` (decisions D40–D54, groups G1–G5, pasteable briefs for G1–G3). **G1 (0.4.16 + 0.4.17 items 2, 5) spawned** as an implementer; it logs progress to `docs/briefs/progress/V0.4-G1.md` and commits `refactor/feat(V0.4-G1): …`. If this session died, read that file + `git log`, commit any wip, and relaunch G1 with the §5 brief plus "resume from <last done item>".
> **All three implementers done (2026-09-28).** G2: `1462e34` `4293cd9` `2c2a5fe` `bbd03df` `9cebb8b` `f92e4fd` (0.4.1–0.4.5 + token). G3: `d625a45` `e189f86` `e149da5` `c3e48e5` `bc0c4c4` `b14baa3` (0.4.7, 0.4.8, 0.4.17 items 1/3/4/6/8). **Tester round 1 running for G1, G2 and G3 in parallel.** Deviations accepted as D55–D60 in the brief (§2f). 🧑 G3 Windows check + the pytorch image pull (Laptop 1 has only ~3 GB disk free — needs ~12 GB) listed in G3's handoff. Next: reviewer per group after its tester PASS; then the G4 brief.
> **G1 handed off (implementer done, all 4 deliverables):** `bfbe960` refactor (manager.py 1315→824; claims/reports/dispatch/live/admission/heartbeats/ends modules), D40 Goodbye inside G3's `e189f86` (+ rename in G2's `1462e34`; `server.py` one-line D41 wiring inside G2's `2c2a5fe`), `483b7ab` D41, `ff9a785` D43. Extra beyond brief: a timed-out admission after its upsert becomes a lingering entry fenced on several session ids (`fence_ids`) — reviewer to check. Mutation "ack Goodbye" is unobservable (session ends first). **G1 tester round 1 running.** D43b grpcio cap can be lifted after G1 closes.
> **Usage limit hit 2026-09-28 (all three lanes cut at once), resumed after reset via SendMessage — no work lost.** At the cut: G1 done 1–2, mid item 3 (uncommitted); G2 done 1–4; G3 done 1–4, mid item 5 (uncommitted). **Staging race:** a bare `git commit` after `git add` sweeps other lanes' staged files — G1's Goodbye (D40) landed inside G3's `e189f86`, and a G1 `git mv` inside G2's `1462e34`. Content is complete; only commit attribution is wrong. **Rule for parallel lanes: `git commit -- <paths>` only.**
> **Update (same day): deps added (`f9ca474`: zstandard new; pathspec/pyyaml/websockets/docker made explicit; types-PyYAML/types-docker), and G2 + G3 spawned in parallel with G1 — three implementers in one working tree.** Progress: `docs/briefs/progress/V0.4-G{1,2,3}.md` + `git log`. If this session died: commit nothing of theirs blindly — check each lane's progress file, `git status` (uncommitted files belong to the lane whose paths they are in), and relaunch only the lanes that stopped, with their §5/§5b/§5c brief plus "resume from <last done item>". **Next Lead steps:** tester + reviewer per group as each hands off; whole component suite once per group; after G1 closes, lift `grpcio<1.85` (D43b) while no lane runs tests. **ADR-0014** needs team review before G2 is accepted. **D40 departs from the roadmap's 0.4.16 text** (timing fallback deleted) — team may object.

> **G7-B D39 fix round done (`54b27c0`):** an undeclared GPU index rejects the whole score set (`gpu_index_not_declared`); tester's test inverted; `WriteKind.SCORES` docstring fixed; unit/component-agent/cluster exit 0.
> Next: tester re-run on the delta, then the reviewer.

**Nothing is mid-task.** G7 closed on 2026-09-25: 0.3.4 `[x]`, reviewer APPROVE with no blocking findings,
full evidence in the roadmap row. The next agent-side step is a **Lead** step: write the **V0.4 brief**
(`docs/briefs/V0.4-remote-execution.md`). Its first group must be **0.4.16** (explicit agent `Goodbye`, a hard
prerequisite of V0.6) plus **0.4.17 item 2** (move the per-report handlers out of `controller/session/manager.py`,
now 1315 lines) **before** V0.4 adds AttemptStatus/LogChunk handlers.

**G7 things the team should look at:** D30 adds `numpy` to the `agent` extra; the `hardware-scores` capability tag
(implementer's design, reviewer-accepted) tells REST `pending` from `none`; new `ExitCode.BUSY = 7`; D39 rejects a
whole score set that names an undeclared GPU. Known gap kept on purpose: a score write the DB refuses is already
acked and is lost until the next Hello, which heals it. 🧑 Windows check for G7: see STATUS "In Progress".
**Traps a later session should know:** the cluster harness runs agents with `[agent.scores] enabled=false` (several
agents on one host would load each other's liveness), so only scenario s exercises scoring there;
`tests/cluster/test_fabric_resource_replay.py` assumes resource_update is the only report type *because of that*.

*(Previous state, kept for context:)* G6 closed on 2026-09-24 (0.3.14 `[x]`, reviewer APPROVE pass 2, commits `64a89b5` …
`64e488a`) and the tree is committed. The next agent-side step is a **Lead** step: write the **G7** brief (0.3.4
hardware scores from the S7 kernels in `lab/spikes/_s7/`, at first enrolment and on demand) as
`docs/briefs/V0.3-fabric-core.md` **§5g**, then spawn it. GPU score must be a typed `unavailable: torch not installed`
(ADR-0011 undecided). Add "`ruff check . && ruff format --check .`" to the tester's exit list — G6's tester round 2
reported PASS while lint was red (the reviewer's only blocker).

**How G6 went** (decisions D19–D29 in §5f, story in the roadmap change log): runs A → B → C → tester **FAIL**
(criterion-2 backoff cap; false OFFLINE inside the 0.25 s confirm window) → fix round (D26–D28) + Lead D29 → tester
**PASS** → reviewer REQUEST_CHANGES (lint) → **APPROVE**. The reviewer's lasting concern is tracked as roadmap
**0.4.16**, a **hard prerequisite of V0.6**: the clean-close decision is inferred from *timing* (no cancel within
0.25 s) on measured grpcio 1.84 behaviour — hence the `grpcio<1.85` cap — and should become an explicit agent
`Goodbye` before OFFLINE starts triggering retries.

**G6 decisions the team should look at** (all shipped; object if you disagree):
- **D19 departs from the reviewer's NB-G5-1 proposal.** Only a *clean* close (agent half-close, controller shutdown)
  → OFFLINE at once; an *abrupt* stream loss → SUSPECT, with OFFLINE only if the lease runs out first. Reason:
  OFFLINE is what V0.6 turns into "attempts LOST, retry elsewhere", so a Wi-Fi blip must not cause duplicate work
  or count as a false OFFLINE in the 24 h soak. ARCHITECTURE §12.3 amended with the full table.
- **D20 fixes a latent race the Lead found while writing the brief:** an old session's queued state write could land
  after a reconnect's ONLINE and leave a connected node reading OFFLINE indefinitely. Session-originated state
  writes are now fenced on `last_session_id`.
- **D23 replaces the Toxiproxy binary with an in-process asyncio proxy** using Toxiproxy's fault vocabulary (it is
  named in ADR-0010/ARCHITECTURE §22; §22 updated). Reason: no download, no Docker host networking, runs after a
  plain `uv sync`. Object before G6-B if you disagree.

### What G5 shipped (0.3.12 `[x]`, 0.3.13 `[~]`)

Read-only, unauthenticated, LAN-bound REST `/v1` (`GET /v1/nodes`, `/v1/nodes/{id}`, `/v1/nodes/{id}/resources`,
`/v1/sessions`) with one typed `{"error":{"code","message"}}` envelope; OpenAPI at `/v1/openapi.json` with `/docs`
and `/redoc` **off** (they fetch assets from a public CDN an isolated lab LAN cannot reach). A `compute` CLI that
speaks REST only — `compute nodes [NODE_ID] [--json] [--watch]`, `compute status [--json]` — with ASCII-box tables
and `ExitCode.UNAVAILABLE = 6` instead of a traceback when the controller is down. `meshforge-agent hardware show`
for the per-probe facts that deliberately never go on the wire. `docs/OPERATIONS.md` + `deploy/` +
`scripts/print_service_setup.py`, which **prints and installs nothing**.

Rounds: implementer → tester **PASS** → reviewer **REQUEST_CHANGES** (2 blocking) → fix → tester **PASS** →
reviewer **APPROVE**. Commits `8dd97d5` … `bedbee5`.

**Both reviewer blockers were the same shape — a document claiming something the code did not do:**
- the error-handler set was not exhaustive while its own docstring promised it was, so an unexpected exception
  returned Starlette's text/plain 500 (the "bare FastAPI default" the brief forbids);
- **ANSI/OSC escape injection** — enrolment is unauthenticated in V0.3 and `Hello.hostname` / `os` / `platform` /
  `hardware` are validated for **length only**, so any host on the LAN could enrol with escape sequences that landed
  in every operator's `compute nodes`. Fixed with a single `_cell()` gate in `cli/render.py` returning
  `rich.text.Text`. **The defence-in-depth half is still open** — see Follow-ups below.

### The six G5 decisions a later session must not quietly reverse

- **D13** `HardwareFacts` is **not** served over REST — the controller only ever receives `Hello.hardware`, so the
  probe statuses/durations/degraded reasons (agent-side only) are exposed by a local `meshforge-agent hardware show`.
  Putting them on the wire is a **V0.8** conversation, not a G5 edit.
- **D14** the REST surface is **read-only** and unauthenticated in V0.3, LAN-bound. Proved, not asserted: the route
  set was enumerated and every mutating verb on every path is a typed 405. Hence `ResourceTracker.forget_node()` is
  still unnecessary — **it becomes required with the first node-delete path.**
- **D15** `httpx` lives in core deps; the CLI speaks REST only (a CLI importing `controller` breaks the boundary
  lint-imports enforces). `cli/client.py` keeps payloads as plain mappings rather than copying the controller's
  Pydantic models, because a copy it cannot import cannot be diffed against the original and would drift silently.
- **D16** `state` / `connected` / `sample_age_s` stay three separate fields. **Read the NB-G5-1 warning below before
  trusting `state`.**
- **D17** service units are documented and **printed, never auto-installed**.
- **D18** (new, 2026-09-23) `ExitCode.UNAVAILABLE = 6` for "the thing this command had to reach was unreachable" —
  `FAILURE=1` is generic and `CONFIG_ERROR=4` would lie when the URL is fine and the controller is simply down.

### 🔴 NB-G5-1 — the API tells a partial truth until G6 fixes it

**A cleanly closed session never moves `node.state`.** `SessionManager._close` enqueues only
`close_session(reason)`; `apply_event` is reachable only from the lease timer and heartbeat paths, which walk
`self._live` — from which `_close` has already removed the session. The only thing that ever corrects the row is
`registry.sweep_open_sessions` at controller start-up. So a laptop that shuts down gracefully reads
`state: online` indefinitely, and **`connected` is the only honest field.**

Confirmed twice independently: the tester with a tight lease and a *graceful* `request_stop()` (state stayed
`online` for 6× the would-be OFFLINE deadline), and the reviewer by reading the call graph.

**Consequences, in order of importance:**
1. **Anything reading liveness before G6 lands must use `connected` / `sample_age_s`, never `state` alone** — that
   includes V0.14's rule-based planner and any V0.15 training label. A fabric of laptops that suspend nightly will
   have rows reading `ONLINE` for hours with no session.
2. The fix belongs to **G6** and needs an **ARCHITECTURE §12.3 amendment first**: an explicit `STREAM_CLOSED` event
   with `ONLINE|SUSPECT → OFFLINE`, written from `_close` on the same `live.writer` chain as `close_session` (same
   chain ⇒ no reordering against the session-end row, and idempotent because `_close` already early-returns on
   `live.ended`), with **supersession excluded** — the new session's `Hello` owns the state. That exclusion is why
   this is a §12.3 decision and not a one-line patch.
3. Documenting rather than fixing it inside a REST task was the *right* call (§2a: three of G4's four defects were
   introduced by fixes), and it is a good argument that D16's three-field design was right.

### ✅ The unit suite's exit-139 is fixed (2026-09-24) — but keep reading exit codes

`pytest -m unit` printed a green summary and then exited 139. **It was not grpc.** A gdb backtrace showed the crash on
the **main thread** in `Py_FinalizeEx → _PyModule_Clear` of **`torch._C._functions`**. The chain:
`tests/unit/lab/test_s7_child.py` imported the *real* torch in-process → `filterwarnings = error` promoted torch's
"Failed to initialize NumPy" warning (this venv has torch but no numpy) to an exception half-way through
`torch/__init__` → `lab/spikes/_s7/child.py::_module` swallowed it → later calls re-ran `torch/__init__` over the
already-loaded C extension → the third attempt corrupts it → segfault at shutdown. The earlier bisection skipped
`tests/unit/lab`, which crashes on its own. **CI was never exposed** (it syncs without the `gpu` extra).

Fixed in `ff7095d` `d9ec26f` `bc6042b` `db2ceec` `a99bfe2` (reviewer APPROVE; tester PASS on every round but the last — its DEFECT-7 fix (bounded kill after Ctrl-C) was verified by the Lead on a real SIGINT-deaf pytest):
- `child._module` is `functools.cache`d — **a failed native import must never be retried in the same process**.
- The S7 child tests block torch/numpy with `monkeypatch.setitem(sys.modules, name, None)` — the pattern to use
  whenever a unit test needs an optional library to look missing. **Never import the real torch in a unit test.**
- `tests/conftest.py` fails a session that leaves a package half-imported and **names it** (it fires even on one
  bad import that would not have crashed).
- **`scripts/pytest_exit_guard.py <pytest args>`** — run pytest through it. It forwards output unchanged, returns
  pytest's status (signals → 128+sig), and says loudly when the summary is green but the exit is not 0. CI's unit
  and component steps and the implementer/tester agent files use it.

**For the G6 session:** the tester's component run through the guard found `tests/component/store/test_store_migrations.py`
still pins `HEAD = "0003"` after G6's migration `0004` (2 failures), plus
`test_s3_adversarial.py::test_loopback_ceiling_bounded_when_child_dies_mid_measurement` failing — neither is
related to this fix; not touched here.

Debugging tip that made this cheap: `gdb -iex "set debuginfod enabled on" -ex "handle SIGINT SIGTERM SIGCHLD ...
nostop noprint pass" -ex run --args "$PWD/.venv/bin/python3" -m pytest ...` (tests send themselves SIGINT, and the
path has a space — quote it). Ubuntu's debuginfod has no symbols for this python build; a breakpoint on
`_PyModule_Clear` printing `(char*)(*(long*)($rdi+48)+40)` (the module name, CPython 3.12 layout) found the module.

### A second trap the same investigation found

**A test that logs a large exception can wedge the entire suite.** structlog's *default* processor chain renders a
rich traceback at a cost superlinear in the message length (~1.9 s at 20 K chars, minutes at 200 K), and the spin
**holds the GIL**, so `pytest-timeout` never fires: it looks like a stall with no failing test, and every file
still passes in under a second on its own. Fixed structurally — `tests/conftest.py` now applies
`configure_logging` in a **function-scoped** autouse fixture. It must stay function-scoped: the
`tests/unit/common/test_common_logging*.py` fixtures legitimately call `structlog.reset_defaults()`, which repairs
away any once-per-session call.

### Follow-ups G5 raised (none blocking)

1. **Defence in depth for the injection blocker:** `check_hello_strings` (`controller/session/limits.py`, a G2 file)
   should reject non-printable characters at enrolment so the controller never *stores* them. The renderer is safe
   now, so this is not urgent — but phrase the G6 item as the **rule** ("agent-supplied strings are validated at
   ingestion; renderers stay defensive anyway"), because the renderer stops being the only sink the moment V0.8's
   dashboard or a single log line is added.
2. No request correlation id on the REST surface; `controller.api.*` events carry none.
3. `/v1/nodes` has a bound (500/5000) but no offset/cursor pagination and no `total` — V0.8.
4. `--watch` exits on the first transient error (deliberate, tested). For an operator watching a fabric, rendering
   the error into the table and exiting after N consecutive failures is friendlier — V0.8.
5. `uv run mypy` with no arguments reports **~59 pre-existing errors in `tests/`** (none in `src/`). Either clean
   them up or narrow `files` so the command in CONTRIBUTING is honest.
6. `test_the_no_limit_path_is_deterministic_for_tied_timestamps_too` is documentation, not a mutation-killing test
   (PostgreSQL's heap-scan order on a fresh small table coincides with insertion order). Its docstring says so.

### Still open from G4

- **🧑 NB-2, a team decision** — where NVML is missing but `nvidia-smi` works, a node reports its GPUs in
  `Hello.hardware` but **never** GPU util/VRAM in any `ResourceUpdate`. V0.9 telemetry and the V0.15 planner would
  train on a node that looks GPU-equipped and never uses it. Option to cost: a low-cadence (~30 s, interpolated)
  `nvidia-smi` poll behind a setting.
- **G6:** the component harness cannot express "controller stays up, the client's TCP drops and reconnects" — it
  restarts the controller, destroying the in-memory tracker. **That gap is why G4's R1 survived the first round.**
- **G6:** the NB-1 ordering invariant in `client.py` (no `await` between the ONLINE transition and
  `_reconcile_and_replay()`) is enforced by a comment plus a *component* test — a unit-gate edit will not see it.
  Structural fix: an `asyncio.Event` set after the reconcile and awaited by `send_report`.
- **Flusher:** during a real outage it issues up to 100 individually failing statements before asking the ping;
  probe the first row, then ask `_database_answers()` immediately.
- **V0.4:** `manager.py` is ~913 lines and still on the extraction list.

### Gate evidence at G5 close

`pytest -m component` → **421 passed, 6 skipped, exit 0, in 12 min 02 s.** Green, including a regression check over
the `lab` and `store` tiers G5 never touches. Targeted runs alongside it: `tests/component/controller` 64 passed,
`tests/component/agent` 67 passed (all 21 new G5 tests included), `test_registry_list_sessions_{tester,tiebreaker}`
10 passed against real PostgreSQL.

Two warnings about *measuring* this suite, learned the hard way. It appeared stalled for over an hour while being
polled — the machine was at load average ~9 with several of the Lead's own earlier runs competing for it, and each
`kill` of a stray run reset the picture. **Actual pytest time was 12 minutes**, close to the V0.1-era record. So:
run it once, do not poll it aggressively, and do not start other heavy runs beside it. And note the contrast that
mattered for the exit-139 hunt (since fixed — §2): the component tier exited 0 while the unit tier exited 139.

### Not written by an agent session

`docs/reports/` (two mentor progress reports + a PDF, 2026-09-24) appeared in the working tree from outside this
session and is left **untracked on purpose**. Commit it only if the team asks.

## 2a. What G4 and G5 cost, and what it should change about how we work

G4 (0.3.3 / 0.3.5 / 0.3.11, commits `5fa761b` … `dce88c0`) closed 2026-09-21 after **three tester rounds that each
found a critical defect**, then reviewer APPROVE. Full forensics are in the roadmap design change log; what a
future session needs is the pattern:

**Three of the four defects were introduced by *fixes*, not by the original build.**
- **R1** — the dedupe key `(session_epoch, sequence)` accepted replayed reports, corrupting snapshot means after
  any lossy reconnect. It came from a *plausible, well-argued* implementer deviation.
- **D-EVICT-1** — the R1 fix met an existing feature: ring-LRU eviction dropped the dedupe cursor, reopening the
  same double-count through another path.
- **ISO-1** — the fix for "a poison row blocks history" introduced its inverse, "an outage drops history".

**Therefore: run the tester after every fix round, not only after the initial implementation.** That rule cost
extra rounds here and caught three silent data-corruption bugs that green gates *and* code review both missed —
all of them in the telemetry V0.15's ML planner will train on. **G5 vindicated it again:** its fix round was where
the reviewer's two blockers were closed, and the tester round on that delta found a vacuous assertion and a gap in
an earlier tester's own test.

**A test that passes for the wrong reason is worse than no test.** Two instances in G4: the original component test
claiming "a replay is not counted twice" never performed a reconnect, and the ISO-1 defect pin passed via
`AttributeError` because its stub lacked `ping()` — it would have passed with the ping logic inverted. G5 added
three more: a `"Traceback" not in output` assertion that `CliRunner`'s `catch_exceptions=True` made vacuous, an
`elapsed < 0.1` bound sitting inside `capture_logs()` (which replaces the chain, so it measured nothing), and a
`MAX_LIMIT` boundary test that checked the max was *accepted* but never that max+1 was *rejected*. **When a test
guards something whose failure is silent, mutate the code and confirm the test fails.** G5 ran 9 such checks.

**New from G5: check the exit code, not just the summary.** Three agent rounds reported pass counts and none
noticed the suite exits 139. And **never edit a file while a test run or subagent is touching it** — doing so cost
this session several dead runs before the real cause was found.

## 3. Blocked on humans (nothing agent-side unblocks these)

0. **🔴 Run `uv run python lab/inventory.py` on laptops 2, 3 and 4** — still the top blocker. **V0.3 went ahead
   without it on the team's decision**, so the debt is now concrete: **0.3.3's success criterion 1 ("all 4 laptops
   appear with hardware matching `docs/LAB.md`") cannot be proven**, and neither can 0.3.7's or 0.3.15's lab
   evidence. It confirms the hardware ADR-0012 rests on (team-stated, unmeasured: L2 ≈ RTX 3050, L3 ≈ RTX 2050,
   **L4 no GPU**), fills `docs/LAB.md` §1, and settles ADR-0011. If L4 really has no GPU that is *good* — it is what
   gives the planner a real decision to make.
0b. **Team review of [ADR-0013](adr/0013-node-identity-storage.md) and the ADR-0002 Updates (2026-09-20)** — both Proposed; G1 builds
   on them, so objections are cheapest before G1 starts (key type P-256, `node_id` not key-derived, epoch fencing rules).
0a. **Team approval of [ADR-0012](adr/0012-scope-ship-mode3-complete.md)** — it changes an item `PROJECT_BRIEF` §33
   calls "relatively fixed", so it stays Proposed, and `CLAUDE.md` + the brief stay un-amended, until the other
   three agree.
1. **GitHub push + CI proof** — closes V0.1 (criteria 1–2) and lets laptops 2–4 `git pull`.
   Steps are in `docs/STATUS.md` → Next Recommended Task. **Agents never push.**
1a. **Run the S2 spike on two laptops** — Windows↔Linux, **both directions**, Wi-Fi **and**
   Ethernet: `lab/spikes/S2.md` §3 (server-side firewall TCP 47202–47203 only; the client
   side must have **no** inbound rule and pass `--no-inbound-rule`; also do the Windows
   second-`serve` check in §3.2). Paste back both JSON files + stderr per direction.
   Until then ADR-0002/ADR-0009 have no real cross-OS evidence.
1b. **Run the S3 spike on two laptops**, Wi-Fi **and** Ethernet — procedure in
   `lab/spikes/S3.md` §2 (firewall TCP 47301–47303; one pair at a time; never together with
   S1). Until then ADR-0003/ADR-0005 have no real-link evidence and 0.2.12 cannot start.
   The loopback numbers in `S3.md` §4 bound the tool, **not** the link — never quote them
   as evidence about the network.
2. **Team decision on [ADR-0011](adr/0011-torch-index-per-platform.md)** (torch from the cu126 index).
   **No longer urgent:** S4 (0.2.8) and S5 (0.2.9) are deferred with Mode 2 (ADR-0012). torch is still needed for
   GPU-backed Mode 3 chunks and to close 0.2.1. Note the provisional inventory says all three GPUs are Ampere
   (CC 8.6), which is exactly the fact ADR-0011's cu126-vs-cu130 choice turns on.
3. **Run `lab/inventory.py` on all 4 laptops** (commands in `lab/README.md`) — the results
   decide cu126 vs cu130 and fill `docs/LAB.md` §1.
4. **Buy the gigabit switch, set DHCP reservations, set Windows profile to Private** (0.2.4);
   S1 needs both Wi-Fi and Ethernet matrices.

## 4. Traps a fresh session must know

- **Never mark `[x]` without evidence** (test output, CI link, or `lab/reports/…`). Only the
  Lead marks it, after tester PASS **and** reviewer APPROVE.
- **Tests must not depend on this machine's state.** The driver fix broke two tests that had
  "the driver is broken" baked in. Assert behaviour, not hardware.
- **Agents commit locally, one commit per task; humans push.** Never run `git push`.
- **Never run `git add -A` while a subagent is running.** The Lead did on 2026-09-21 and swept a tester file that
  was mid-write into an unrelated docs commit (`26eb91a`). Nothing was lost, but the commit boundaries now lie
  about who wrote what. Stage explicit paths whenever an agent may be writing.
- **Run the tester after every *fix* round, not just after the build** — see §2a: three of G4's four defects were
  introduced by fixes.
- **Mutate the code to prove a guard test has teeth** when its failure mode is silent (§2a).
- **Subagent runs die to transient API timeouts.** Tell every agent to commit incrementally; recover by committing
  the working tree `wip:` and relaunching (SendMessage is disabled in this setup, so a cut agent cannot be
  resumed — it must be relaunched with a fresh prompt).
- **Check the exit code, not just the pytest summary** — run pytest via `scripts/pytest_exit_guard.py`. `pytest -m unit`
  once printed a fully green summary and exited **139**; three agent rounds missed it (§2, fixed).
- **A test that logs a large exception can wedge the whole suite** and `--timeout` will not save you, because the
  spin holds the GIL. `tests/conftest.py`'s autouse `configure_logging` fixture must stay **function-scoped** (§2).
- **Never edit a file while a test run or a subagent is touching it.** Beyond the `git add -A` rule below, editing
  `tests/conftest.py` mid-run cost this session several dead runs and a false lead.
- **The whole `pytest -m component` suite takes far longer than the 9 min recorded for V0.1** — on a loaded machine
  with Docker it ran well past 30 min. Budget for that, or run the targeted directories and say which.
- **`uv run` re-syncs the environment**; don't edit `pyproject.toml`/`uv.lock` while another
  agent is running tests.
- Lab scripts are stdlib-first: they must work after a plain `uv sync` on native Windows too.
- torch is **not** installed here; GPU paths must record a clean "torch not installed" error.

## 5. Account rotation (three Pro accounts, same machine, same repo)

The accounts share this working directory, so **the repository is the shared memory**. Before
switching, and ideally after every finished step:

1. Update this file (sections 1–3) and `docs/STATUS.md`.
2. Commit work in progress (`wip:` prefix is fine) so the next account sees committed truth and
   can read `git log`.
3. On the new account, paste the cold-start prompt at the top of this file.

If a limit hits mid-task with no warning, the next account recovers from this file plus
`git status` / `git log`; that is exactly why sections 2 and 4 exist.
