# LEAD2 guide: second lead on L2 (written by LEAD on L1, 2026-11-21)

Read this whole file before doing anything. You are **LEAD2**, a second lead for the project
"Hive" (repo still named MeshForge inside). The main LEAD (on L1) is busy with the evaluation
presentation (23 Nov 2026). You own **two tasks**: D462 ML gate verification and the DEV UI review.
The human is **Aayush**. He wants short, simple answers. Teach in plain technical words.

## 0. Who is who (chat names)
- **LEAD** = main lead, L1 (Ubuntu, JARVISM3, 192.168.31.13). Runs the controller + an agent. Owns the presentation.
- **LEAD2** = you, on L2 (Windows 11, i7-12650H, RTX 3050 A). Owns D462 + DEV UI (below).
- **L1, L2, L3, L4** = lab laptop sessions. **DEV** = 5th computer, UI work, branch `dev/ui`.
  **FIX** = code-fix worker (built D462 on L2, branch `fix/ml-per-term-gate`).
- Human words in chat: `send`, `read`, `pause`, `start`, `stop`.

## 1. Setup from zero on L2 (do with the human)
The human does anything that needs a login or password. You never type passwords or tokens.
1. Check tools: `git --version`, `gh --version`, `uv --version`, `node --version` (20+), `python --version`.
   Install anything missing (winget is fine). Ask the human before installing.
