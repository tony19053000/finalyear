# STATUS

> Concise current state. Long-term plan: [`DEVELOPMENT_ROADMAP.md`](DEVELOPMENT_ROADMAP.md). Update at the end of every work session.

**Last updated:** 2026-09-15

## Current Version
V0.2 — Lab setup & feasibility validation · **in progress** (started 2026-09-15)
V0.1 — code-complete; closes when the 🧑 first GitHub CI run is green (0.1.4, 0.1.10)

## Current Task
0.2.5 S1 network spike + shared spike harness `[~]` (implementer). 0.2.2 inventory code done (commit `feat(0.2.2)`), waiting for 🧑 runs on all 4 laptops. V0.1: 0.1.4 and 0.1.10 wait only for the 🧑 first real GitHub CI run.

## Recently Completed
- 0.2.2 inventory script: tester PASS (round 2), reviewer APPROVE, committed; `[~]` until real 4-laptop runs
- All V0.1 tasks implemented, tested and reviewed; committed locally (one commit per task + fix commits). `[x]`: 0.1.1, 0.1.2, 0.1.3, 0.1.5, 0.1.6, 0.1.7, 0.1.8, 0.1.9, 0.1.11, 0.1.12
- `[~]` pending real GitHub run: 0.1.4 (import boundaries enforced in CI), 0.1.10 (CI green incl. Windows)
- Team accepted ARCHITECTURE v1 and ADRs 0001–0010 (2026-09-15)

## In Progress
- 0.2.5 S1 network spike + `lab/spikes/` harness (implementer → tester → reviewer)
- 🧑 0.2.2 inventory runs on all 4 laptops (commands in lab/README.md; Windows: also confirm no leftover processes after `--timeout 5`)
- 🧑 GitHub push + CI proof (V0.1 criteria 1–2), steps below

## Blockers
- 🧑 Private GitHub repo + first push not done → V0.1 criteria 1 (Windows) and 2 (CI green, bad commit rejected) cannot be proven until then
- Laptop 1 NVIDIA driver blocked by Secure Boot (MOK not enrolled) — user must run `sudo mokutil --import /var/lib/shim-signed/mok/MOK.der`, reboot, enroll
- Hardware of laptops 2–4 unknown; AI-agent access to Windows laptops undecided; Ethernet switch not purchased

## Known Bugs
- None open. DEFECT-0.1.8-01 (test Postgres container leaked on Ctrl-C) fixed: layered cleanup (atexit + finally, SIGINT-deferring shield, labelled containers, namespace-safe stale sweep, Ryuk).
- Watch: `test_sigkill_during_pg_settings_leaks_no_container` failed once (1 of 8 runs, right after a heavy run) — likely the 20 s Ryuk reap window under load; not reproduced in 7 further runs. If it recurs in CI, widen the deadline or rely on the next-run sweep assertion.
- Fixed this session: secret leak + O(n²) in log scrubber; duplicate IDs after fork; plain-`uv sync` test collection crash; CRLF-dependent codegen output.

## Technical Debt
- Leftover `nvidia-driver-590-open` metapackage alongside 595 on Laptop 1 (clean up after driver fix)
- Dev DB has a default password (`meshforge-dev`, DB bound to 127.0.0.1); product/non-dev must refuse to start without an explicit secret (V0.7/V0.17)
- `pytest.mark.unit` etc. not registered until 0.1.8 (warnings); ruff formats Python blocks inside docs/*.md until 0.1.9 excludes docs
- `.venv` on Laptop 1 has all extras incl. torch installed (reviewer ran `uv sync --all-extras`)

## Test Status
- Linux: `uv run pytest -q` → 646 passed, 3 skipped (2 slow opt-in, 1 AttemptState TODO), 2 xfailed (documented redaction scope gaps); ruff, strict mypy (71 files), lint-imports, gen_protos --check all clean; fresh clone + plain `uv sync` → `pytest -m unit` 0 failed
- Windows: not yet run (first real CI run will cover unit + fresh-clone on windows-latest)

## Next Recommended Task
Finish V0.1 commits (0.1.7–0.1.11), then 🧑 **GitHub push + CI proof** (only humans push):
1. Create a **private** GitHub repo (no README/license/.gitignore).
2. In the project folder: `git remote add origin git@github.com:<owner>/<repo>.git` then `git push -u origin main`.
3. Actions tab → run "CI" on `main`: expect lint, types, unit (ubuntu + windows), component, fresh-clone (ubuntu + windows), ci-ok all green. Paste back the run URL (and the Windows logs if red).
4. Settings → Branches/Rulesets for `main`: require PR + status check `ci-ok`.
5. Criterion 2 proof — three branches, one PR each, commit with `--no-verify`: `ci-bad-lint` (add `import os` to `src/meshforge/common/__init__.py`), `ci-bad-type` (add `def _bad() -> int: return "x"`), `ci-bad-boundary` (add `import meshforge.agent  # noqa: F401` to `src/meshforge/controller/__init__.py`). Expect red + merge blocked. Paste back the 3 run URLs, then close PRs and delete branches.
After that the Lead marks 0.1.4 and 0.1.10 `[x]` and closes V0.1; then start **V0.2** (lab setup & spikes) in a new session.
