# DEV — moving to a new laptop (everything, same as the old one)

Goal: the new laptop opens exactly what the old one had — the **dashboard** (all pages, demo data, login),
the **landing website** (robotic bees, 3D Buzz), the **design references**, and the agent picks up exactly
where we stopped. Everything is on GitHub branch **`dev/ui`** (commit `8f5db33d` + this setup commit).

## 1. Install (human, once)
1. **Git** — https://git-scm.com/download/win
2. **GitHub CLI `gh`** — https://cli.github.com/ ; then in PowerShell: `gh auth login` (account that can push to
   `tony19053000/finalyear`; do this yourself, never give the token to Claude).
3. **Node.js 20 LTS or newer** — https://nodejs.org/ (tick "add to PATH"). Check: `node -v`, `npm -v`.
4. **Python 3.12+** — https://www.python.org/ (for the website / reference preview servers). Check: `python --version`.
5. **Claude desktop app** (Code tab).
6. Optional, only for real sign-in against a controller (demo mode does NOT need it): WSL2 Ubuntu
   (`wsl --install -d Ubuntu`, admin PowerShell, reboot).
7. Optional: Blender 5.3 (only to re-render the 3D bee / PC tower).

## 2. Get the code (exact same folder layout)
```powershell
mkdir "$HOME\UI MeshForge"
cd "$HOME\UI MeshForge"
gh repo clone tony19053000/finalyear mf-dev
cd mf-dev
git checkout dev/ui
git worktree add -f ..\mf-chat origin/lab/chat
cd frontend
npm ci
```
Then copy the Claude preview config so the "dash" / "site" / "ref" servers work in the app:
```powershell
mkdir "$HOME\UI MeshForge\.claude"
copy "$HOME\UI MeshForge\mf-dev\lab\coord\DEV\carry\claude\launch.json" "$HOME\UI MeshForge\.claude\launch.json"
```
Open the folder **`UI MeshForge`** (the parent, not mf-dev) in Claude Code.

## 3. Copy by hand (NOT on GitHub)
- `wsl-controller-test-creds.txt` → `mf-dev/lab/coord/DEV/` (test login; never commit). Only needed with a controller.
- Optional: the reference videos `ref/a.mp4 … f.mp4` (~66 MB; design inspiration only).
- Optional: `website/assets/models/buzz-preview-*.png` (render previews; not used by the site; re-render with
  `website/blender/buzz3d.py`).

## 4. Run and check
| What | How | URL |
|---|---|---|
| Dashboard (dev server) | Claude preview "dash" (or `node mf-dev/lab/coord/DEV/dash-dev.mjs`) | http://localhost:5173 |
| **Dashboard demo** (all demo data: 5 laptops, jobs, alerts, benchmarks, planner, settings) | add `?demo=1` (localhost only) | http://localhost:5173/?demo=1 |
| Product guide | header bell | http://localhost:5173/guide?demo=1 |
| Landing website (robotic bees, 3D Buzz) | preview "site" (`python -m http.server 8766 -d mf-dev/website`) | http://localhost:8766 |
| Design references | preview "ref" | http://localhost:8765 |

Demo mode fakes an admin session, so **every page works without a controller** (actions that change data fail —
expected). Real sign-in needs a controller: dev server proxies to `http://127.0.0.1:47111`
(`MESHFORGE_DEV_CONTROLLER` overrides). Setting up a WSL controller on the new laptop = ask the agent; see HANDOFF "WSL test controller".

Checks (in `mf-dev/frontend`): `npx tsc -b --noEmit`, `npx eslint src`, `npx vitest run`
(known failing: g2_tester theme-toggle — fixing it is task 3b).

## 5. Agent memory (so it "feels like the same laptop")
`mf-dev/lab/coord/DEV/carry/memory/` holds the agent's memory notes (bee mascot choice, logo direction, agent
team models). The cold-start prompt tells the agent to copy them into its own memory folder.

## 6. Cold-start prompt (paste into the new Claude Code session)
```
You are the MeshForge DEV workstation agent (chat name DEV) on a NEW laptop. Work in the folder "UI MeshForge"
(mf-dev = branch dev/ui; ..\mf-chat = lab/chat worktree). Never push to main or lab/*; never touch L1-L4.
First: read mf-dev/lab/coord/DEV/NEW-LAPTOP.md and finish any setup step still missing (check node/npm, npm ci,
.claude/launch.json, mf-chat worktree); copy mf-dev/lab/coord/DEV/carry/memory/*.md into your memory folder.
Then read mf-dev/lab/coord/DEV/HANDOFF.md (NEWEST section first), STATUS.md, carry/ref/NOTES.md, carry/ref/RESEARCH.md,
lab/coord/RULES.md (DEV section, send/read), and the newest LEAD-to-DEV chat in ..\mf-chat\lab\chat.
Start the dashboard ("dash" preview), open http://localhost:5173/?demo=1 and the website ("site") to confirm all is there.
Then continue LEAD's orders in order: 3a remove the demo fake admin session (keep ?demo=1 localhost-only, no auth bypass),
3b fix g2_tester theme-toggle test + review the 6 changed g5_settings tests, 3c npm test / npm run check:api / tsc / eslint
green, 3d DEV note listing every changed page; then SCREENSHOTS.md, website with LEAD's content, video-ready pages.
The user reviews every step (no reviewer agent); implementer agent = Opus high. Small steps, show the user.
Commit/push to dev/ui only when the user says; `send LEAD` progress after each step. Short, point-to-point messages.
```
