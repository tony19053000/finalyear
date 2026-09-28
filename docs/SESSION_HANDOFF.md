# SESSION HANDOFF — read this first

> Any session, on **any of the team's accounts**, continues from here without seeing the previous conversation.
> Kept current **after every finished step**. Kept **short on purpose** (token cost — every session and agent reads
> it). Full history up to 2026-09-28: [`archive/SESSION_HANDOFF-history-to-2026-09-28.md`](archive/SESSION_HANDOFF-history-to-2026-09-28.md)
> — read a section there only when a line below points to it.
>
> **Cold-start prompt** (paste as the first message on the new account):
>
> ```
> Read docs/SESSION_HANDOFF.md, then docs/STATUS.md, then only the parts of
> docs/briefs/V0.4-remote-execution.md that the next step names. Check git log and docs/briefs/progress/.
> Do the "Exact next step" in §2. RULE: ONE subagent at a time.
> ```
>
> Order of truth: `PROJECT_BRIEF → ARCHITECTURE → ROADMAP → STATUS → this file`. If this file disagrees with the
> roadmap, the roadmap wins — fix this file.

**Last updated:** 2026-09-28 · **By:** Lead session

---

## 0. 🔒 DIRECTION LOCK — do not drift from this

**What we are building now:** V0.4 **Remote execution core** = the foundation of **Mode 1 (Job Offloading)**: a user
submits a job (manifest + bundle) → the controller stores it → dispatches it to one chosen laptop → the agent runs it
in Docker (or an opted-in subprocess) → logs and outputs come back. Plan: `docs/briefs/V0.4-remote-execution.md`.

**Where the whole project is going** ([ADR-0012](adr/0012-scope-ship-mode3-complete.md) — still *Proposed*, but it is
the plan everyone works to): **Mode 3 (Task Splitting) complete and production-grade, Mode 1 as its foundation, the AI planner in
full** (V0.9 telemetry → V0.10 Mode 3 → V0.14 planner v2 → V0.15 ML planner). Version order:
V0.4 → V0.5 → V0.6 → V0.7 → V0.8 → **V0.9 before V0.10** → V0.14 → V0.15 → V0.17 → V1.0.

**Must NOT be started (stop and ask the team if you see it happening):**
- **Mode 2 (V0.11) and Mode 4 (V0.12, V0.13, V0.16) — DEFERRED.** Also deferred: 0.2.8 (S4), 0.2.9 (S5).
- Anything from a later version or another group while a group is open. One group at a time, in order.
- Re-planning or changing scope on your own. Scope changes = team decision + ADR.

**If the plan must shrink** (calendar ~5 weeks from 2026-09-21, ~2026-10-26, unconfirmed): cut in this order only —
**V1.0 → V0.17 → thin V0.8 → V0.5 polish. Never V0.10, V0.15 or V0.9.** (Why: archive §0, §0b, §0d.)

**`CLAUDE.md` and `PROJECT_BRIEF.md` still describe four modes on purpose** — they wait for formal team approval of
ADR-0012. This section and the roadmap's DEFERRED banners are what apply.

## 1. Working rules (team decisions — not advice)

1. **ONE subagent at a time** (2026-09-28, token cost). Wait for it to finish before starting the next.
2. **Per group:** implementer → tester → (FAIL → implementer fix → **tester again**) → reviewer → (REQUEST_CHANGES →
   fix → tester → reviewer) → Lead marks `[x]` with evidence. **Never skip the tester or reviewer, and re-test after
   every fix** — most past critical bugs were introduced by fixes.
3. **Only the Lead** marks `[x]`, edits this file, STATUS and the roadmap. Agents append to
   `docs/briefs/progress/V0.4-G<n>.md` and commit with explicit paths (`git commit -- <paths>`). Nobody pushes.
4. **Token rules (2026-09-28)** — quality stays the same, waste goes:
   - Briefs give each agent a **short reading list** (exact sections/files, or the needed lines pasted in), never
     "read ARCHITECTURE" whole.
   - Agents run pytest with `-q` and look only at the summary + failures (`scripts/pytest_exit_guard.py`, read the
     **exit code**). Never paste full test logs into a report.
   - Tester: cover **every success criterion and every bound** (max ok, max+1 rejected) + 2–3 mutation checks; no
     open-ended attack lists beyond that.
   - Reviewer: reviews **the group's commits (`git diff <first>^..<last>`) + the tester report**, not the whole repo.
   - Reports ≤ 500 words. **One Lead session per group** (fresh session per group, not one endless session).
   - Models (`.claude/agents/*.md`): implementer **Opus 5.5** (team decision 2026-09-28: the implementer is never
     downgraded to save tokens), tester Sonnet 5, reviewer Opus 5.5.
