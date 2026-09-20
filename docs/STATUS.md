# STATUS

> Concise current state. Long-term plan: [`DEVELOPMENT_ROADMAP.md`](DEVELOPMENT_ROADMAP.md). Update at the end of every work session.

**Last updated:** 2026-09-20 (**scope changed — ADR-0012**; S6 + 0.2.13 closed, all V0.2 agent-side work done)

## Current Version
V0.2 — Lab setup & feasibility validation · **agent-side complete**, waiting on 🧑 lab runs
V0.1 — code-complete; closes when the 🧑 first GitHub CI run is green (0.1.4, 0.1.10)

> **Scope changed 2026-09-20 — [ADR-0012](adr/0012-scope-ship-mode3-complete.md) (Proposed).** Budget is ~10 weeks
> ≈ 90 sessions. **Ship Mode 3 complete and production-grade** (Mode 1 is its foundation), AI planner + telemetry +
> security + dashboard **in full**; **Mode 2 (V0.11) and Mode 4 (V0.12/V0.13/V0.16) deferred**. V0.9 moves before
> V0.10. 129 tasks vs 140–180 capacity → **~1.5 tasks/session**. Needs the other three team members' approval.

## Current Task
**All V0.2 agent-side work is done.** S1 (0.2.5), S2 (0.2.6), S3 (0.2.7), **S6 (0.2.10)** and S7 (0.2.11) are committed with tester PASS + reviewer APPROVE; all five wait on 🧑 runs. **0.2.13 is `[x]`** (report CLIs merge instead of overwriting prose). The critical path is now entirely human: **`lab/inventory.py` on laptops 2–4 blocks V0.3.**

## Recently Completed
- 2026-09-20: **S3-review follow-up done** — `spikes._harness.protocol` (framing + LAN address rules) replaces cross-spike imports of `_s1.protocol`; boundary test added. Lead-direct mechanical move; unit 1360, component suite re-run (see Test Status).
- 2026-09-20: **0.2.13 `[x]`** — `write_report()` in `_harness/report.py`: an existing `--out` is merged (human regions kept, generated regions refreshed), malformed / other-spike files refused with `ReportMergeError` and left byte-identical, `--force` regenerates; all five CLIs write atomically. Tester found 3 defects (glued human table dropped, lone surrogate traceback, UTF-8 BOM refused) → fixed `2fd3185`; reviewer APPROVE. Gates: ruff + mypy clean, lab unit 1313, component 240 passed / 3 skipped.
- 2026-09-20: **Scope decision ADR-0012** — ship Mode 3 complete, defer Mode 2 + Mode 4, keep the AI in full. Roadmap, `PROJECT_SUMMARY.md`, `docs/LAB.md` and the handoff updated. `CLAUDE.md` and `PROJECT_BRIEF.md` deliberately **not** amended until the team approves.
- 2026-09-20: **0.2.10 S6 environments spike** — tester PASS (no defects, 16 adversarial tests), reviewer APPROVE (no blocking findings); commits `f7b094c` `f3364b8` `bfce2fa` `fb9376a`. Real Laptop 1 run: **GPU is visible inside a container** (nvidia-smi 361 ms), cold pull 9.7 s, venv cold 3.56 s vs warm 0.10 s. `docker-gpu-torch` skipped as typed `insufficient_disk` → `torch-cuda-in-container` **TBD**, Q4 unanswered. `[~]` until the 🧑 Windows runs.
- 2026-09-20: **`n = 0` report headline fixed** in the shared `_harness/report.py` — one skipped case no longer makes a healthy table read `n = 0`. Affects every spike's report. New task **0.2.13** raised for the report-CLI overwrite hazard.
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
- **Flake (1 of 2 full runs, 2026-09-20):** `tests/component/lab/test_s1_network_adversarial.py::test_udp_flood_of_garbage_is_dropped_not_amplified` failed once under full-suite load, passed 3/3 in isolation. It asserts `udp_dropped >= before + 500`, i.e. that the kernel delivers all 540 burst datagrams to the server's socket — under load the loopback UDP receive buffer can drop some before the server ever counts them. Unrelated to the protocol move (UDP path untouched; `MAGIC`/`PING` stay in `_s1`). Fix: count `sent - kernel_dropped` or assert a lower bound (e.g. ≥ 90 %) + "nothing echoed"; the "never amplified" half is the real invariant.
- Watch: `test_sigkill_during_pg_settings_leaks_no_container` failed once (1 of 8 runs, right after a heavy run) — likely the 20 s Ryuk reap window under load; not reproduced in 7 further runs. If it recurs in CI, widen the deadline or rely on the next-run sweep assertion.
- Fixed this session: secret leak + O(n²) in log scrubber; duplicate IDs after fork; plain-`uv sync` test collection crash; CRLF-dependent codegen output.

