# STATUS

> Concise current state. Plan: [`DEVELOPMENT_ROADMAP.md`](DEVELOPMENT_ROADMAP.md). Direction + next step:
> [`SESSION_HANDOFF.md`](SESSION_HANDOFF.md). Older detail (full completed-list, old test runs, bug forensics):
> [`archive/STATUS-history-to-2026-09-28.md`](archive/STATUS-history-to-2026-09-28.md). Kept short on purpose.

**Last updated:** 2026-09-28 (G1 closed; G2 tester next; docs trimmed for token cost)

## Progress

Generated from the roadmap checkboxes by `uv run python scripts/status_bars.py` — **re-run it and replace this block whenever checkboxes change.** Deferred versions (V0.11, V0.12, V0.13, V0.16) and deferred tasks are not counted. █ = `[x]` (tester + reviewer + evidence); ▒ = `[~]`.

```
TOTAL   ███▒▒▒░░░░░░░░░░░░░░  14%  23/154 done, 20 in progress

V0.1    █████████████████▒▒▒  83%  10/12  Foundations & engineering system
V0.2    ██▒▒▒▒▒▒▒▒▒▒▒▒▒░░░░░   9%   1/11  Lab setup & feasibility validation
V0.3    ███████████████▒▒▒▒░  73%  11/15  Fabric core: controller ⇄ agent
V0.4    █▒▒▒▒▒▒▒▒▒░░░░░░░░░░   5%   1/17  Remote execution core (Mode 1 foundation)
V0.5    ░░░░░░░░░░░░░░░░░░░░   0%   0/11  Data plane & environments
V0.6    ░░░░░░░░░░░░░░░░░░░░   0%   0/13  Scheduling & reliability — Mode 1 complete
V0.7    ░░░░░░░░░░░░░░░░░░░░   0%   0/11  Security & pairing
V0.8    ░░░░░░░░░░░░░░░░░░░░   0%   0/10  Dashboard & CLI v1
V0.9    ░░░░░░░░░░░░░░░░░░░░   0%   0/10  Benchmark & telemetry system v1
V0.10   ░░░░░░░░░░░░░░░░░░░░   0%   0/10  Mode 3 Task Splitting
V0.14   ░░░░░░░░░░░░░░░░░░░░   0%   0/8   Unified planner v2
V0.15   ░░░░░░░░░░░░░░░░░░░░   0%   0/10  ML planner
V0.17   ░░░░░░░░░░░░░░░░░░░░   0%   0/8   Productization
V1.0    ░░░░░░░░░░░░░░░░░░░░   0%   0/8   Release hardening & final evaluation
```

## Current Version
**V0.4 — Remote execution core (Mode 1 foundation)** · started 2026-09-28 · brief `docs/briefs/V0.4-remote-execution.md`.
- V0.3 fabric core: closed agent-side (G1–G7); formally closes on the 🧑 0.3.15 lab run.
- V0.2: agent-side complete, waiting on 🧑 lab runs. V0.1: waits on the 🧑 first GitHub CI run (0.1.4, 0.1.10).
- Scope: [ADR-0012](adr/0012-scope-ship-mode3-complete.md) — Mode 3 complete + Mode 1 + full AI planner; **Mode 2/4 deferred** (see handoff §0).

## Current Task
**G2 tester round** (0.4.1–0.4.5 + D44 token). One subagent at a time. Then G2 reviewer → G3 tester → G3 reviewer → G4 → G5.

## Recently Completed
- 2026-09-28: **V0.4 G1 closed** — 0.4.16 `[x]`: explicit agent `Goodbye` replaces timing-based close detection (D40), admission deadline (D41), `manager.py` split into claims/reports/dispatch/… modules, write-queue overflow signal (D43); `grpcio<1.85` cap lifted. Tester PASS `6f545ca`, reviewer APPROVE; follow-ups → D61 (G4).
- 2026-09-28: V0.4 G2 and G3 implementers done (G2 `1462e34`…`f92e4fd`; G3 `d625a45`…`b14baa3`) — awaiting tester/reviewer.
- 2026-09-25: V0.3 G7 closed agent-side — 0.3.4 `[x]` hardware scores.
- Earlier: see the archive.

