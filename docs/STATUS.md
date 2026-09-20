# STATUS

> Concise current state. Long-term plan: [`DEVELOPMENT_ROADMAP.md`](DEVELOPMENT_ROADMAP.md). Update at the end of every work session.

**Last updated:** 2026-09-20 (S2 closed)

## Current Version
V0.2 — Lab setup & feasibility validation · **in progress** (started 2026-09-15)
V0.1 — code-complete; closes when the 🧑 first GitHub CI run is green (0.1.4, 0.1.10)

## Current Task
**All V0.2 spike code is done.** S1 (0.2.5), S2 (0.2.6), S3 (0.2.7) and S7 (0.2.11) are committed with reviewer APPROVE; all four wait for 🧑 runs on the laptops. S4/S5 wait for the 🧑 ADR-0011 decision. V0.1: 0.1.4 and 0.1.10 wait only for the 🧑 first real GitHub CI run (remote `origin` added 2026-09-20, push pending).

## Recently Completed
- 2026-09-20: **0.2.6 S2 cross-OS spike** — tester rounds 1–2 (6 + 1 defects, all fixed), reviewer REQUEST_CHANGES ×4 then APPROVE on pass 5; commit `633d474`. 1033 lab unit tests, 42 S2 component tests; whole lab component suite 226 passed. Real loopback runs in `S2.md` §5 (the tool marks them `⚠ same machine`). `[~]` until the 🧑 two-laptop runs.
- 2026-09-20: **0.2.7 S3 transport spike** — tester PASS (24 adversarial tests), reviewer APPROVE on the second pass after 3 blocking fixes; commit `81d620f`. 245 unit + 62 component S3 tests; real loopback run verified end to end. `[~]` until the 🧑 two-laptop runs.
- 2026-09-20: 0.2.5 S1 spike + `_harness/` — reviewer APPROVE after fix round 3, commit `ef065ac`; 0.2.11 S7 spike — tester PASS, reviewer APPROVE, commit `8072b9f`; GitHub remote added
- 0.2.1 (Laptop 1 part): NVIDIA driver fixed by the user — `nvidia-smi` works (RTX 3050 Laptop, 595.91.07, 4 GiB, CC 8.6); `torch.cuda` check still pending ADR-0011/0.2.3
- 0.2.5 S1 spike: tester PASS round 2; small fix round in progress (host-dependent tests, flaky flood margin, case-insensitive filename collision)
- 0.2.2 inventory script: tester PASS (round 2), reviewer APPROVE, committed; `[~]` until real 4-laptop runs
- All V0.1 tasks implemented, tested and reviewed; committed locally (one commit per task + fix commits). `[x]`: 0.1.1, 0.1.2, 0.1.3, 0.1.5, 0.1.6, 0.1.7, 0.1.8, 0.1.9, 0.1.11, 0.1.12
- `[~]` pending real GitHub run: 0.1.4 (import boundaries enforced in CI), 0.1.10 (CI green incl. Windows)
- Team accepted ARCHITECTURE v1 and ADRs 0001–0010 (2026-09-15)

## In Progress
- 🧑 0.2.6 S2 two-laptop runs, Windows↔Linux both directions, Wi-Fi **and** Ethernet (procedure in `lab/spikes/S2.md` §3; server-side firewall TCP 47202–47203 only; includes the Windows second-`serve` check)
- 🧑 0.2.7 S3 two-laptop runs, Wi-Fi **and** Ethernet (procedure in `lab/spikes/S3.md` §2; firewall TCP 47301–47303)
- 🧑 0.2.2 inventory runs on all 4 laptops (commands in lab/README.md; Windows: also confirm no leftover processes after `--timeout 5`)
- 🧑 GitHub push + CI proof (V0.1 criteria 1–2), steps below

## Blockers
- 🧑 Private GitHub repo + first push not done → V0.1 criteria 1 (Windows) and 2 (CI green, bad commit rejected) cannot be proven until then
- Hardware of laptops 2–4 unknown; AI-agent access to Windows laptops undecided; Ethernet switch not purchased

