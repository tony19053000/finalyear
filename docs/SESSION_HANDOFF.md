# SESSION HANDOFF — read this first

> **Purpose:** any Claude Code session, on **any of the team's accounts**, can pick up the work
> here without having seen the previous conversation. A usage limit can end a session mid-task,
> so this file is kept current **continuously**, not only at a tidy session end.
>
> **Cold-start prompt** (paste this as the first message on the new account):
>
> ```
> Read docs/SESSION_HANDOFF.md, then docs/STATUS.md, then the current version in
> docs/DEVELOPMENT_ROADMAP.md, and continue the work exactly where it stopped.
> ```
>
> Order of truth: `PROJECT_BRIEF → ARCHITECTURE → ROADMAP → STATUS → this file`. If this file
> disagrees with the roadmap, the roadmap wins and this file is stale — fix it.

**Last updated:** 2026-09-20 · **Updated by:** Lead session (account 1)

---

## 1. Where the project is

- **Version:** V0.2 (lab setup & feasibility validation). V0.1 is code-complete; its last two
  tasks (0.1.4, 0.1.10) only await the **first real GitHub CI run**, which needs a human push.
- **Committed and done:** 0.2.2 `lab/inventory.py` (commit `75d8df5`) — awaits 🧑 runs on the
  4 laptops. ADR-0011 written (commit `b3ddd4d`), Proposed, awaiting team approval.
- **0.2.1:** Laptop 1's NVIDIA driver is **fixed** (RTX 3050 Laptop, driver 595.91.07, 4 GiB,
  compute capability 8.6, Secure Boot still enabled). Still `[~]` because
  `torch.cuda.is_available()` cannot be checked until torch is installed (ADR-0011 / 0.2.3).

## 2. In flight right now (uncommitted work in the working tree)

Four agent tasks were **interrupted by a usage limit** on 2026-09-19 with their code largely
written but unfinished. Nothing is lost; it is all in the working tree (and in the WIP commit
that follows this file). Resume them in this order:

| Task | State | What is missing |
|---|---|---|
| **0.2.5 S1 network** (`lab/spikes/s1_network.py`, `_s1/`, `_harness/`) | tester PASS round 2; a fix round was interrupted | 3 small fixes: (A) two tests in `tests/component/lab/test_inventory_acceptance.py` assert this machine's NVIDIA driver is broken — it now works, so they must be made host-independent; (B) `test_s1_network_adversarial.py::test_connection_flood_is_capped_and_server_stays_responsive` is flaky in full-suite runs (margin too tight for evict-oldest-pending); (C) `safe_name` in `_harness/envelope.py` must casefold so hosts "Lab1"/"lab1" don't collide on Windows NTFS. Then **reviewer**, then commit. |
| **0.2.11 S7 hardware scores** (`s7_hardware.py`, `_s7/`) | code + unit tests written, interrupted during CLI tests | finish tests; fix `E501` ×2 in `tests/unit/lab/test_s7_report.py` (lines 77, 99) and the mypy error `psutil` stubs in `tests/component/lab/test_s7_hardware.py:53`; write `lab/spikes/S7.md`; then tester + reviewer. |
| **0.2.7 S3 transport** (`s3_transport.py`, `_s3/`) | code written, **no tests yet**, interrupted while fixing the report's "best chunk" bug and the verdict wording for parallel ranges | write unit + component tests, `lab/spikes/S3.md`, finish those two fixes; then tester + reviewer. |
| **0.2.6 S2 cross-OS** (`s2_crossos.py`, `_s2/`) | code + most unit tests written, interrupted during verdict aggregation + CLI tests | finish those tests, write `lab/spikes/S2.md`; then tester + reviewer. |

**Gate state of the working tree (measured 2026-09-20):** `pytest -m unit` → 1229 passed,
1 skipped, 2 xfailed. `ruff check` → 3 errors (2 × E501 above + 1 fixable). `mypy` → 1 error
(psutil stubs). `lint-imports` → clean. So the tree is close to green, not green.

**File ownership while several agents run:** each spike owns only its own paths
(`lab/spikes/<sN>_*.py`, `lab/spikes/_sN/`, `tests/**/test_sN_*.py`, `lab/spikes/SN.md`).
`_harness/**` is shared — only one agent may edit it at a time; others read it.

## 3. Blocked on humans (nothing agent-side unblocks these)

1. **GitHub push + CI proof** — closes V0.1 (criteria 1–2) and lets laptops 2–4 `git pull`.
   Steps are in `docs/STATUS.md` → Next Recommended Task. **Agents never push.**
2. **Team decision on [ADR-0011](adr/0011-torch-index-per-platform.md)** (torch from the cu126
   index). Until it is Accepted and torch is installed, **S4 (0.2.8) and S5 (0.2.9) cannot
   start** and 0.2.1 cannot close.
3. **Run `lab/inventory.py` on all 4 laptops** (commands in `lab/README.md`) — the results
   decide cu126 vs cu130 and fill `docs/LAB.md` §1.
4. **Buy the gigabit switch, set DHCP reservations, set Windows profile to Private** (0.2.4);
   S1 needs both Wi-Fi and Ethernet matrices.

## 4. Traps a fresh session must know

- **Never mark `[x]` without evidence** (test output, CI link, or `lab/reports/…`). Only the
  Lead marks it, after tester PASS **and** reviewer APPROVE.
- **Tests must not depend on this machine's state.** The driver fix broke two tests that had
  "the driver is broken" baked in. Assert behaviour, not hardware.
- **Agents commit locally, one commit per task; humans push.** Never run `git push`.
- **`uv run` re-syncs the environment**; don't edit `pyproject.toml`/`uv.lock` while another
  agent is running tests.
- Lab scripts are stdlib-first: they must work after a plain `uv sync` on native Windows too.
- torch is **not** installed here; GPU paths must record a clean "torch not installed" error.

## 5. Account rotation (three Pro accounts, same machine, same repo)

The accounts share this working directory, so **the repository is the shared memory**. Before
switching, and ideally after every finished step:

1. Update this file (sections 1–3) and `docs/STATUS.md`.
2. Commit work in progress (`wip:` prefix is fine) so the next account sees committed truth and
   can read `git log`.
3. On the new account, paste the cold-start prompt at the top of this file.

If a limit hits mid-task with no warning, the next account recovers from this file plus
`git status` / `git log`; that is exactly why sections 2 and 4 exist.