2. Human runs `gh auth login` (GitHub account with access to tony19053000/finalyear).
3. Clone into a NEW folder (do not touch FIX's existing folders on L2):
   `git clone https://github.com/tony19053000/finalyear.git %USERPROFILE%\Desktop\mf-lead2`
4. In it: `uv sync --all-extras` then `cd frontend && npm ci`.
5. Lab chat worktree: `git worktree add -B lab/chat ..\mf-chat-lead2 origin/lab/chat`
   (all chat reads/sends happen in that folder).
6. Read, in order: `CLAUDE.md`, `docs/SESSION_HANDOFF.md` (top + "Known bugs from the final runs",
   "G14 planner calibration fix"), `docs/STATUS.md`, `lab/coord/RULES.md`, `docs/adr/0026-per-part-ml-gate.md`,
   and on branch fix/ml-per-term-gate: `lab/coord/L2/D462-HANDOFF.md`.

## 2. Lab chat (how to talk to LEAD and others)
- Branch `lab/chat`, folder `lab/chat/`, one file per message: `lab/chat/<YYYYMMDD-HHMMSS>-<FROM>-to-<TO>.md`.
- Header `# FROM -> TO: subject`, then `time / step / reply-needed`, then sections:
  What I need / Do this / Expected result / Reply with.
- Send: `git pull --rebase origin lab/chat`, write the file, commit `chat: LEAD2 -> X <subject>`, push. Retry pull --rebase if rejected.
- Read: pull, then read newest files addressed to LEAD2 (and LEAD/ALL if relevant).
- First thing after setup: send `LEAD2 -> LEAD: online` so LEAD knows you exist.

## 3. Hard rules (do not break)
- **Nothing is merged, tagged or released until Aayush himself says "merge".** You prepare; he decides.
- Never push to `main` while a fix session has unpushed work. Never `git stash`. Do not touch untracked `docs/brand/`.
- Workflow for code (CLAUDE.md 4b): implementer -> tester -> reviewer, using the subagents in `.claude/agents/`
  (they carry their own model/effort). You, the lead, do not write product code yourself.
- Never type passwords, tokens or join codes. Join codes go by voice/WhatsApp only.
- Do not change Defender/firewall/security settings yourself. Give the human steps instead, starting with
  "🚨 HUMAN ALERT" and numbered simple steps.
- Lab: before any step that stops a service or touches Wi-Fi, message the humans with context and wait for "ready".
- Do not invent data. Every number must come from a real run.
- Product language: no "Mode 1/2/3/4". Say local execution, job offloading, task splitting.
- Keep `docs/SESSION_HANDOFF.md` current after each finished step and commit work in progress (`wip:` ok)
  ONLY on your own branches, never main.

## 4. TASK A: D462 per-term ML gate (verify, make merge-ready)
**What:** the old ML gate judged all learned terms as one set. In the final campaign `lane_env` failed coverage
(57 % < 70 %), so the whole set was rejected, including env (kNN, 0.44 vs calibration 2.57) and combine
(LightGBM, 0.75 vs 2.85). Dashboard Models tab says "all rejected". D462 judges each term separately.
**State:** branch `fix/ml-per-term-gate`, head **a349b22c** (11c07b98 + "fixture serves field, regen API docs +
schema hash (LF)"). Tester PASS (37 tests), reviewer #2 APPROVE, Windows partial unit run OK. NOT merged.
**Do:**
1. Open a PR from `fix/ml-per-term-gate` to `main` (CI does not run on fix/* without a PR). Body: summary + link to
   `lab/coord/L2/D462-HANDOFF.md`. Do not merge.
2. Wait for CI (Linux full unit + component tiers with Postgres). If red: implementer fixes on the branch -> tester
   -> reviewer. Re-run until green.
3. Check what a349b22c changed (it came after the reviewer's approve): have the reviewer look at that one commit.
4. Live check on the real controller (L1 is the controller, so this needs L1): send `LEAD2 -> L1` (and cc LEAD)
   with the exact steps from D462-HANDOFF.md "How L1 should verify" steps 2-5:
   `compute planner ml train --wait`, `compute planner ml models` (expect new set active, ML PARTS `combine, env`),
   `compute planner ml models --set <version>`, a split job + `compute explain <job>`, dashboard Planner > Models.
   Ask for the pasted output. Note: deploying a branch on L1 must be agreed with the human first.
5. Report to Aayush + `send LEAD`: CI result, review of a349b22c, L1 outputs, "ready for merge: yes/no".
   Then reply to FIX in chat with the outcome.

## 5. TASK B: DEV UI "blended hive skin" (review, test, make merge-ready)
**What:** DEV pushed `dev/ui` **e8e292a3**: Benchmarks + Planner tables restyled, Overview hex hover fix +
smooth pop, page transitions, new login (black-glass card, faint comb), agent console redesign (Overview-style
cards, no theme toggle, QR join scan removed, `?demo=1` demo mode at 127.0.0.1:47130). Frontend only, no API
change. DEV reports tsc + eslint green, vitest 1007/1007. Message: `lab/chat/20261121-161108-DEV-to-LEAD.md`.
**Do:**
1. Fetch `dev/ui`; diff against main; confirm "frontend only" is true (no `src/` Python or API changes).
2. Run `npx tsc --noEmit`, `npm run lint`, `npm test` in `frontend/` on the branch.
3. Reviewer subagent on the diff (correctness, accessibility, nothing broken, QR removal does not break the
   paste-code join path, jsQR can be removed from package.json later).
4. Real test: build the frontend and open the dashboard against the real controller (L1 dashboard is
   https://192.168.31.13:47110, self-signed cert; if the in-app browser rejects it, use Claude in Chrome).
   Check: login page, Overview, Nodes, Planner tabs, Benchmarks, a job page; agent console at 127.0.0.1:47130 on L2.
   Note the dashboard served by L1 is main's build until merged, so testing the new skin live needs the branch
   build served somewhere (vite dev server on L2 pointed at L1's API, or L1 deploy, agree with human first).
5. Report to Aayush + `send LEAD` and `send DEV`: results, screenshots if useful, "ready for merge: yes/no".

## 6. Order and priority
Presentation is 23 Nov 09:00. Both tasks matter because the video/Q&A may show the real dashboard
(ML Models tab, new skin). Do Task A steps 1-3 and Task B steps 1-3 in parallel (non-conflicting), then the live
checks. If something will not be ready by the evening of 22 Nov, say so early; keep main stable.

## 7. After these two tasks
Ask Aayush what next. Other known open items (fix later, not now): stuck lane after a cancelled speculative chunk;
min_nodes > feasible runs unsplit; `ml compare` needs ml extras in the CLI; battery/charger awareness; lab test of
peer-to-peer transfer (built, off by default, never lab-tested); MeshForge -> Hive rename.
