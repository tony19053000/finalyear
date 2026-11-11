# Test plan to v1.0 (L1 lab view) - updated 2026-11-12

Step-by-step list of what is left. Times are estimates. Tick `[x]` only with REPORT evidence.
Release now on all laptops: **v0.17.2**. Next release: **v0.17.3** (round 11 = D452, D453, D454, D456, D457, D458; ETA ~7-9 h from 05:15 on 2026-11-12).

## A. Tests still left (on real laptops)

| # | Done | Test (roadmap box) | What it checks | Who | Time | When |
|---|---|---|---|---|---|---|
| 1 | [x] | BUG-4 a process check | one python.exe per chunk on L4 | L4 | 5 min | DONE - PASS 2026-11-12 |
| 2 | [ ] | **1.0.7** demo scripts 01-05 | 5 demos run start to end | L1 + all | ~1 h | now: 02, 03, 05 + 04 without Local act; 01 + 04-Local after v0.17.3 (D457) |
| 3 | [ ] | **V0.2** old checks (0.2.2-0.2.7, 0.2.10, 0.2.11) | inventory, GPU stack, network, speeds, hardware scores | all | ~2 h | now |
| 4 | [ ] | **0.8.10** dashboard visual review | the dashboard looks and works right (people look at it) | whole team | ~2.5 h | now (can run alongside) |
| 5 | [ ] | **0.3.15 / 0.4.15 / 0.6.13 / 0.7.12 / 0.10.10** leftover rows | old lab boxes; most rows already passed | all | 0-2 h each | after LEAD ticks and lists what is left |
| 6 | [ ] | **v0.17.3 re-test** | D452 exclude-node skips revoked, D453 revoke shown, D454 `data rm` frees disk, D456 planner predicts real split time on L2/L4, D457 `run --local` (demo 01 + 04 Local), D458 venv uses new python after upgrade | L1 + all | ~2 h | after v0.17.3 |
| 7 | [ ] | **0.17.6** upgrade + uninstall | 0.17.2 -> 0.17.3 upgrade works, clean removal, old-Python venv finding | L1 + one Windows laptop | ~1 h | with the v0.17.3 upgrade |
| 8 | [ ] | **0.14.8** planner, new workloads | planner predictions + picks on unseen jobs (C1-C4) | L1 + all | 3-10 h (campaign) | after v0.17.3 |
| 9 | [ ] | **0.9.10** overnight benchmark | full benchmark campaign | all 4, overnight | 1 night (~10 h) | after v0.17.3 |
| 10 | [ ] | **0.15.11** ML data + training check | >= 2000 runs, train ML, compare with planner | all 4, overnight | 1 night (<= 24 h) + 1 h | after v0.17.3 (can share a night with #9) |
| 11 | [ ] | **0.17.8** fresh-machine install | install from zero on a clean laptop | one clean machine | ~2 h | release candidate |
| 12 | [ ] | **1.0.1** full acceptance re-run | all criteria V0.3-V0.17 on the release build | all | 2-3 days | release candidate |
| 13 | [ ] | **1.0.2** 72 h soak + chaos | 3 days non-stop with planned failures | all 4 | 3 days + 1 setup day | release candidate |

Test time total: ~8-9 working days, incl. 1-2 overnight runs and the 3-day soak.
Parallel rule: tests that measure time (planner, benchmark, ML, speed) run ALONE; anything that restarts the
controller (0.17.6) runs alone; looking-only work (0.8.10) can run alongside.

## B. Work other than testing

| # | Done | Work | Who | Time |
|---|---|---|---|---|
| 1 | [ ] | Round 11 -> v0.17.3 (D452, D453, D454, D456, D457, D458) | BUG-FIX | ~7-9 h |
| 2 | [ ] | LEAD ticks roadmap boxes + status bar | LEAD | ~1 h |
| 3 | [x] | Old-Python venv finding -> D458 in v0.17.3; do NOT remove old 0.17.1-1 folders until then | LEAD | decided |
| 4 | [x] | Demo 01 `--local` finding -> D457 in v0.17.3; no workaround today | LEAD | decided |
| 5 | [ ] | 0.2.12 write-up of V0.2 findings | LEAD | ~2 h |
| 6 | [ ] | 0.15.TD1 failure classifier (only if enough failure data) | BUG-FIX | ~1 day, maybe skipped |
| 7 | [ ] | Fixes for anything the tests find | BUG-FIX | ~1-2 days per round |
| 8 | [ ] | 1.0.3 performance tuning pass | BUG-FIX | ~2-3 days |
| 9 | [ ] | 1.0.4 final security review | reviewer + LEAD | ~1-2 days |
| 10 | [ ] | 1.0.5 documentation complete | agents | ~1-2 days (mostly done) |
| 11 | [ ] | 1.0.6 final evaluation report | LEAD + agents | ~3-4 days |
| 12 | [ ] | 1.0.8 release tag v1.0.0 + changelog | LEAD | ~0.5 day |
| 13 | [ ] | PROJECT_SUMMARY + college presentation | team + LEAD | ~1-2 days |
| 14 | - | Postponed (future work): Windows warm python worker (ADR-0024), per-chunk phase times, lane end +2.6/4.2 s, L2/L4 live gpus list empty | - | not in v1.0 |
| 15 | [ ] | Decide Mode 2 + Mode 4 (deferred, V0.11-V0.13, V0.16) | team | weeks, if built |

About 2.5-3 weeks to v1.0 without Modes 2 and 4.

## Open findings (2026-11-12)

- **Demo 01 `--local` on a packaged Linux install:** `compute run --local` (as user aayush) fails "this laptop has no
  MeshForge agent identity yet" - the system agent's identity is in `/var/lib/meshforge/identity/node.json`, folder
  `drwxr-x--- meshforge:meshforge`, so the CLI user cannot read it. Blocks demo 01 and the Local act of demo 04 on L1.
- **Old-Python venv after upgrade (L4):** chunks run the 0.17.1-1 interpreter under agent 0.17.2-1; env hash ignores
  interpreter path/version; removing old version folders would break cached venvs.