## Technical Debt
- Leftover `nvidia-driver-590-open` metapackage alongside 595 on Laptop 1 (clean up after driver fix)
- Dev DB has a default password (`meshforge-dev`, DB bound to 127.0.0.1); product/non-dev must refuse to start without an explicit secret (V0.7/V0.17)
- `pytest.mark.unit` etc. not registered until 0.1.8 (warnings); ruff formats Python blocks inside docs/*.md until 0.1.9 excludes docs
- `.venv` on Laptop 1 has all extras incl. torch installed (reviewer ran `uv sync --all-extras`)

## Test Status
- Lab suites (2026-09-20, after 0.2.13): `ruff check` / `ruff format --check` clean; `mypy lab tests/unit/lab tests/component/lab` clean (132 files); `pytest tests/unit/lab` **1360 passed**; `pytest tests/component/lab` **240 passed, 3 skipped** (5 min 51 s, on 0.2.13); after the protocol move 239 passed / 1 flake (see Known Bugs) / 3 skipped
- Linux: `uv run pytest -q` → 646 passed, 3 skipped (2 slow opt-in, 1 AttemptState TODO), 2 xfailed (documented redaction scope gaps); ruff, strict mypy (71 files), lint-imports, gen_protos --check all clean; fresh clone + plain `uv sync` → `pytest -m unit` 0 failed
- Windows: not yet run (first real CI run will cover unit + fresh-clone on windows-latest)

## Next Recommended Task
**🔴 🧑 `uv run python lab/inventory.py` on laptops 2, 3 and 4 — this blocks V0.3**, confirms the hardware ADR-0012 rests on (team-stated, unmeasured: L2 ≈ RTX 3050, L3 ≈ RTX 2050, **L4 no GPU**), fills `docs/LAB.md` §1 and settles ADR-0011.
**🧑 Team approval of ADR-0012** — it changes an item `PROJECT_BRIEF` §33 calls relatively fixed.
Agent-side, **nothing is left in V0.2**: the S3-review follow-up (protocol helpers promoted into `_harness/protocol.py`) is done. Small optional follow-ups from the 0.2.10/0.2.13 reviews are in the roadmap design change log. **0.2.12 must wait for real lab data** — never run it on loopback numbers.

**🧑 Lab runs are now the critical path.** Agent-side spike work in V0.2 is finished; what remains needs real machines: (1) S2 two-laptop runs (`lab/spikes/S2.md` §3), (2) S3 two-laptop runs (`lab/spikes/S3.md` §2), (3) `lab/inventory.py` on all 4 laptops, (4) the ADR-0011 decision + torch install (unblocks S4 0.2.8 / S5 0.2.9), (5) the GitHub push + CI proof below. Agent-side while waiting: nothing required; start 0.2.12 once any lab data lands, then V0.3 once the inventory of laptops 2–4 is in.

In parallel, 🧑 **GitHub push + CI proof** (only humans push):
1. Create a **private** GitHub repo (no README/license/.gitignore).
2. In the project folder: `git remote add origin git@github.com:<owner>/<repo>.git` then `git push -u origin main`.
3. Actions tab → run "CI" on `main`: expect lint, types, unit (ubuntu + windows), component, fresh-clone (ubuntu + windows), ci-ok all green. Paste back the run URL (and the Windows logs if red).
4. Settings → Branches/Rulesets for `main`: require PR + status check `ci-ok`.
5. Criterion 2 proof — three branches, one PR each, commit with `--no-verify`: `ci-bad-lint` (add `import os` to `src/meshforge/common/__init__.py`), `ci-bad-type` (add `def _bad() -> int: return "x"`), `ci-bad-boundary` (add `import meshforge.agent  # noqa: F401` to `src/meshforge/controller/__init__.py`). Expect red + merge blocked. Paste back the 3 run URLs, then close PRs and delete branches.
After that the Lead marks 0.1.4 and 0.1.10 `[x]` and closes V0.1; then start **V0.2** (lab setup & spikes) in a new session.
