# STATUS

> Concise current state. Long-term plan: [`DEVELOPMENT_ROADMAP.md`](DEVELOPMENT_ROADMAP.md). Update at the end of every work session.

**Last updated:** 2026-09-21 (**V0.3 G1–G3 closed agent-side** — a live controller ⇄ agent fabric works in-process; V0.2 waits on 🧑 lab runs)

## Current Version
**V0.3 — Fabric core** · started 2026-09-20 on the team's decision (before the laptop 2–4 inventory); **G1–G3 closed agent-side** (0.3.1, 0.3.6, 0.3.8, 0.3.9, 0.3.10 `[x]`; 0.3.2 `[~]` Windows run; 0.3.7 `[~]` lab evidence)
V0.2 — Lab setup & feasibility validation · agent-side complete, waiting on 🧑 lab runs
V0.1 — code-complete; closes when the 🧑 first GitHub CI run is green (0.1.4, 0.1.10)

> **Scope changed 2026-09-20 — [ADR-0012](adr/0012-scope-ship-mode3-complete.md) (Proposed).** Budget is ~10 weeks
> ≈ 90 sessions. **Ship Mode 3 complete and production-grade** (Mode 1 is its foundation), AI planner + telemetry +
> security + dashboard **in full**; **Mode 2 (V0.11) and Mode 4 (V0.12/V0.13/V0.16) deferred**. V0.9 moves before
> V0.10. 129 tasks vs 140–180 capacity → **~1.5 tasks/session**. Needs the other three team members' approval.

## Current Task
**Next: V0.3 group G4** — 0.3.3 hardware discovery (lift `lab/_inventory/probes.py` logic into `agent/hardware/`, NVML + nvidia-smi, fill `Hello.hardware`), 0.3.5 resource monitor (psutil + NVML every 2 s, change-threshold reporting via `send_report("resource_update", …)`), 0.3.11 snapshots (controller: live ring per node + 1-min downsample into a `resource_snapshot` table, retention setting; cap `hardware` JSON size — G2 follow-up). The Lead writes the brief as §5d of `docs/briefs/V0.3-fabric-core.md` using G3's real API (`AgentSessionClient.send_report`, `REPORT_PAYLOADS`, `ResourceUpdate.sequence` keyed replay).

**First real two-machine smoke is now possible** (no lab task yet — optional): controller on Laptop 1 (`meshforge-controller run` with Postgres via compose), `meshforge-agent run` on another laptop with `MESHFORGE_AGENT__CONTROLLER_ADDRESS=<L1-ip>:47100`; expect the node ONLINE in `/healthz` sessions count and the `node` table. Firewall: TCP 47100 inbound on Laptop 1 only.

