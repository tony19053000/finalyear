# ADR-0023: Release acceptance — one manifest, one check, evidence tied to the release build

> **Status:** Proposed (2026-10-27) — V1.0 brief approved by the team 2026-10-27 (D406–D421, Q42–Q54); ADR pending formal acceptance · **Deciders:** team + Lead agent

## Context
Roadmap V1.0 criterion 1 says *"all acceptance criteria pass on the release build"*. Those criteria are the
**Success criteria** blocks of V0.1–V0.17 (73 lines, brief `docs/briefs/V1.0-release.md` §0). Today they are
proven in scattered places: thousands of tests across five tiers, 13 open 🧑 lab tasks with their own
checklists and analyzers, and four deferred versions (ADR-0012) that will never be proven in v1.0. Nobody can
answer "which criterion is still unproven on `v1.0.0-rc.N`?" without reading the whole roadmap and test tree,
and a late code fix silently invalidates some of the old evidence but not all of it.

The brief decides the shape (D406 release build, D407 manifest, D408 check script); this ADR records the
mechanism so later sessions (G1–G6, the lab campaign) build on it rather than around it. The research-evaluation
half of V1.0 (D415–D419) has its own harness (G2) and is out of scope here.

## Decision
1. **Manifest `release/acceptance.yaml`** (schema `meshforge.acceptance/1`): exactly one row per success
   criterion of every roadmap version below V1.0, id `<version>-c<n>` (e.g. `V0.6-c2`), with the roadmap text
   **verbatim**. Each row has one `kind`:
   - `auto` — `evidence.tests` (pytest node ids) and optional `evidence.commands` (repo scripts); optional
     `evidence.flags` limited to `--run-slow`, `--run-chaos`, `--run-perf`.
   - `lab` — `evidence.section` = a V1.0 campaign section **L0–L5** (brief §4), plus the old checklist and the
     analyzer command; optional `evidence.tests` are *supporting* harness tests (collected, not run).
   - `waiver` — `reason` + `decision` (ADR-NNNN or a `| Dnnn |` decision-table row). Only ADR-0012 deferrals
     (V0.11, V0.12, V0.13, V0.16) and decisions the team approved (Q44).
   Every row has `affected_by` source globs (fnmatch, `*` crosses `/`); required for `lab` rows.
2. **Rule for `auto` vs `lab`.** A criterion is `auto` when the property is fully decided by our code and the
   harness reproduces the real condition (real processes, sockets, PostgreSQL, Docker). It is `lab` when the
   criterion names physical hardware, a real link or cable, timings on real links, several laptops/OSes, a
   human judgment, a `(calibrate)` threshold, or real-world duration/scale. Harness tests that approximate a lab
   criterion are listed as supporting tests of the lab row, never as its proof. Result today: 31 auto,
   27 lab, 15 waiver.
3. **Coverage gate** `tests/unit/release/test_manifest_coverage.py` (unit tier, every CI run): parses the
   roadmap; fails if a criterion has no row, a row has no criterion, a row's text drifted from the roadmap, a
   waiver cites no existing decision, a lab row lacks a section or redo globs, or any named node id does not
   exist (static AST check, every OS) or does not **collect** (`pytest --collect-only`, Linux with all extras).