## Known Bugs
- No bugs open in the lab suites (S2's two red tests from 2026-09-19 were fixed in the S2 rounds). DEFECT-0.1.8-01 (test Postgres container leaked on Ctrl-C) fixed: layered cleanup (atexit + finally, SIGINT-deferring shield, labelled containers, namespace-safe stale sweep, Ryuk).
- Watch: `test_sigkill_during_pg_settings_leaks_no_container` failed once (1 of 8 runs, right after a heavy run) — likely the 20 s Ryuk reap window under load; not reproduced in 7 further runs. If it recurs in CI, widen the deadline or rely on the next-run sweep assertion.
- Fixed this session: secret leak + O(n²) in log scrubber; duplicate IDs after fork; plain-`uv sync` test collection crash; CRLF-dependent codegen output.

## Technical Debt
- Leftover `nvidia-driver-590-open` metapackage alongside 595 on Laptop 1 (clean up after driver fix)
- Dev DB has a default password (`meshforge-dev`, DB bound to 127.0.0.1); product/non-dev must refuse to start without an explicit secret (V0.7/V0.17)
- `pytest.mark.unit` etc. not registered until 0.1.8 (warnings); ruff formats Python blocks inside docs/*.md until 0.1.9 excludes docs
- `.venv` on Laptop 1 has all extras incl. torch installed (reviewer ran `uv sync --all-extras`)

## Test Status
- Lab suites (2026-09-20, after S2): `ruff check` / `ruff format --check` clean; `mypy lab tests/unit/lab tests/component/lab` clean (111 files); `pytest tests/unit/lab` 1033 passed; `pytest tests/component/lab` 226 passed, 3 skipped (~5 min, no flake across the S2 rounds)
- Linux: `uv run pytest -q` → 646 passed, 3 skipped (2 slow opt-in, 1 AttemptState TODO), 2 xfailed (documented redaction scope gaps); ruff, strict mypy (71 files), lint-imports, gen_protos --check all clean; fresh clone + plain `uv sync` → `pytest -m unit` 0 failed
- Windows: not yet run (first real CI run will cover unit + fresh-clone on windows-latest)

## Next Recommended Task
**🧑 Lab runs are now the critical path.** Agent-side spike work in V0.2 is finished; what remains needs real machines: (1) S2 two-laptop runs (`lab/spikes/S2.md` §3), (2) S3 two-laptop runs (`lab/spikes/S3.md` §2), (3) `lab/inventory.py` on all 4 laptops, (4) the ADR-0011 decision + torch install (unblocks S4 0.2.8 / S5 0.2.9), (5) the GitHub push + CI proof below. Agent-side while waiting: the S3-review follow-up — promote `spikes._s1.protocol` (address rules, framing, `ProtocolError`) into `_harness/` so spikes stop importing each other's internals — or start 0.2.12 once any lab data lands.

In parallel, 🧑 **GitHub push + CI proof** (only humans push):
1. Create a **private** GitHub repo (no README/license/.gitignore).
2. In the project folder: `git remote add origin git@github.com:<owner>/<repo>.git` then `git push -u origin main`.
3. Actions tab → run "CI" on `main`: expect lint, types, unit (ubuntu + windows), component, fresh-clone (ubuntu + windows), ci-ok all green. Paste back the run URL (and the Windows logs if red).
4. Settings → Branches/Rulesets for `main`: require PR + status check `ci-ok`.
5. Criterion 2 proof — three branches, one PR each, commit with `--no-verify`: `ci-bad-lint` (add `import os` to `src/meshforge/common/__init__.py`), `ci-bad-type` (add `def _bad() -> int: return "x"`), `ci-bad-boundary` (add `import meshforge.agent  # noqa: F401` to `src/meshforge/controller/__init__.py`). Expect red + merge blocked. Paste back the 3 run URLs, then close PRs and delete branches.
After that the Lead marks 0.1.4 and 0.1.10 `[x]` and closes V0.1; then start **V0.2** (lab setup & spikes) in a new session.
