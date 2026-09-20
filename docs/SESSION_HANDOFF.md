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

**Last updated:** 2026-09-20 (**V0.3 G1 + G2 closed agent-side**; G3 next; V0.2 waits on 🧑 lab runs) · **Updated by:** Lead session (account 1)

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

- **Version:** V0.2 (lab setup & feasibility validation). V0.1 is code-complete; its last two
  tasks (0.1.4, 0.1.10) only await the **first real GitHub CI run**, which needs a human push.
- **All four V0.2 spikes are code-complete with reviewer APPROVE** and committed:
  0.2.5 S1 (`ef065ac`), **0.2.6 S2 (`633d474`, closed 2026-09-20)**, 0.2.7 S3 (`81d620f`),
  0.2.11 S7 (`8072b9f`). 0.2.2 `lab/inventory.py` (`75d8df5`). ADR-0011 written, Proposed.
  Every one of these is `[~]` only because it awaits 🧑 runs on real laptops.
- **0.2.1:** Laptop 1's NVIDIA driver is fixed (RTX 3050 Laptop, 595.91.07); still `[~]`
  because `torch.cuda.is_available()` needs torch installed (ADR-0011 / 0.2.3).

## 2. In flight right now

**G3: implementer complete (`feat(V0.3-G3)`); with the TESTER next, then reviewer.** If this
session is lost mid-flight: look for uncommitted files under `src/meshforge/agent/`, `tests/unit/agent/`,
`tests/component/agent/`; run the gates; commit `wip(V0.3-G3)` with what is missing in the message; relaunch an
implementer pointing at that commit. V0.3 **G1 and G2 are closed agent-side** (2026-09-20).

- **G1** (`da27ff1` → `0acf7eb`): proto V0.3 contract, node identity (ADR-0013), liveness logic. 0.3.1 `[x]`; 0.3.2 `[~]`
  until a 🧑 Windows unit run (`uv run pytest tests/unit/agent -q`, exercises `icacls` + `msvcrt.locking`).
- **G2** (`bd416eb` → `a1014d5`): controller session server, registry (`0002_nodes`), lease wiring, ping/pong RTT, one
  process (gRPC 47100 + REST 47110 `/healthz`). 0.3.6 `[x]`, 0.3.10 `[x]`, 0.3.7 `[~]` only for the 🧑 cable-pull
  evidence in 0.3.15. Two implementer runs were cut by usage limits; partial work was always committed as `wip:` first —
  keep doing that. Reviewer pass 2 APPROVE; follow-ups in the roadmap design change log (2026-09-20 "G2 review
  follow-ups" row). **Dev-DB notice:** migration `0002_nodes` was amended in place; a dev Postgres migrated between
  `226506e` and `35bc872` must be dropped or `alembic downgrade 0001 && upgrade head`.

**Next: G3 (agent runtime + discovery, 0.3.8 + 0.3.9).** The Lead writes the brief as §5c of
`docs/briefs/V0.3-fabric-core.md` from G2's real contract (see STATUS → Current Task). Musts for G3 from G2:
`message_id` = `cmd_<ULID>` (`common.ids.new_command_id`); heartbeat interval and lease come from `HelloAck`, never
hard-coded; on stream end read the gRPC status — `ABORTED` + details `superseded` → stop (another instance of this
node won), `lease_expired` / `protocol_error` → reconnect with backoff, `OK` → controller went away cleanly → reconnect;
`STALE_EPOCH` in `HelloAck` → call `identity.next_epoch` (via `asyncio.to_thread`) and retry; answer every `Ping` with
`Pong` immediately; never derive anything from `controller_time` except display skew. Gates at handoff: ruff /
mypy (49 files) / lint-imports / gen_protos clean, unit **2259**, component store+controller **67**, controller
component file **10/10** runs.

**Documented loss in 0.2.13, not a bug:** hand-edits *inside* a generated region (e.g. filling
"speed: TBD" or the power-mode cell in the host table) are still overwritten. Recorded in
`lab/README.md`. Making those cells human is a design change worth an ADR.

**0.2.10 (S6) closed agent-side 2026-09-20** — tester PASS (no defects, 16 adversarial tests),
reviewer APPROVE (no blocking findings). Commits `f7b094c` `f3364b8` `bfce2fa` `fb9376a`
`c97f013`. Real Laptop 1 run: **the GPU is visible inside a container** (nvidia-smi 361 ms), cold
pull 9.7 s, venv cold 3.56 s vs warm **0.10 s** (V0.5 criterion 5 wants ≤ 3 s job-start overhead,
so the warm path has plenty of room). **Stays `[~]`:** `docker-gpu-torch` was skipped as typed
`insufficient_disk` (5.8 GB free, image ~3.7 GB), so `torch-cuda-in-container` is **TBD on every
laptop**, ARCHITECTURE Q4 is unanswered, and Windows/Docker Desktop has **zero** evidence.

**Remaining non-blocking follow-ups from the 0.2.10 review** (logged in the roadmap design change
log, none started): cross-check `summary.n == len(samples)` and render INVALID on mismatch (a
better honesty check than any upper bound on `n`) · `TEMPLATE.md:34` hardcodes "n < 5" while all
code uses `MIN_VALID_REPS` · `_s6/run.py` `_cleanup` removes **every** `s6-` container, so two
concurrent S6 runs on one host would kill each other's.

**After 0.2.13:** all V0.2 agent-side work is done. **0.2.12 must wait for real lab data** — never
run it on loopback numbers. The next real work is **V0.3, the first version of the new plan**, and
it is blocked on the 🧑 inventory of laptops 2–4. The S3-review follow-up (promote `_s1.protocol`
into `_harness/`) is **done**: `_harness/protocol.py` holds framing + LAN address rules;
`test_harness_protocol_boundary.py` fails if any non-S1 spike imports `spikes._s1` again.

**Harness rules every spike must respect:** framing / address rules come from `_harness/protocol.py`, never from another spike's package · envelope `link` may be `None` (n/a); `load_dir` skip
reasons carry only `path.name`; `envelope.sanitize_text()` on any extra markdown; `report.cell()`
for *any* interpolated free text in a markdown table cell; `_harness/report.py` renders a
malformed case entry as one INVALID (n=0) row + note; report writes go through `write_report()`.

**File ownership while several agents run:** each spike owns only its own paths
(`lab/spikes/<sN>_*.py`, `lab/spikes/_sN/`, `tests/**/test_sN_*.py`, `lab/spikes/SN.md`).
`_harness/**` is shared — one agent at a time.

## 3. Blocked on humans (nothing agent-side unblocks these)

0. **🔴 Run `uv run python lab/inventory.py` on laptops 2, 3 and 4** — now the top blocker. **V0.3 cannot start
   without it.** It confirms the hardware ADR-0012 rests on (team-stated, unmeasured: L2 ≈ RTX 3050, L3 ≈ RTX 2050,
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
