# DEV HANDOFF — read this first (any account)

Updated 2026-11-19. Cold-start prompt: see bottom of this file.

## START HERE ON THE NEW LAPTOP — detailed cold-start prompt (2026-11-19)
Setup steps: `NEW-LAPTOP.md`. Then paste this whole prompt into a new Claude Code session opened on the folder `UI MeshForge`:

```
You are the MeshForge DEV workstation agent (lab chat name DEV), now on a NEW laptop. The human moved here from the
old DEV laptop; everything was pushed to GitHub branch dev/ui. Your job first is to rebuild full context so it feels
like the same machine, then continue exactly where we stopped.

Layout: folder "UI MeshForge" contains mf-dev (repo tony19053000/finalyear, branch dev/ui) and mf-chat (worktree of
branch lab/chat). Hard rules: never push to main or lab/*; never touch L1-L4 or their files; push only dev/ui (only when
the user says) and lab/chat (chat messages). Never commit wsl-controller-test-creds.txt or any secret.

STEP 1 - setup check (fix what is missing, tell the user in one line each):
- `git -C mf-dev status` on dev/ui and up to date with origin/dev/ui; mf-chat worktree exists (else
  `git -C mf-dev worktree add -f ../mf-chat origin/lab/chat`).
- node/npm/python on PATH; `npm ci` done in mf-dev/frontend; `.claude/launch.json` exists (copy from
  mf-dev/lab/coord/DEV/carry/claude/launch.json; runtimeExecutable "node" must resolve).
- Copy mf-dev/lab/coord/DEV/carry/memory/*.md into your own memory folder and index them in MEMORY.md.

STEP 2 - read ALL context, in this order, fully:
1. mf-dev/lab/coord/DEV/NEW-LAPTOP.md
2. mf-dev/lab/coord/DEV/HANDOFF.md - every section; newest first (2026-11-19, then 2026-11-18 late, then LATEST).
3. mf-dev/lab/coord/DEV/STATUS.md and NOTES.md, SCREENSHOTS.md
4. mf-dev/lab/coord/RULES.md (DEV section; `send` / `read` lab chat rules) and README.md, ANNOUNCE.md (top)
5. Newest LEAD-to-DEV chat files in mf-chat/lab/chat (git -C mf-chat pull --rebase origin lab/chat first) -
   especially 20261119-185955-LEAD-to-DEV.md (the current orders).
6. mf-dev/lab/coord/DEV/carry/ref/NOTES.md and RESEARCH.md (design references; frames in carry/ref/frames-b).
7. mf-dev/CLAUDE.md and mf-dev/docs/PROJECT_SUMMARY.md (what the product is).
8. Skim the dashboard code to know it: mf-dev/frontend/src/app (Header, MainNav, App routes), src/pages/* (overview,
   nodes, jobs, benchmarks, planner, settings, guide), src/demo/* (demo mode data + fetch interception),
   src/styles/index.css (all page styles: ov-*, jb-*, bm-*, pl-*, st-*, gd-*, nl-*), src/components/jobs
   (ListboxField, DateTimeField). Website: mf-dev/website (index.html, assets/js/home.js, bees.js, 3D Buzz model).
9. `git -C mf-dev log --oneline -15 origin/dev/ui` to see the latest commits.

STEP 3 - prove it runs, show the user:
- Start preview "dash" (dashboard dev server, http://localhost:5173) and open http://localhost:5173/?demo=1 - the
  Overview must show demo laptops, jobs, speed-up. Click through Nodes, Jobs, Benchmarks, Planner, Settings, and the
  bell -> /guide. Demo mode fakes an admin session, so no controller is needed.
- Start preview "site" (landing website, http://localhost:8766) - robotic bees + 3D Buzz must work.
- Run in mf-dev/frontend: tsc -b --noEmit, eslint src, vitest run. Known failing: g2_tester theme-toggle.
- Tell the user in a few lines: what is set up, what works, anything missing.

STEP 4 - continue where we stopped (LEAD's orders, in order; small steps, the user reviews each):
 3a remove the demo fake admin session from src/demo/install.ts (keep ?demo=1 localhost-only, no auth bypass) -
    decide with the user how the demo then signs in (e.g. demo shows pages with a real test login), ask first.
 3b fix the old g2_tester theme-toggle test; review the 6 changed g5_settings tests (row actions behind "⋯").
 3c npm test, npm run check:api, tsc, eslint all green.
 3d short DEV note listing every changed page -> send LEAD.
 Then: lab/coord/DEV/SCREENSHOTS.md (label the 36 controller screenshots), then the website with LEAD's content
 draft (numbers must stay real), then video-ready pages (Overview speed-up card, a split job, Planner evaluation).

Way of working (the user is strict): the user reviews every step; no reviewer agent; implementer subagent = Opus,
high effort, with a full self-contained brief. Small steps, show screenshots at 1920x830. Taste: clean, sparse, few
boxes, plain tables on the page background with hairlines (preferred over frosted glass), no pills/glow, mono
uppercase small labels, light big numbers, even spacing, explanations in tooltips; settings pages calm, not
dashboard-like. Demo must mirror the real system "same to same" - only data replaced, never change routes/APIs.
No inline style={} (CSP). Node may need a full path if not on PATH. Back up frontend/src before each big change
(frontend-src-backup-vNN, local only). `send LEAD` progress after each step. Short, point-to-point messages.
```

