# D462 handoff - per-part ML gate and serving (FIX on L2 -> LEAD, 2026-11-21)

Branch `fix/ml-per-term-gate` (from origin/main). ADR: `docs/adr/0026-per-part-ml-gate.md`. Decision log: D462 row in
`docs/DEVELOPMENT_ROADMAP.md`. Not merged, not tagged.

## What changed
- `controller/ml/train/gate.py` - rules judged per term; a learned term is approved if it fails no rule and beats
  calibration by > `min_effect_log`. Accepted iff >= 1 approved term. Gate doc adds `per_term`, `approved_terms`,
  `rejected_terms` (term -> first failed rule) and `terms[t].approved`. A non-approved term whose calibration is worse
  than the reference model (x1.05) still rejects the set. No term approved -> old verdict (first failed rule).
- `controller/ml/regret.py` - candidate loaded via the serving path, so regret judges the partial set; a regret
  rejection sets `approved_terms = []`.
- `controller/ml/serve.py` - `approved_terms(gate)`, `NOT_APPROVED`, `LoadedSet.not_approved`.
- `controller/ml/registry.py` (`load`), `ml/folds.py` (`loaded_set`, `planner_input`) - load only approved members;
  sets without `approved_terms` (pre-D462) serve every learned member as before.
- `controller/ml/predictor.py` (`ml_input(withheld=)`), `ml/source.py` - pass withheld terms to the planner.
- `controller/planner/predictor.py` - `MlInput.withheld`, `MlReason.NOT_APPROVED` (`not_approved`); per-case
  fallbacks unchanged. Cost formula unchanged.
- `common/ml_explain.py`, `frontend/src/components/jobs/mlText.ts` - text "part not approved by the gate (calibration)".
- `controller/api/ml_model_routes.py` - added fields only: `MlSetOut.approved_terms`, `MlMemberOut.serves`
  (`ml|calibration`), `MlMemberOut.gate_reason`. `frontend/openapi.json` + `src/api/schema.d.ts` regenerated.
- `cli/ml_model_commands.py` - `ML PARTS` column, `(cal)` mark in LEARNED, `SERVES` column per member.
- `frontend/src/pages/planner/{mlView.ts,ModelsTab.tsx}` - "ML parts" column, "Uses" column per term.
- Tests: `tests/unit/ml/test_d462_per_part_gate.py` (11), `frontend/src/pages/planner/mlViewPerPart.test.ts` (3),
  `tests/unit/ml/test_v015_g3g4_set_predictor.py` (kwargs assert now includes `withheld`).

## Tests run on L2 (Windows 11)
- `uv run ruff check .` pass; `uv run ruff format --check .` pass; `uv run lint-imports` 11 kept, 0 broken.
- `uv run mypy --platform linux src` - no issues (610 files). Plain `uv run mypy src` on Windows shows 64 errors in
  agent POSIX code (`os.getgroups`, `os.chown`, ...) - same on main, platform only.
- Unit: `tests/unit/{ml,planner,cli,common,controller}` -m unit: 18243 passed, 264 skipped, 3 xfailed, 5 failed.
  1 was mine (set_predictor kwargs assert) - fixed, file now 7/7 pass. The other 4 also fail on clean origin/main on
  Windows (pre-existing): `common/test_v017_g1_install_mode.py` (2), `common/test_v05_g4_tester_r3_digest_lock.py`
  (1), `controller/test_v017_g2_db_settings.py` (1).
- Frontend: `npx tsc --noEmit` pass, `npm run lint` pass, `npm run check:api` up to date, vitest planner + jobs
  283 passed.

## Not run
- Full unit tier (`-m unit` on all of tests/unit: agent/bench/evaluation/lab/release dirs not run - time).
- PostgreSQL-backed component/integration tests (registry store/pin with a real DB) - no Postgres on this laptop.
- Full frontend `npm test` (only planner + jobs folders).

## How L1 should verify
1. CI on the branch (Linux: full unit + component tiers with Postgres).
2. After deploy: `compute planner ml train --wait` (existing dataset, no new runs). Old rejected sets stay rejected.
3. `compute planner ml models` - the new set `active`, `ML PARTS` expected `combine, env` (on the 2026-11-19 data);
   LEARNED shows e.g. `compute=... (cal), chunk_rate=... (cal), lane_env=... (cal)`.
   `compute planner ml models --set <version>`: SERVES `ml` for env/combine, `calibration` + gate reason for the rest.
4. Submit a split job, then `compute explain <job>`: env/combine terms `ML ... -> ...`; compute/chunk_rate/lane_env
   `ML not used: part not approved by the gate (calibration)`; ood / interval / predictor_error still shown per case.
5. Dashboard Planner > Models: "ML parts" column on the set list, "Uses" column in the set detail.
6. If regret rejects the partial set: set `rejected`, rule `regret_worse`, `approved_terms` empty.