4. **Check script** `scripts/release_check.py` (+ `release_manifest.py`, `release_outcomes_plugin.py`):
   - runs the `auto` node ids (one pytest process per tier and flag set, through `pytest_exit_guard.py`) and
     the commands; a row is **PASS** only with ≥ 1 *passed* test per node id and no failure; a node id whose
     tests all skipped is **MISSING** (a skip is not evidence); a crashed pytest fails its rows;
   - reads `lab` evidence: JSON files under `lab/reports/raw/` with schema `meshforge.acceptance-evidence/1`
     (`results: [{row, verdict, sha, package_version, recorded_at, detail, evidence}]`); the newest entry per
     row counts; `release_check.py record-lab` writes such a file for checklist-driven rows, and later
     analyzers (G1 soak, G2 evaluation) emit the same schema;
   - **D406 redo rule:** lab evidence counts only from a release package (`package_version` not `dev`), and
     only if it was recorded at the target SHA **or** no file matching the row's `affected_by` changed between
     its SHA and the target (`git diff --name-only`); otherwise the row is MISSING ("stale");
     - the evidence `sha` must be a full 40-hex commit SHA - a movable ref (`HEAD`, a branch) would resolve
       to the current checkout and never go stale, so it is invalid evidence;
     - `pyproject.toml` and `uv.lock` (the dependency set) count as affected for **every** lab row backed
       by source (any `affected_by` glob under `src/`), without each row listing them;
     - a `package_version` different from the release's (`--package-version`) does **not** by itself
       stale the evidence: the SHA diff decides, since an rc rebuilt from the same SHA ships the same code.
       The mismatch is printed in the row's detail so a reviewer sees it;
     - the newest entry decides; equal `recorded_at` timestamps tie-break on the evidence file path, so the
       verdict never depends on directory order;
   - a selection that leaves no row of the requested kind (`--rows` / `--only`) is a usage error (exit 2):
     a gate that judged nothing never exits 0;
   - writes `docs/reports/release-v1.0-acceptance.{md,json}` (SHA, package version, dirty-tree flag, per-row
     PASS/FAIL/MISSING/WAIVED/NOT_RUN) and exits 0 only when no evaluated row is FAIL or MISSING
     (`--only auto|lab` counts that kind only). `--sha` must equal the checked-out HEAD when auto rows run.
5. **V1.0's own criteria are not rows.** Criterion 1 is this report at exit 0; criteria 2–3 are the soak
   analyzer and the evaluation report (G1, G2, G6).

## Alternatives considered
- **A Markdown checklist per version, ticked by hand.** No machine check that every criterion is covered or
  that a ticked box still holds after a fix; this is what the 13 open lab checklists already are.
- **pytest markers per criterion (`@pytest.mark.criterion("V0.6-c2")`).** Spreads the mapping over ~70 files,
  cannot express lab or waiver rows, and edits hundreds of reviewed tests for no behavioural gain.
- **Re-run the whole test suite for criterion 1.** Already done by CI on every commit; it does not say which
  criterion is proven by what, and says nothing about lab or waived criteria.
- **Invalidate all lab evidence on any new rc.** Simple and safe, but a one-line docs fix would cost the team
  another 72 h soak; the `affected_by` rule limits the redo to rows whose code changed.
- **Status per row by the newest *passing* evidence.** Would hide a later FAIL; the newest entry wins instead.

## Trade-offs
+ One file is the full list of what v1.0 claims and how each claim is proven; the report is one command.
+ Drift is caught in CI: a reworded criterion, a renamed test, or a missing decision fails the unit tier.
+ Skips cannot pass for evidence; dev-checkout lab runs cannot count; late fixes redo only affected rows.
− `affected_by` globs are hand-written: too narrow and stale evidence passes, too wide and the team redoes
  labs needlessly. Reviewers must check them when a row's code moves.
− The auto run is long (slow fresh-clone unit run, the 4 × 100 / 10 k-job dashboard perf test, cluster
  scenarios): hours, not minutes. It runs on the release candidate, not on every commit.
− The coverage test's real collection needs Linux + all extras; Windows runs only the static check.
− `MESHFORGE_USE_INSTALLED` (D408: cluster tier against `/opt/meshforge/current`) is **not implemented** yet:
  the harness does not read it, so `auto` rows run from the source checkout at the release SHA (Q43 allows
  that for source-level tests). Installed-build evidence today comes from the lab rows (L1–L3).

## Consequences
- New files: `release/acceptance.yaml`, `scripts/release_check.py`, `scripts/release_manifest.py`,
  `scripts/release_outcomes_plugin.py`, `tests/unit/release/`.
- Changing a roadmap success criterion now requires updating its manifest row in the same commit.
- G1 (soak) and G2 (evaluation) analyzers must emit `meshforge.acceptance-evidence/1` for their rows; the V1.0
  campaign checklist (`lab/checklists/V1.0-campaign.md`) references row ids and ends each step with
  `release_check.py record-lab` or the analyzer's evidence file.
- G6 runs `release_check.py --sha <rc sha> --package-version <rc>` on the tagged commit; the tag rule (D421)
  needs exit 0.
- Revisit if: the cluster harness gains installed-package mode (then auto rows record the package version too),
  or the team approves a waiver beyond ADR-0012 (cite its D-number).
