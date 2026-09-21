# SESSION HANDOFF — read this first

> **Purpose:** any Claude Code session, on **any of the team's accounts**, can pick up the work
> here without having seen the previous conversation. A usage limit can end a session mid-task,
> so this file is kept current **continuously**, not only at a tidy session end.
>
> **Cold-start prompt** (paste this as the first message on the new account):
>
> ```
> Read docs/SESSION_HANDOFF.md (including section 0, the scope change), then
> docs/adr/0012-scope-ship-mode3-complete.md, then docs/STATUS.md, then the current
> version in docs/DEVELOPMENT_ROADMAP.md, and continue the work exactly where it stopped.
> ```
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

**Last updated:** 2026-09-21 (**V0.3 G4 closed — 0.3.3 / 0.3.5 / 0.3.11 `[x]`; G5 in flight**; V0.2 still waits on 🧑 lab runs) · **Updated by:** Lead session (account 2)

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

## 1. Where the project is

- **Version: V0.3 (fabric core).** **G1–G4 are closed agent-side; G5 is in flight.** A live controller ⇄ agent
  fabric works in-process: identity, sessions, leases, RTT, reconnect, mDNS discovery, hardware discovery,
  resource monitoring and downsampled snapshots in Postgres.
  - `[x]` 0.3.1, 0.3.3, 0.3.5, 0.3.6, 0.3.8, 0.3.9, 0.3.10, 0.3.11
  - `[~]` 0.3.2 (awaits a 🧑 Windows unit run), 0.3.7 (awaits 🧑 lab evidence in 0.3.15),
    0.3.12 + 0.3.13 (G5, in flight)
  - Remaining: G6 (0.3.14 cluster harness + fault injection), G7 (0.3.4 hardware scores), 🧑 0.3.15 lab run
- **V0.2:** all agent-side spike work is complete with reviewer APPROVE (S1 `ef065ac`, S2 `633d474`, S3 `81d620f`,
  S7 `8072b9f`, inventory `75d8df5`, 0.2.13 done). Every one is `[~]` **only** because it awaits runs on real
  laptops. **0.2.12 must wait for real lab data — never run it on loopback numbers.**
- **V0.1:** code-complete; 0.1.4 and 0.1.10 await the first real GitHub CI run, which needs a human push.
- **0.2.1:** Laptop 1's NVIDIA driver is fixed (RTX 3050 Laptop, 595.91.07); still `[~]` because
  `torch.cuda.is_available()` needs torch installed (ADR-0011 / 0.2.3).

## 2. In flight right now

**V0.3 G5 — the operator surface (0.3.12 REST + CLI, 0.3.13 documented service units).** The brief is
`docs/briefs/V0.3-fabric-core.md` **§5e** with the Lead decisions **D13–D17** in the table above it; both tasks are
`[~]`; the implementer was spawned 2026-09-21 from `bd05e74`. Nothing needs re-deriving — §5e is complete and
self-contained.

**If this session died mid-G5:** run `git status`. If there is uncommitted G5 code, commit it `wip:` first, then
either re-run the implementer on §5e telling it what already exists, or continue directly. Then tester → reviewer →
the component suite **once** → the Lead marks `[x]` with evidence → write the **G6** brief.

The five G5 decisions a later session must not quietly reverse:
- **D13** `HardwareFacts` is **not** served over REST — the controller only ever receives `Hello.hardware`, so the
  probe statuses/durations/degraded reasons (agent-side only) are exposed by a local
  `meshforge-agent hardware show`. Putting them on the wire is a **V0.8** conversation, not a G5 edit. *(This
  corrects an earlier STATUS note that said G5 would serve them over REST.)*
- **D14** the REST surface is **read-only** and unauthenticated in G5, LAN-bound. `CLAUDE.md` §9 allows assuming a
  trusted LAN early; it does not allow a mutating unauthenticated endpoint. Hence `ResourceTracker.forget_node()`
  is still unnecessary — **it becomes required with the first node-delete path.**
- **D15** `httpx` moves from the `agent` extra to core; the CLI speaks REST only (a CLI importing `controller`
  breaks the boundary lint-imports enforces).
- **D16** `state` / `connected` / `sample_age_s` stay three separate fields — they disagree legitimately (a node can
  be ONLINE in the database while the controller that wrote it has restarted), and V0.8's dashboard is built on
  this shape.
- **D17** service units are documented and **printed, never auto-installed**.

### 2a. What G4 cost, and what it should change about how we work

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
all of them in the telemetry V0.15's ML planner will train on.

**A test that passes for the wrong reason is worse than no test.** Two instances in G4: the original component test
claiming "a replay is not counted twice" never performed a reconnect, and the ISO-1 defect pin passed via
`AttributeError` because its stub lacked `ping()` — it would have passed with the ping logic inverted. When a test
guards something whose failure is *silent*, mutate the code and confirm the test fails.

**Still open from G4** (details in the roadmap design change log row "V0.3 G4 review follow-ups"):
- **🧑 NB-2, a team decision** — where NVML is missing but `nvidia-smi` works, a node reports its GPUs in
  `Hello.hardware` but **never** GPU util/VRAM in any `ResourceUpdate`. V0.9 telemetry and the V0.15 planner would
  train on a node that looks GPU-equipped and never uses it. Option to cost: a low-cadence (~30 s, interpolated)
  `nvidia-smi` poll behind a setting.
- **G6:** the component harness cannot express "controller stays up, the client's TCP drops and reconnects" — it
  restarts the controller, destroying the in-memory tracker. **That gap is why R1 survived the first round.**
- **G6:** the NB-1 ordering invariant in `client.py` (no `await` between the ONLINE transition and
  `_reconcile_and_replay()`) is enforced by a comment plus a *component* test that the token policy runs once per
  group — a unit-gate edit will not see it. Structural fix: an `asyncio.Event` set after the reconcile and awaited
  by `send_report`.
- **Flusher:** during a real outage it issues up to 100 individually failing statements before asking the ping;
  probe the first row, then ask `_database_answers()` immediately.
- **V0.4:** `manager.py` is ~913 lines and still on the extraction list.

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
