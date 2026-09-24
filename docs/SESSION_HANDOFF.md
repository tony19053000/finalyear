# SESSION HANDOFF — read this first

> **Purpose:** any Claude Code session, on **any of the team's accounts**, can pick up the work
> here without having seen the previous conversation. A usage limit can end a session mid-task,
> so this file is kept current **continuously**, not only at a tidy session end.
>
> **Cold-start prompt** (paste this as the first message on the new account):
>
> ```
> Read docs/SESSION_HANDOFF.md (sections 0, 0b and 2 especially), then docs/STATUS.md, then
> the current version in docs/DEVELOPMENT_ROADMAP.md. G5 is closed; the next agent-side step
> is to write the V0.3 G6 brief (0.3.14) as docs/briefs/V0.3-fabric-core.md §5f and spawn it.
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

**Last updated:** 2026-09-24 (**G5 CLOSED agent-side**; **models moved to Opus 5.5** — see §0c; **G6 is next and its brief is not written yet**; the exit-139 fix task was killed and must be relaunched) · **Updated by:** Lead session (account 2)

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
| tester | `claude-sonnet-5` (unchanged) | medium | `.claude/agents/tester.md` |
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

**The app restart killed the exit-139 fix task** (started in its own session just before). It left no worktree
and no commits, so **it must be relaunched** — see §2. Restarting the desktop app kills every running session;
check nothing is mid-task first.

---

## 1. Where the project is

- **Version: V0.3 (fabric core).** **G1–G5 are closed agent-side.** A live controller ⇄ agent fabric works
  in-process: identity, sessions, leases, RTT, reconnect, mDNS discovery, hardware discovery, resource monitoring,
  downsampled snapshots in Postgres — **and now an operator surface**: a read-only REST `/v1`, a `compute` CLI that
  speaks REST only, `meshforge-agent hardware show`, and documented (never auto-installed) service units.
  - `[x]` 0.3.1, 0.3.3, 0.3.5, 0.3.6, 0.3.8, 0.3.9, 0.3.10, 0.3.11, **0.3.12**
  - `[~]` 0.3.2 (awaits a 🧑 Windows unit run), 0.3.7 (awaits 🧑 lab evidence in 0.3.15),
    **0.3.13** (awaits the 🧑 Windows logoff check in `docs/OPERATIONS.md` §3 — the docs say "unverified" on
    purpose; do not let anyone replace that with a guess)
  - Remaining: **G6** (0.3.14 cluster harness + fault injection — *brief not written yet*), G7 (0.3.4 hardware
    scores), 🧑 0.3.15 lab run
- **V0.2:** all agent-side spike work is complete with reviewer APPROVE (S1 `ef065ac`, S2 `633d474`, S3 `81d620f`,
  S7 `8072b9f`, inventory `75d8df5`, 0.2.13 done). Every one is `[~]` **only** because it awaits runs on real
  laptops. **0.2.12 must wait for real lab data — never run it on loopback numbers.**
- **V0.1:** code-complete; 0.1.4 and 0.1.10 await the first real GitHub CI run, which needs a human push.
- **0.2.1:** Laptop 1's NVIDIA driver is fixed (RTX 3050 Laptop, 595.91.07); still `[~]` because
  `torch.cuda.is_available()` needs torch installed (ADR-0011 / 0.2.3).

## 2. In flight right now

**Nothing is mid-task.** G5 closed cleanly on 2026-09-24 and the working tree is committed. The next agent-side
step is a **Lead** step: write the **G6** brief (0.3.14 cluster test harness + fault injection) as
`docs/briefs/V0.3-fabric-core.md` **§5f**, following the §5d/§5e pattern — Lead decisions first, then the pasteable
brief with the §4 checklist — then spawn it.

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

### 🔴 The unit suite's exit code is not green — read this before pushing

`uv run pytest -m unit -q` prints `3214 passed, 2 skipped, 2 xfailed` and then **exits 139 (SIGSEGV)**.
Pre-existing, reproduced with and without this session's changes. faulthandler shows
`Fatal Python error: Segmentation fault` in a native thread with `<no Python frame>` — a C-level crash at
interpreter shutdown. Each of `tests/unit/{cli,common,agent,controller}` exits **0** alone; only the whole
`tests/unit` set crashes, and the top-level files are the ones importing grpc/protobuf, so **`grpcio`'s C core is
the prime suspect — not yet proven.**

**This will make the first GitHub Actions `unit` job red no matter how many tests pass, so it blocks closing V0.1
(0.1.4, 0.1.10).** It survived three agent rounds because every one of them reported pass counts and none checked
the exit code. **A green summary is not a green run.** Raised as its own task — **which was killed by a desktop app restart on 2026-09-24 before committing anything, so it must be relaunched** (the prompt is in `docs/STATUS.md` → Next Recommended Task).

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
matters for the exit-139 hunt: **the component tier exits 0 while the unit tier exits 139.**

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
- **Check the exit code, not just the pytest summary.** `pytest -m unit` currently prints a fully green summary and
  then exits **139**. Three agent rounds missed it (§2).
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