## Recently Completed
- 2026-09-21: **V0.3 G3 closed agent-side** — 0.3.8 `[x]`, 0.3.9 `[x]`. The agent runtime exists: `meshforge-agent run` connects, heartbeats, answers pings, reconnects with full-jitter backoff (controller restart → ONLINE again in ~1–2 s in-process), replays buffered reports after `Reconcile`, finds the controller by config or mDNS, exits CONFLICT when superseded. Tester: 1 defect (heartbeat interval unbounded → clamped) ; reviewer pass 1 → 1 blocking (`replay_from_sequence` contract) → APPROVE pass 2. Commits `a934621` … `0f55af0`. Unit 2437, component agent 37 (~78 s), store+controller+agent 90.
- 2026-09-20: **V0.3 G2 closed agent-side** — 0.3.6 `[x]`, 0.3.10 `[x]`, 0.3.7 wired. Implementer (2 runs) → tester (2 defects) → reviewer REQUEST_CHANGES (3 blocking) → fixes → **APPROVE pass 2**. Key design outcomes: liveness decided in memory with a per-session ordered write chain (DB never blocks the reader/timer); epoch fenced against the persisted high-water mark; `session_epoch` as NUMERIC(20,0) full uint64; stream end reasons surfaced as `ABORTED` details. Commits `bd416eb` … `a1014d5`. Unit 2259, component store+controller 67, controller component 10/10.
- 2026-09-20: **V0.3 G1 closed agent-side** — 0.3.1 proto `[x]`; 0.3.2 identity (ADR-0013) and 0.3.7 liveness logic code-complete. Implementer → tester (2 critical concurrency defects + 1 minor, fixed) → reviewer (1 blocking: `next_epoch` clobbering a reset identity, fixed; APPROVE pass 2). Commits `da27ff1` `b7eda28` `7b7e7ff` `0acf7eb`. `pytest -m unit` 2122 passed. ARCHITECTURE §12.3 amended (DRAINING lease arrows, revoke from every live state).
- 2026-09-20: **V0.3 prepared** — `docs/briefs/V0.3-fabric-core.md` (groups G1–G7, G1 brief), **ADR-0013** node identity (P-256 key + ULID `node_id`, OS-protected) and **ADR-0002 Updates** (session epoch fencing, `session_id` envelopes, full-jitter backoff cap 15 s, bounded replay, monotonic leases) — all Proposed, need team review.
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
- 🧑 0.3.2 Windows verification: `uv run pytest tests/unit/agent -q` on any Windows laptop after `git pull` + `uv sync` (exercises `icacls` and `msvcrt.locking`); paste the summary line
- 🧑 0.2.6 S2 two-laptop runs, Windows↔Linux both directions, Wi-Fi **and** Ethernet (procedure in `lab/spikes/S2.md` §3; server-side firewall TCP 47202–47203 only; includes the Windows second-`serve` check)
- 🧑 0.2.7 S3 two-laptop runs, Wi-Fi **and** Ethernet (procedure in `lab/spikes/S3.md` §2; firewall TCP 47301–47303)
- 🧑 0.2.2 inventory runs on all 4 laptops (commands in lab/README.md; Windows: also confirm no leftover processes after `--timeout 5`)
- 🧑 GitHub push + CI proof (V0.1 criteria 1–2), steps below

## Blockers
- 🧑 Private GitHub repo + first push not done → V0.1 criteria 1 (Windows) and 2 (CI green, bad commit rejected) cannot be proven until then
- Hardware of laptops 2–4 unknown; AI-agent access to Windows laptops undecided; Ethernet switch not purchased