5. Tests never depend on this machine (GPU, Docker, hostname); Docker/GPU tests SKIP with a reason elsewhere.

## 2. Where we are · exact next step

**V0.4 groups** (brief §3): G1 session hardening · G2 job model & submission · G3 runtimes & sandbox · G4 dispatch &
execute · G5 logs, outputs, CLI, examples · then 🧑 0.4.15 lab run.

| Group | State |
|---|---|
| **G1** | ✅ **CLOSED** — 0.4.16 `[x]` (tester PASS `6f545ca`, reviewer APPROVE). Its 4 non-blocking follow-ups = **D61**, done inside G4. |
| **G2** | Code done (`1462e34` … `f92e4fd`). **Tester round: NOT yet run** (an earlier round was stopped; its files are parked). |
| **G3** | Code done (`d625a45` … `b14baa3`). Tester + reviewer not yet run. |
| G4 | Brief not written yet (scope fixed in brief §5d + D55, D60, D61). |
| G5 | Brief not written yet (scope in brief §5d). |

**➡ Exact next step: G2 tester round.** Spec = brief §5b's TESTS section + roadmap 0.4.1–0.4.5 criteria + §4
checklist. Reuse the parked G2 files in `docs/briefs/progress/parked-tests/` (see its README); leave the G3 ones.
Accepted, not defects: D55 (3 missing transition arrows, G4 adds them), D56, "QUEUED stays QUEUED".
**Then:** G2 reviewer → G3 tester → G3 reviewer → write G4 brief → G4 → G5.

**G3 warning for its tester:** the parked test `test_run_phase_network_is_off_even_though_setup_phase_had_it_on`
FAILED once — the run container may have network when the setup container had it. Reproduce this first.

**Team to review (not blocking the next step):** ADR-0014 (fabric token + execution opt-in), D40 (timing-based
close replaced by explicit Goodbye), D55–D61 (brief §2f).

## 3. Blocked on humans 🧑 (agents cannot do these)

- `uv run python lab/inventory.py` on laptops 2, 3, 4 (hardware is team-stated only: L2 ≈ RTX 3050, L3 ≈ RTX 2050, L4 no GPU).
- GitHub push + first CI run (closes V0.1) — steps in STATUS "Next Recommended Task". **Agents never push.**
- 0.3.15 lab run + 24 h soak (`lab/checklists/V0.3-fabric.md`); Windows checks for 0.3.2, 0.3.13, G7 (STATUS "In Progress").
- S2 / S3 two-laptop spike runs; Docker (+ NVIDIA) on laptops 2–4 for 0.4.15; Laptop 1 needs ~12 GB free disk for the pytorch image.
- Keep `meshforge-agent` running on all laptops — V0.15's training data is calendar-bound.

## 4. Traps (short; details in archive §2a, §4)

- A green pytest summary is not a green run — **check the exit code** (once exited 139 after all-green).
- A test that passes for the wrong reason is worse than none — mutation-check guards whose failure is silent.
- Never `git add -A` / `git commit -a`. Never edit a file a running test or agent is touching.
- Don't edit `pyproject.toml` / `uv.lock` while tests run (`uv run` re-syncs). New deps are added by the Lead only.
- `tests/conftest.py`'s autouse `configure_logging` fixture must stay **function-scoped**.
- Whole `-m component` suite ≈ 13+ min (longer under load) — run it **once per group**, by the tester.
- torch 2.14 (CUDA 13.0, no numpy) is in this venv only; code must give a typed "torch not installed" elsewhere.
- Restarting the desktop app kills running sessions — check nothing is mid-task first.

## 5. Switching accounts

Before switching (and after every finished step): update §2 of this file + `docs/STATUS.md`, commit (`wip:` is fine).
On the new account paste the cold-start prompt above. If a limit cut an agent mid-run: read its progress file +
`git status` / `git log`, commit its finished work `wip:`, and relaunch it with "resume from <last done item>".