## In Progress (🧑 human steps)
- 🧑 **G7 Windows check:** on a Windows laptop `git pull`, `uv sync --extra agent`, `uv run meshforge-agent hardware score` (exit 0); same in two terminals at once (second exits **7**); Ctrl-C mid-run (no leftover `python` in Task Manager). Paste outputs + exit codes.
- 🧑 **0.3.13 Windows logoff check** — procedure `docs/OPERATIONS.md` §3. The docs say "unverified" on purpose; never replace that with a guess.
- 🧑 **0.3.2:** `uv run pytest tests/unit/agent -q` on any Windows laptop after `git pull` + `uv sync`; paste the summary line.
- 🧑 **0.3.15** lab run + 24 h soak (`lab/checklists/V0.3-fabric.md`).
- 🧑 S2 (`lab/spikes/S2.md` §3) and S3 (`lab/spikes/S3.md` §2) two-laptop runs, Wi-Fi **and** Ethernet.
- 🧑 `lab/inventory.py` on laptops 2–4.
- 🧑 V0.4 G3: Windows check + pytorch image pull (Laptop 1 needs ~12 GB free, has ~3 GB).

## Blockers
- 🧑 Private GitHub repo + first push not done → V0.1 criteria 1–2 unprovable.
- Hardware of laptops 2–4 unmeasured; Ethernet switch not bought; Docker not yet on laptops 2–4.

## Known Bugs
- Possible **G3 sandbox defect**: run container may have network when the setup container had it (parked test failed once) — G3 tester reproduces first.
- Watch: `test_sigkill_during_pg_settings_leaks_no_container` failed once under load (1 of 8).
- Rule from the fixed exit-139 bug: a green pytest summary is not a green run — read the exit code (`scripts/pytest_exit_guard.py`).

## Technical Debt
- Roadmap 0.4.17 items still open after G1–G3 (see the roadmap row); D59: subprocess runtime on Linux leaves grandchildren of a SIGKILLed agent (fix before V0.6 chaos suite).
- Dev DB default password (`meshforge-dev`, bound to 127.0.0.1) — product must refuse it (V0.7/V0.17).
- Laptop 1 venv: torch 2.14 CUDA 13.0, no numpy — not ADR-0011's cu126 index.

## Test Status
- **Last full run (2026-09-28, G1 tester):** unit 1641 selected / component controller+agent 290 / cluster 41 — all exit 0 (see G1 row in roadmap).
- **Last whole-suite baseline (2026-09-25, after G7):** ruff + format clean; `mypy src` clean; lint-imports 7/0; gen_protos clean; `-m unit` 3725 passed exit 0; `-m component` 462 passed exit 0 (13 min 24 s); `-m cluster` 40 passed exit 0.
- Windows: never run (first CI run will cover it).

## Next Recommended Task
**Agent-side:** G2 tester round (handoff §2). **Human-side:** GitHub push + CI proof:
1. Create a **private** GitHub repo (no README/license/.gitignore).
2. `git remote add origin git@github.com:<owner>/<repo>.git` then `git push -u origin main`.
3. Actions → run "CI" on `main`: expect all jobs green. Paste back the run URL.
4. Settings → protect `main`: require PR + status check `ci-ok`.
5. Criterion 2 proof — three branches with a bad commit each (`--no-verify`): `ci-bad-lint` (add `import os` to `src/meshforge/common/__init__.py`), `ci-bad-type` (add `def _bad() -> int: return "x"`), `ci-bad-boundary` (add `import meshforge.agent  # noqa: F401` to `src/meshforge/controller/__init__.py`). Expect red + merge blocked; paste the 3 run URLs, then close the PRs.
