# SESSION HANDOFF — read this first

> **Purpose:** any Claude Code session, on **any of the team's accounts**, can pick up the work
> here without having seen the previous conversation. A usage limit can end a session mid-task,
> so this file is kept current **continuously**, not only at a tidy session end.
>
> **Cold-start prompt** (paste this as the first message on the new account):
>
> ```
> Read docs/SESSION_HANDOFF.md, then docs/STATUS.md, then the current version in
> docs/DEVELOPMENT_ROADMAP.md, and continue the work exactly where it stopped.
> ```
>
> Order of truth: `PROJECT_BRIEF → ARCHITECTURE → ROADMAP → STATUS → this file`. If this file
> disagrees with the roadmap, the roadmap wins and this file is stale — fix it.

**Last updated:** 2026-09-20 (**scope changed — ADR-0012**; S6 in tester) · **Updated by:** Lead session (account 1)

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

**0.2.10 (S6 environments) is with the tester** covering ONE group: the S6 spike **and** the shared
`n_headline()` fix in `_harness/report.py`. Commits `f7b094c` (recovered interrupted implementer run),
`f3364b8` (mypy clean + `lab/spikes/S6.md`), `bfce2fa` (n=0 headline fix). If the tester result is lost, re-run it
from the brief shape above; then reviewer; then the Lead marks 0.2.10 `[~]` (it still needs 🧑 Windows runs) and
records evidence.

**The `n = 0` headline fix (done, awaiting test+review):** `_harness/report.py` used to set the results headline
from `min(n over ALL rows)`, so one skipped case made a healthy table read `**n = 0**`. `n_headline()` now
describes the **valid** rows and counts invalid ones separately. S6 report line 34 went from
`**n = 0** (smallest n of any row)` to `**n = 5** across the 15 valid rows; 4 rows INVALID (n < 5), marked in the
table`. Verified: 19 rows, 4 INVALID marks, raw JSON md5 unchanged. **This renders every spike's report**, so
S1/S2/S3/S7 regressions are the risk.

**TRAP:** `s6_environments.py report --out <existing file>` rewrites the **whole** file from `TEMPLATE.md`,
blanking any hand-written prose (Goal, "Background load", "Commands run", Conclusion) back to placeholders. It was
snapshotted and restored, so nothing was lost — but **never re-run `report` over a report a human has filled in
without a backup.** Every spike's report CLI shares this shape. Worth fixing (preserve human sections, or refuse
to overwrite without `--force`).

**After S6 closes:** 0.2.12 (consolidate findings) once any real lab data exists — do **not** run it on loopback
numbers — then **V0.3, the first version of the new plan**. V0.3 needs the 🧑 inventory of laptops 2–4 first.

Also still open: the S3-review follow-up — promote `spikes._s1.protocol` (address rules, framing, `ProtocolError`)
into `_harness/` so spikes stop importing each other's internals. Shared `_harness` edit → one implementer, then
tester + reviewer, then the whole lab suite.

**Harness rules every spike must respect:** envelope `link` may be `None` (n/a); `load_dir` skip reasons carry only
`path.name`; `envelope.sanitize_text()` on any extra markdown; `report.cell()` for *any* interpolated free text in
a markdown table cell; `_harness/report.py` renders a malformed case entry as one INVALID (n=0) row + note.

**File ownership while several agents run:** each spike owns only its own paths (`lab/spikes/<sN>_*.py`,
`lab/spikes/_sN/`, `tests/**/test_sN_*.py`, `lab/spikes/SN.md`). `_harness/**` is shared — one agent at a time.

**Gate state (measured 2026-09-20 after `bfce2fa`):** `ruff check` / `ruff format --check` clean;
`mypy lab tests/unit/lab tests/component/lab` clean (127 files); `pytest tests/unit/lab` **1223 passed**;
`pytest tests/component/lab` **240 passed, 3 skipped** (~6 min). Nothing is red.

## 3. Blocked on humans (nothing agent-side unblocks these)

0. **🔴 Run `uv run python lab/inventory.py` on laptops 2, 3 and 4** — now the top blocker. **V0.3 cannot start
   without it.** It confirms the hardware ADR-0012 rests on (team-stated, unmeasured: L2 ≈ RTX 3050, L3 ≈ RTX 2050,
   **L4 no GPU**), fills `docs/LAB.md` §1, and settles ADR-0011. If L4 really has no GPU that is *good* — it is what
   gives the planner a real decision to make.
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