## Known Bugs
- No bugs open in the lab suites (S2's two red tests from 2026-09-19 were fixed in the S2 rounds). DEFECT-0.1.8-01 (test Postgres container leaked on Ctrl-C) fixed: layered cleanup (atexit + finally, SIGINT-deferring shield, labelled containers, namespace-safe stale sweep, Ryuk).
- Fixed 2026-09-20: `test_udp_flood_of_garbage_is_dropped_not_amplified` flaked once under full-suite load — a single 540-datagram burst (~1 KiB skb `truesize` each) can overflow the server's 212 KiB default UDP receive buffer while its thread is descheduled, so the kernel dropped datagrams before the server counted them. The test now sends paced bursts of ≤ 50, each acknowledged by the `udp_dropped` counter, and still asserts the exact count and that nothing is echoed.
- Watch: `test_sigkill_during_pg_settings_leaks_no_container` failed once (1 of 8 runs, right after a heavy run) — likely the 20 s Ryuk reap window under load; not reproduced in 7 further runs. If it recurs in CI, widen the deadline or rely on the next-run sweep assertion.
- Fixed this session: secret leak + O(n²) in log scrubber; duplicate IDs after fork; plain-`uv sync` test collection crash; CRLF-dependent codegen output.

## Technical Debt
- Leftover `nvidia-driver-590-open` metapackage alongside 595 on Laptop 1 (clean up after driver fix)
- Dev DB has a default password (`meshforge-dev`, DB bound to 127.0.0.1); product/non-dev must refuse to start without an explicit secret (V0.7/V0.17)
- `pytest.mark.unit` etc. not registered until 0.1.8 (warnings); ruff formats Python blocks inside docs/*.md until 0.1.9 excludes docs
- `.venv` on Laptop 1 has all extras incl. torch installed (`uv sync --all-extras`, re-done in G1) — **torch 2.14 with CUDA 13.0 and no numpy**; the S7 GPU cases run for real here now. Note for ADR-0011: this is not the cu126 index it proposes; check what `uv` resolved before deciding

## Test Status
- Lab suites (2026-09-20, after 0.2.13): `ruff check` / `ruff format --check` clean; `mypy lab tests/unit/lab tests/component/lab` clean (132 files); `pytest tests/unit/lab` **1360 passed**; `pytest tests/component/lab` **240 passed, 3 skipped** (5 min 51 s, on 0.2.13); after the protocol move + flood-test fix **240 passed, 3 skipped** again (5 min 50 s)
- Core (2026-09-20, after G1): `ruff` clean; `mypy src` clean (34 files); `lint-imports` 7 kept / 0 broken; `gen_protos.py --check` clean; `pytest -m unit` **2122 passed**, 2 skipped, 2 xfailed (31 s); component suite (`pytest -m component`, 9 min 07 s): 279 passed, 6 skipped, 2 failed → (1) S7 `gpu-bfloat16` setup info lacked the `backend` key — a **real latent S7 defect** that only shows with a real CUDA torch, which the G1 `uv sync --all-extras` installed on Laptop 1 (torch 2.14, CUDA 13.0); fixed in `_s7/child.py` `_device_info`, S7 component file 17 passed with the GPU cases running for real; (2) the known `test_sigkill_during_pg_settings_leaks_no_container` load flake (passed on rerun — see Known Bugs)
- Linux (V0.1 baseline): `uv run pytest -q` → 646 passed, 3 skipped (2 slow opt-in, 1 AttemptState TODO), 2 xfailed (documented redaction scope gaps); ruff, strict mypy (71 files), lint-imports, gen_protos --check all clean; fresh clone + plain `uv sync` → `pytest -m unit` 0 failed
- Windows: not yet run (first real CI run will cover unit + fresh-clone on windows-latest)

## Next Recommended Task
**🔴 🧑 `uv run python lab/inventory.py` on laptops 2, 3 and 4 — this blocks V0.3**, confirms the hardware ADR-0012 rests on (team-stated, unmeasured: L2 ≈ RTX 3050, L3 ≈ RTX 2050, **L4 no GPU**), fills `docs/LAB.md` §1 and settles ADR-0011.
**🧑 Team approval of ADR-0012** — it changes an item `PROJECT_BRIEF` §33 calls relatively fixed.
Agent-side, **nothing is left in V0.2**: the S3-review follow-up (protocol helpers promoted into `_harness/protocol.py`) is done. Small optional follow-ups from the 0.2.10/0.2.13 reviews are in the roadmap design change log. **0.2.12 must wait for real lab data** — never run it on loopback numbers.

**🧑 Lab runs are now the critical path.** Agent-side spike work in V0.2 is finished; what remains needs real machines: (1) S2 two-laptop runs (`lab/spikes/S2.md` §3), (2) S3 two-laptop runs (`lab/spikes/S3.md` §2), (3) `lab/inventory.py` on all 4 laptops, (4) the ADR-0011 decision + torch install (unblocks S4 0.2.8 / S5 0.2.9), (5) the GitHub push + CI proof below. Agent-side while waiting: nothing required. **V0.3 is planned** in `docs/briefs/V0.3-fabric-core.md` (task groups G1–G7, decisions D1–D8, G1 brief ready); ADR-0013 + ADR-0002 Updates written (Proposed); when the inventory of laptops 2–4 is in, spawn G1. 0.2.12 once any lab data lands.

In parallel, 🧑 **GitHub push + CI proof** (only humans push):
1. Create a **private** GitHub repo (no README/license/.gitignore).
2. In the project folder: `git remote add origin git@github.com:<owner>/<repo>.git` then `git push -u origin main`.
3. Actions tab → run "CI" on `main`: expect lint, types, unit (ubuntu + windows), component, fresh-clone (ubuntu + windows), ci-ok all green. Paste back the run URL (and the Windows logs if red).
4. Settings → Branches/Rulesets for `main`: require PR + status check `ci-ok`.
5. Criterion 2 proof — three branches, one PR each, commit with `--no-verify`: `ci-bad-lint` (add `import os` to `src/meshforge/common/__init__.py`), `ci-bad-type` (add `def _bad() -> int: return "x"`), `ci-bad-boundary` (add `import meshforge.agent  # noqa: F401` to `src/meshforge/controller/__init__.py`). Expect red + merge blocked. Paste back the 3 run URLs, then close PRs and delete branches.
After that the Lead marks 0.1.4 and 0.1.10 `[x]` and closes V0.1; then start **V0.2** (lab setup & spikes) in a new session.
