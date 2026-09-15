# STATUS

> Concise current state. Long-term plan: [`DEVELOPMENT_ROADMAP.md`](DEVELOPMENT_ROADMAP.md). Update at the end of every work session.

**Last updated:** 2026-09-15

## Current Version
V0.1 — Foundations & engineering system · **approved, ready to start** (roadmap v1 approved by team 2026-09-15)

## Current Task
Start V0.1 with task 0.1.1 in a fresh session.

## Recently Completed
- Project brief consolidated (`docs/PROJECT_BRIEF.md`), agent guide (`CLAUDE.md`)
- Four execution modes agreed (1 Job Offloading · 2 Remote GPU · 3 Task Splitting · 4 Combined GPU)
- Architecture v1, roadmap v1, 10 ADRs drafted; **roadmap approved** by team
- Subagents created in `.claude/agents/`: implementer (Opus 5, medium), tester (Sonnet 5, medium), reviewer (Opus 5, low — consider `high` for V0.6, V0.11–V0.13)
- Session protocol: one session per version (CLAUDE.md §4c)
- Laptop 1 inventoried: i7-12650H · 16 GB · RTX 3050 Mobile (~4 GB) · Ubuntu 24.04 · Docker 29.7 · Python 3.12

## In Progress
- None. Environment check done on Laptop 1: uv 0.10.12, git 2.43, Docker 29.7 (daemon usable without sudo), Node 22 / npm 10, gh 2.45, Python 3.12.3. Missing: `protoc` (use grpcio-tools instead), `psql` (not needed; Postgres runs in Docker).

## Blockers
- Laptop 1 NVIDIA driver blocked by Secure Boot (MOK not enrolled) — user must run `sudo mokutil --import /var/lib/shim-signed/mok/MOK.der`, reboot, enroll
- Hardware of laptops 2–4 unknown
- AI-agent access to Windows laptops undecided
- Ethernet switch not purchased; Git/GitHub repo not created

## Known Bugs
- None (no code yet)

## Technical Debt
- None yet
- Leftover `nvidia-driver-590-open` metapackage alongside 595 on Laptop 1 (clean up after driver fix)

## Test Status
- No tests yet

## Next Recommended Task
**0.1.1** repository init (license choice still open — ask team; default: no license file until decided); in parallel 🧑 **0.2.1** driver fix and **0.2.2** hardware inventory.
