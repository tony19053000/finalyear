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

**Last updated:** 2026-09-20 (S2 closed — all V0.2 spike code done) · **Updated by:** Lead session (account 1)

---

## 1. Where the project is

- **Version:** V0.2 (lab setup & feasibility validation). V0.1 is code-complete; its last two
  tasks (0.1.4, 0.1.10) only await the **first real GitHub CI run**, which needs a human push.
- **All four V0.2 spikes are code-complete with reviewer APPROVE** and committed:
  0.2.5 S1 (`ef065ac`), **0.2.6 S2 (`633d474`, closed 2026-09-20)**, 0.2.7 S3 (`81d620f`),
  0.2.11 S7 (`8072b9f`). 0.2.2 `lab/inventory.py` (`75d8df5`). ADR-0011 written, Proposed.
  Every one of these is `[~]` only because it awaits 🧑 runs on real laptops.
- **0.2.1:** Laptop 1's NVIDIA driver is fixed (RTX 3050 Laptop, 595.91.07); still `[~]`
  because `torch.cuda.is_available()` needs torch installed (ADR-0011 / 0.2.3).

## 2. In flight right now

**In flight: 0.2.10 S6 environments spike** (started 2026-09-20 by the Lead session on the
team's instruction). Design is in the roadmap line for 0.2.10 and will be in `lab/spikes/S6.md`.
Round plan is the S2 one: implementer → tester → reviewer, fixing what each returns; commit
`wip(0.2.10)` after each round. If you find uncommitted `lab/spikes/_s6/` or
`s6_environments.py`, an implementer run was cut off: read its files, run the gates, and
re-brief a new implementer with "finish the interrupted S6 run".

**Done 2026-09-20 (team request): the `n = 0` report headline is fixed.**
`_harness/report.py:251` renders `Repetitions per data point: **n = {min}** (smallest n of any
row)`. One skipped/INVALID case (n=0) therefore makes the headline of an otherwise healthy
table read `n = 0` — visible in `lab/reports/2026-09-20-S6-environments.md:34`, where 15 of 19
rows were n=5 and only the disk-skipped `docker-gpu-torch` case (4 metric rows) was n=0. This is the
"confidently wrong sentence" class the S2 reviewer rejected four times, and it affects every
spike's report, not just S6. Intended rendering: state the n of the **valid** rows and count
the invalid ones separately, e.g. `**n = 5** across the 18 valid rows; 1 row INVALID (n < 5),
marked in the table`; a range (`n = 5-10`) when valid rows differ; `n = 0 (no valid rows)`
only when there genuinely are none. Keep the template sentence's existing contract about
marking `n < MIN_VALID_REPS` rows INVALID. `_harness/**` is shared: ONE implementer, then
tester + reviewer, then the **whole** lab suite. Assertion to update:
`tests/unit/lab/test_spike_report.py:88`. Implemented by `n_headline()` in `_harness/report.py`
(+6 unit tests); the S6 report line 34 now reads `**n = 5** across the 15 valid rows; 4 rows
INVALID (n < 5), marked in the table`. Still needs tester + reviewer before the Lead closes it.

**TRAP found while regenerating that report:** `s6_environments.py report --out <existing file>`
rewrites the **whole** file from `TEMPLATE.md`, blanking any hand-written prose (Goal,
"Background load", "Commands run", "Expected vs actual", Conclusion) back to placeholders. The
implementer snapshotted first and restored those sections verbatim, so nothing was lost, but
**never re-run `report` over a report a human has filled in without a backup.** Every spike's
report CLI shares this shape. Follow-up worth doing: make `report` preserve human-written
sections (or refuse to overwrite without `--force`).

**What to do next (agent side, while humans run the lab):** pick ONE of
- the S3-review follow-up: promote `spikes._s1.protocol` (address rules, framing,
  `ProtocolError`) into `_harness/` so S2/S3/S7 stop importing another spike's internals
  (roadmap "Blockers & problems" table, 2026-09-20 row). Shared `_harness` edit → one
  implementer only, then tester + reviewer, then run the **whole** lab suite
  (`uv run pytest tests/unit/lab tests/component/lab`, ~5 min);
- or 0.2.12 (spike follow-ups + ADR-0003/0005 updates) **once any lab data exists** —
  do not start it on loopback numbers.

**How the S2 round went** (precedent for future spikes): implementer finished the
interrupted run → tester FAIL (6 defects pinned by tests) → fix → tester FAIL (1 new defect
in the shared harness) → fix → reviewer REQUEST_CHANGES ×4 → APPROVE on pass 5. The
reviewer's theme every time: **a generated sentence or cell that could be confidently
wrong** (a loopback row that looks real; a death time measured when the client noticed;
an operator assertion phrased as a measurement; any corrupt result-file field crashing
`report` instead of rendering "?"/TBD/INVALID). Build the next spike with that bar from
the start: every value read from a result file goes through a shape guard (`_mapping()`,
`_records()` in `_s2/verdict.py` are the pattern), and every loopback artefact must be
marked by the tool itself.

**Harness rules every spike must respect:** envelope `link` may be `None` (n/a);
`load_dir` skip reasons carry only `path.name`; `envelope.sanitize_text()` on any extra
markdown; `report.cell()` for *any* interpolated free text in a markdown table cell;
`_harness/report.py` renders a malformed case entry as one INVALID (n=0) row + note.

**File ownership while several agents run:** each spike owns only its own paths
(`lab/spikes/<sN>_*.py`, `lab/spikes/_sN/`, `tests/**/test_sN_*.py`, `lab/spikes/SN.md`).
`_harness/**` is shared — only one agent may edit it at a time; others read it.

**Gate state (measured 2026-09-20 after `633d474`):** `ruff check` / `ruff format --check`
clean; `mypy lab tests/unit/lab tests/component/lab` clean (111 files);
`pytest tests/unit/lab` 1033 passed; `pytest tests/component/lab` 226 passed, 3 skipped.
Nothing is red.

## 2b. GitHub

Remote `origin` = https://github.com/tony19053000/finalyear.git added 2026-09-20; the **first push is the user's job** (in progress).

## 3. Blocked on humans (nothing agent-side unblocks these)

1. **GitHub push + CI proof** — closes V0.1 (criteria 1–2) and lets laptops 2–4 `git pull`.
   Steps are in `docs/STATUS.md` → Next Recommended Task. **Agents never push.**
1a. **Run the S2 spike on two laptops** — Windows↔Linux, **both directions**, Wi-Fi **and**
   Ethernet: `lab/spikes/S2.md` §3 (server-side firewall TCP 47202–47203 only; the client
   side must have **no** inbound rule and pass `--no-inbound-rule`; also do the Windows
   second-`serve` check in §3.2). Paste back both JSON files + stderr per direction.
   Until then ADR-0002/ADR-0009 have no real cross-OS evidence.
1b. **Run the S3 spike on two laptops**, Wi-Fi **and** Ethernet — procedure in
   `lab/spikes/S3.md` §2 (firewall TCP 47301–47303; one pair at a time; never together with
   S1). Until then ADR-0003/ADR-0005 have no real-link evidence and 0.2.12 cannot start.
   The loopback numbers in `S3.md` §4 bound the tool, **not** the link — never quote them
   as evidence about the network.
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
