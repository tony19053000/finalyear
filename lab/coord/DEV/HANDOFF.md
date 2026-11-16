# DEV HANDOFF — read this first (any account)

Updated after every step. Cold-start prompt for a new account:
```
You are the MeshForge DEV workstation agent (chat name DEV). Work only in "UI MeshForge\mf-dev" on branch dev/ui; never push to main or lab/*; never touch L1-L4. Read lab/coord/DEV/HANDOFF.md, lab/coord/DEV/STATUS.md, docs/briefs/UI-redesign.md, then lab/coord/RULES.md ("Lab chat", "DEV workstation"). Continue the next task in STATUS.md. Short simple messages.
```

## How LEAD reviews
`git fetch origin dev/ui` · `git log --oneline origin/main..origin/dev/ui` · `git diff origin/main...origin/dev/ui`.
Files: this folder (HANDOFF, STATUS, NOTES), `docs/briefs/UI-redesign.md`, `docs/briefs/progress/UI-G*.md`, `frontend/`.

## Reporting to LEAD
After every finished step: `send LEAD` a "UI progress update N" (done, commit, what is next, what LEAD should reply).

## In flight
- 2026-11-17: MeshForge-Agent-Setup-0.17.5-1.exe downloaded to "UI MeshForgedownloads" (checksum OK) and launched; installer NOT finished. Task: screenshot every install + agent-console page into "UI MeshForgescreenshots## In flight1-windows-install" (named 01-..., 02-...). Do NOT join the fabric (no join code). Needs computer use (now enabled in app Settings > Capabilities; requires a new session).
- Background capture script: "UI MeshForgescreenshotspture.ps1" (frames raw-NNN.png, 2 s, 15 min; earlier frames are mostly Settings screens - drop them).
- Brief DRAFT written; waiting for team discussion. Nothing built.

## Traps
- Git push account on DEV is tony19053000 (gh). Commit identity tony19053000.
- Chat worktree: `..\mf-chat` (branch lab/chat).
