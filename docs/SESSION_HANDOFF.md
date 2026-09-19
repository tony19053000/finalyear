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

**Last updated:** 2026-09-20 (later) · **Updated by:** Lead session (account 1)

---

## 1. Where the project is

- **Version:** V0.2 (lab setup & feasibility validation). V0.1 is code-complete; its last two
  tasks (0.1.4, 0.1.10) only await the **first real GitHub CI run**, which needs a human push.
- **Committed and done:** 0.2.11 S7 spike (reviewer APPROVE, commit `feat(0.2.11)`; awaits 🧑 runs incl. a torch+CUDA laptop). 0.2.5 S1 spike + `_harness/` (reviewer APPROVE, commit `feat(0.2.5)`; awaits 🧑 lab runs). 0.2.2 `lab/inventory.py` (commit `75d8df5`) — awaits 🧑 runs on the
  4 laptops. ADR-0011 written (commit `b3ddd4d`), Proposed, awaiting team approval.
- **0.2.1:** Laptop 1's NVIDIA driver is **fixed** (RTX 3050 Laptop, driver 595.91.07, 4 GiB,
  compute capability 8.6, Secure Boot still enabled). Still `[~]` because
  `torch.cuda.is_available()` cannot be checked until torch is installed (ADR-0011 / 0.2.3).

## 2. In flight right now (uncommitted work in the working tree)

**Exact next task: 0.2.7 S3 transport.** On 2026-09-20 the Lead launched an `implementer`
run for S3 (prompt: read `_s3/`, fix the two known bugs below, write unit + component tests,
real loopback run, write `lab/spikes/S3.md` + README subsection, all gates). If you find
uncommitted S3 test files or `S3.md` in the tree, that run was cut off mid-way — check
`git status`, run the S3 gates, and continue from whatever is missing. If the tree is clean,
start the implementer run again with the same brief. Then `tester` → `reviewer` → commit
`feat(0.2.7)` → mark `[~]` in the roadmap with evidence. After S3, do S2 the same way.

Two spikes remain from the 2026-09-19 usage-limit interruption. Their code is in commit
`b039b0f`; all S1 and S7 work is now finished and committed (`ef065ac`, `8072b9f`).

| Task | State | What is missing |
|---|---|---|
| **0.2.7 S3 transport** (`s3_transport.py`, `_s3/`) | code written, **no tests yet**, interrupted while fixing the report's "best chunk" bug and the verdict wording for parallel ranges | write unit + component tests, `lab/spikes/S3.md`, finish those two fixes; then tester + reviewer. |
| **0.2.6 S2 cross-OS** (`s2_crossos.py`, `_s2/`) | code + most unit tests written, interrupted during verdict aggregation + CLI tests | finish those tests, write `lab/spikes/S2.md`; then tester + reviewer. |

**Gate state (measured 2026-09-20 after S7 commit):** `ruff check` clean, `mypy` clean,
`pytest -m unit` 1315+ passed, `pytest tests/unit/lab -k "s7 or spike"` 280 passed.
Known pre-existing S2 issues (S2 owner fixes them): `tests/unit/lab/test_s2_verdict.py` fails
`ruff format --check`; `tests/component/lab/test_s2_crossos.py` has 1 failure
(`test_session_counts_server_initiated_beats`: 0 server beats) and 1 error
(`test_peer_vanishing_mid_stream_is_recorded_not_hung`: subprocess pids not freed).

**Harness change from S1 round 3 that S2/S3 must respect:** envelope `link` may be `None`
(n/a); `load_dir` skip reasons carry only `path.name`; use `envelope.sanitize_text()` on any
extra markdown; `report.cell()` escapes `|`.

**File ownership while several agents run:** each spike owns only its own paths
(`lab/spikes/<sN>_*.py`, `lab/spikes/_sN/`, `tests/**/test_sN_*.py`, `lab/spikes/SN.md`).
`_harness/**` is shared — only one agent may edit it at a time; others read it.

## 2b. GitHub

Remote `origin` = https://github.com/tony19053000/finalyear.git added 2026-09-20; the **first push is the user's job** (in progress).

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