## NEWEST (2026-11-19) — moving to another dev laptop; read FIRST (supersedes everything below)
- **New laptop: follow `NEW-LAPTOP.md` (setup + cold-start prompt).** Work pushed: dev/ui `8f5db33d` + setup commit. References/memory/launch.json in `carry/`.
- **All dashboard work is UNCOMMITTED on `dev/ui` (~160 files) on the old laptop.** It must reach the new laptop (wip commit + push to `dev/ui` on user's OK, or copy the folder) before anything else.
- **Done since 2026-11-18 (all user-reviewed):** whole dashboard zoomed to 0.9 (`--mf-zoom` in index.css). Jobs: filter popover, custom ListboxField/DateTimeField, round honey "+" submit, sticky header fading in only on scroll, table now PLAIN (no frosted box, hairlines; `.nl-plain .jb-plain`). Submit-a-job restyled. Benchmarks + Planner (Calibration/Evaluation/Models) restyled with demo data (`src/demo/benchDemoData.ts`, `plannerDemoData.ts`); Evaluation regret bars custom (SVG, value column); Models charts split by a thin line. Settings redesigned as a calm settings page (one column, intro + labelled honey button, plain rows, "⋯" row menu, Account as label/value sections; demo data `settingsDemoData.ts`). Overview: bell on Job progress / Speed-up opens the card like the arrow, showing only short points. Header bell → new **Product guide** page `/guide` (`src/pages/guide/GuidePage.tsx`, `.gd-*` CSS), visible to all users. Nodes list table PLAIN (`.nl-plain`), detail pages untouched.
- **User taste update:** for tables the user prefers PLAIN (on page background, hairlines) over frosted glass for look/cleanliness. Settings must not look like a dashboard. Demo must mirror the real system "same to same" — only data replaced.
- **Demo caveat:** `src/demo/install.ts` fakes an admin session (added while WSL was broken). WSL works again — remove the fake session when convenient. Demo mutations fail on missing CSRF cookie (accepted).
- **Backups:** `mf-dev/frontend-src-backup-v19`…`v27` (v23 before Settings, v24 before Settings redesign, v25 before guide, v26 before Nodes table, v27 before Jobs table).
- **Tests:** tsc/eslint clean; Planner 138, Jobs 445, Settings/shell/demo 59 pass; known failing: g2_tester theme-toggle.
- **Next:** ask user. Candidates: plain tables on Benchmarks/Planner too, remove demo fake session, fix g2 theme test.
- **New laptop setup:** Node path, WSL controller and test creds (`wsl-controller-test-creds.txt`, never commit) are machine-specific — recheck them.

## NEWEST (2026-11-18 late) — dashboard state, read FIRST (supersedes LATEST below)
Dashboard restyle well advanced; user reviews every step. Demo data: append `?demo=1` (localhost only; `src/demo/`): 5 laptops (asha, lab-pc-07, ravi, old-desktop suspect, mini-pc offline), 21 jobs (split/sent-away/local rotate every 20 s + 18 failed), 20+ alerts, node detail/network/caches/policy/scores, job attempts/logs. Login needed after a session restart (creds file; user granted read access).
- **Overview (DONE, approved):** top row Job progress (mode label, split real % / est % for local+sent away, ↗ expands right over Speed-up showing per-node WORKING ON/DONE rows or step line) · Speed-up honey card (count-up number, frosted local-vs-hive pills, ↗ expands left: job name+id, options rows node names/MODE/time, chosen full strength, no tick/no bars) · Resources pie (equal slices, auto-cycles CPU→RAM→GPU, centre = total; ↗ widens left with per-node cores/RAM/VRAM table, columns centred, header "node"). Load card (legend labels only, faint grid, frosted tooltip; ↗ unfolds down = per-laptop charts, offline excluded) / Alerts (3 shown; count button expands up, Load folds). Nodes honeycomb fills card, fades under text, laptops in balanced zigzag, idle drift, hover lifts + honey ring + frosted hardware box (cores/RAM/VRAM, "—" no GPU), offline = black prism, counts top-right. Top-of-page comb has cursor glow (app/HiveBackdrop.tsx). Rejected & reverted: lit/drifting comb, brighter comb behind glass, open-ring pie, area fills.
- **Nodes list (DONE):** title + centred frame-B text tabs (All·Online·Suspect·Offline·Draining·Enrolling·Revoked, always shown, counts); frosted table (.ov-card.ov-frost.ov-table-fit.ov-table-big — BIGGER fonts ONLY here), no Status/Heartbeat columns, state dot right of name, even column widths + equal end padding, tall rows, honey hover edge.
- **Node detail (DONE, iterating):** sticky name block (see-through until stuck), open facts strip, text tabs. Live: 3 shorter cards CPU/Memory/GPU (no "live · last 5 min"/samples lines). Hardware: Blender PC tower (`frontend/blender/pc_setup.py`, single angle, darker, transparent WebP in `public/pc-setup/`, live CPU/RAM/GPU light layers, cursor tilt) + specs column (CPU + GPU as heroes, VRAM big, storage drives inside column, OS+capabilities last row) — all fits one screen. Network/Caches & policy frosted tables; policy uses plain names, "not enforced" inline, no warning/command blocks, both cards equal height.
- **Jobs (DONE, first pass):** list with text tabs + filters + frosted table (mode/nodes/progress/speed-up); job detail with facts strip, tabs Progress/Chunks/Plan/Attempts/Logs/Timeline/Outputs/Manifest.
- **Open question to user:** shrink the whole dashboard ~10–12% (feels "zoomed in" at 1920)? Not answered yet.
- **Next:** Benchmarks, Planner, Settings pages in the same language (ask the user first; small steps). Then fix known failing test g2_tester theme toggle.
- **User taste (strict):** clean/sparse, few boxes, no glow/bloom, no pills, no decorative bars, mono uppercase small labels, light big numbers, content spread evenly, nothing cramped/touching, smooth subtle motion, desktop only, revert immediately when asked. Discuss before big redesigns; never ship a whole-page redesign without showing (the first Nodes redesign attempt was rejected and reverted).
- **Backups:** `mf-dev/frontend-src-backup-v14`…`v18` (v15 = Overview done; v16 before node detail; v17 before Jobs; v18 before Blender tower). Stashes: `stash-pre-v10-restore`, `stash-nodes-attempt`. Leftovers: `frontend/blender/_work` (~30 MB) + `blender/renders` can be deleted/ignored.
- Checks: tsc + eslint clean; vitest all pass except known g2_tester theme-toggle (one benchmark download test flaky). Nothing committed.

## LATEST (2026-11-18) — dashboard work, read first
Current focus = controller DASHBOARD (React, `mf-dev/frontend`). Landing page parked (user will return later).
- Team now: implementer = Opus high. **Reviewer agent PAUSED — the user reviews.** (If reviewer resumes: Sonnet 5.5 medium.)
- Dashboard dev server: http://localhost:5173 (launch.json "dash" / `node lab/coord/DEV/dash-dev.mjs`), proxies to WSL controller http://127.0.0.1:47111 (service meshforge-controller; start WSL keepalive first: `wsl -d Ubuntu -u root -- sleep 7200`). Test login in wsl-controller-test-creds.txt. Node not on PATH: `C:/Users/HP/AppData/Local/OpenAI/Codex/runtimes/cua_node/db1cc9e06a6c48d6/bin/node.exe`.
- History: first full redesign (sidebar+overview) and a b/d-video rework were REJECTED by user ("worse") -> restored original; rejected redesign kept in `mf-dev/frontend-redesign-discarded`.
- Then, step by step, user-approved (ONLY these, small scope each):
  1. Left sidebar -> TOP nav bar inspired by b.mp4: hive mark+wordmark left, tabs CENTERED (active = honey outline+text), right = round bell (admins, links to "/") + round avatar (initials; menu with name/role + Sign out). Removed: "1 of 4 nodes online/Live" pill, admin pill, Sign out button, light/dark toggle (dark forced in main.tsx).
  2. Big bold page title (PageHeader). Overview description line removed.
  3. Landing palette in tokens.css (bg #0B0A08 — user tried pure black, then undid it), warm status colours, honey accents, small glows (index.css).
- In progress when stopped: bring landing-page ELEMENTS (hexagons, honeycomb texture, cube-cell mark, comb seam glow, landing buttons/numbers/labels) from ALL website pages into the dashboard without changing boxes/layout. Agent only studied the website; no dashboard edits made. Restart this task.
- Reference frames: `ref/frames-b/b-*.jpg` (Stockify) and `d-*.jpg` (Haulix ops). User likes b's essence (top bar, big title, round icons), not a literal copy.
- Backups: frontend-src-backup-v1 (original), -v3 (before top bar), -v4 (before theme), -v5 (before elements; = current).
- Known broken tests (user said fix LATER): g2_tester "Switch to light theme" toggle test; Python e2e tests in tests/e2e that click Sign out directly or check fabric-status (see agent notes: test_v08_g2_shell.py, test_v08_g2_tester*.py, test_v08_g7_*).
- Fonts: dashboard uses bundled Inter; landing fonts (Bricolage/Geist) need user OK to download+bundle.
- Landing bees (done before dashboard): 8 equal bees, half size, word-level weaving, cursor/scroll behaviour in website/assets/js/bees.js; reviewed OK; not watched live at full speed. Backup website-backup-v5.
- Nothing committed (user: don't commit).

## Where we are
Public name **hive.** (lowercase + dot). Work = the public website `mf-dev/website/` (static HTML/CSS/JS, three.js r128 + Lenis 1.3.27 from CDN). Dashboard redesign comes later.

### Done today
- Logo: user chose honeycomb symbol **M2 "Stack"** (3 isometric cube-cells, top one honey) + existing "hive." wordmark. Files `website/assets/img/hive-mark.svg`, `hive-favicon.svg`; options in `website/assets/logo-options/`.
- Website shows **zero "meshforge"** anywhere (incl. commands/paths/labels, renamed to hive-*). Only the website, NOT mf-dev/docs or code.
- LEAD replied with content + real numbers: `..\mf-chat\lab\chat\20261117-181825/182044/182243-LEAD-to-DEV.md`. It is REFERENCE ONLY (write own copy). Real numbers: 1.97x (2 laptops CPU), 3.21x (4 CPU), 3.19x (3 GPUs text AI, 97% of ideal), 2.10x (real data AI, 2 GPUs), planner within 15%, ~6 ms decision, 0 failed of 432 runs, 3.4x Windows (164 s -> 48 s). Planned features (Remote GPU, Combined GPU, learning planner, stronger sandboxing) only as "coming next". Footer: final-year project, tested on 4 laptops.
- Research of 15 premium sites + tools: `C:\Users\HP\UI MeshForge\ref\RESEARCH.md` (user said: no skills, research only).
- New home page built (full creative freedom from user): hero left headline "Many laptops. One hive." + 3D honeycomb cluster right; sticky scroll story; "Measured, not promised" giant 3.21x + bars + stats row; facts columns; four parts with live mini-UIs; coming-next rows; "Start your hive." closing; giant wordmark footer. Reviewer pass done + fixes applied. User LIKES the look.

### Where we are (updated 2026-11-17 ~22:30)
- Home page feedback round DONE: hero list/HUD/"your laptop" removed; one-story copy; no clip line (stage sinks/fades); huge comb (~2.6x, bleeds off edges, dark gradient behind text); continuous idle motion; escalation calm (stay local) -> livelier (hand off) -> aggressive (split, 12 pieces, tremble) -> calm lock. Reviewer fixes applied.
- Bug fixed: animation froze because whenVisible() turned threshold 0 into 0.15 and the story is ~8 viewports tall. Keep `threshold == null ? 0.15 : threshold`.
- User: comb moves now but "needs a lot of improvement" -> user will list details. ASK for them.
- Mascot: round B6 dropped. Chosen **C1 "Buzz"**: faceted courier-bee in the comb's own material (dark olive-bronze hex prisms, honey seam glow), jets not wings, visor face, equal-size eyes. AI assistant avatar that flies and carries jobs between hex cells.
  - 2D: website/assets/bee-v2/c1*.svg (gen: bee-v2/_src/gen4.py, eye_back=1.0 for C1). Other sheets: index/prism/char.html (options, not used).
  - 3D: website/assets/models/buzz.glb (118 KB) built by website/blender/buzz3d.py (Blender 5.3, `blender -b --factory-startup --python buzz3d.py`); previews buzz-preview-*.png. Nodes: Head/Visor/EyeL/EyeR/Mouth/*_Happy/*_Think/ArmL/ArmR/Job/FlameL/FlameR; carry quat in userData.carry_quat_gltf.
  - Site: home.js `BUZZ_LOOK` interface; 3D model loaded via GLTFLoader r128 (CDN), 2D sprite fallback. Hero: Buzz ferries jobs cell to cell; hand off: carries along thread; split: 9 bees; lock: bees dive into cells. YAW constant 0.5 rad in modelLook. Shots: lab/coord/DEV/shots/buzz3d-*.png.
  - Untested in browser: sprite fallback, reduced motion, no-WebGL. Bees may overlap on shared split threads. Live motion never watched by agents (pane hidden) -- user watches in Chrome.
- Snapshots: website-backup-v4 (pre-round), website-partial-1900 (half-edited state). Nothing committed.

## Next step
0. DASHBOARD: restart "bring website elements into dashboard" (see LATEST); one small step at a time, show user, user reviews.
1. (parked) Landing comb/home improvements.
2. Buzz extras when asked: more expressions (blink, surprised, sleepy), HD assistant avatar.
3. Later: inner pages redesign; dashboard redesign; send LEAD screenshot review (user OK needed).

## User preferences (important)
- Hates: pill/outline labels, many boxes/cards, too much text, copying references literally, unrequested big layout changes (ask before changing the 3D hero layout).
- Loves: 3D honeycomb cluster hero, honey light, slow premium motion, open full-width layouts (Kavana/Arkitek as inspiration only).
- Nothing committed; user said do NOT commit for now.
- Mascot must feel part of the scene (same material as comb), never a pasted sticker.
- Short, point-to-point messages. Agent team: implementer = Opus high, reviewer = Sonnet 5.5 medium.

## Key facts
- Palette: #0B0A08, #16140F, #2A251C, honey #F5A623/#FFC93C, warm white #F2EDE4, muted #A8A091. Desktop 1280–1920.
- Preview: `.claude/launch.json` "site" serves `mf-dev/website` on **8766** (preview tool may claim 8767 — use http://localhost:8766). Browser pane often counts as hidden → animations slow; jump-scroll screenshots can look empty until reveals finish.
- Backups: website-backup-1812, -v3, -v4 in mf-dev/ (move out before deploy, with website/assets/bee-preview, logo-options, blender).
- Download links are placeholders (`HIVE.downloads` in assets/js/site.js).
- Messaging LEAD: write file in `..\mf-chat\lab\chat\` and push branch lab/chat (needs user OK each time).

## WSL test controller (still set up)
- https 172.18.70.21:47110; Chrome http://127.0.0.1:47111. Keepalive: `wsl -d Ubuntu -u root -- sleep 7200`. Creds: `lab/coord/DEV/wsl-controller-test-creds.txt` (never commit).

## Traps
- Git push account is tony19053000 (gh). Chat worktree `..\mf-chat` (branch lab/chat).
- auto-mode blocks `rm` and `pip install`; move files instead of deleting.

## Cold-start prompt
Use "START HERE ON THE NEW LAPTOP" at the top of this file. The old prompt below is outdated.

```
You are the MeshForge DEV workstation agent (chat name DEV). Work in "C:\Users\HP\UI MeshForge" (mf-dev = branch dev/ui). Never push to main or lab/*; never touch L1-L4. Read mf-dev/lab/coord/DEV/HANDOFF.md first, then mf-dev/lab/coord/DEV/STATUS.md, "C:\Users\HP\UI MeshForge\ref\NOTES.md", "C:\Users\HP\UI MeshForge\ref\RESEARCH.md". Read the NEWEST section of HANDOFF.md first: start the WSL controller (keepalive) and the dashboard dev server (http://localhost:5173), sign in (local test creds), open the demo with ?demo=1, ask the user the open question (shrink dashboard ~10–12%?), then continue with the next page (Benchmarks/Planner/Settings) in small, user-reviewed steps. The user reviews; no reviewer agent. Do not commit. Short, point-to-point messages. Implementer agent = Opus high.
```
