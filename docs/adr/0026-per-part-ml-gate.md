# ADR-0026: Per-part ML activation gate and serving

> **Status:** Proposed (2026-11-21) — D462, team-approved task; LEAD verifies on L1 · **Deciders:** team + Lead agent
> **Context links:** ADR-0021 (ML residual hybrid), D339 / D356 / D357 / D371 (gate, coverage, effect, regret)

## Context

ADR-0021 already plans ML **per term, per candidate** (each cost term falls back to calibration where the model is
out of distribution or its interval is too wide), but the activation gate judged a model set **all-or-nothing**: one
failing term rejected the whole set. On the lab dataset (L1, 2026-11-19) ML clearly wins `env` (log error 0.44 vs
calibration 2.57) and `combine` (0.75 vs 2.85), loses `compute` (0.56 vs 0.50) and `chunk_rate` (0.33 vs 0.16), and
`lane_env` fails coverage (0.57 < 0.70 on `split.image_classify`; 0.73 < 0.80 pooled). So the set was rejected and the
clear gains on `env` and `combine` never reached the planner.

## Decision

1. The gate's rules (`too_few_held_out`, `coverage`, `family_tolerance`, `worse_than_reference`) are judged **per
   term**, unchanged in thresholds. A learned term is **approved** when it fails none of them and beats the
   calibration on the held-out slice by more than `min_effect_log` (0.02, D357).
2. A set is **accepted** when at least one term is approved. It then serves **only** the approved terms; the gate
   document records `approved_terms` and `rejected_terms` (term -> first failed rule). No term approved -> rejected
   with the first failed rule, as before.
3. A term that is not approved serves calibration. If what serves now has a model for it and calibration is worse
   than that by more than the 5 % tolerance, the set is still rejected (`worse_than_reference`): activation never makes
   a term worse than today. For the same reason, a learned term with **no** held-out rows while what serves now has a
   model for it rejects the set (`too_few_held_out`: it cannot be shown not to be worse), and so does a not-approved
   term whose calibration or reference log error is NaN (not comparable).
4. The D371 regret check loads the candidate through the same serving path, so it judges the **partial** set; a
   regret rejection rejects the set and empties `approved_terms`.
5. Serving (`ModelRegistry.load`, `folds.loaded_set`) loads only the approved members; the others are
   `without_model = not_approved_by_gate` and are passed to the planner as `MlInput.withheld`. The planner answers
   reason `not_approved` for them (calibration). The per-case fallbacks (`ood`, `interval_too_wide`,
   `predictor_error`, ...) are unchanged for approved terms. Reason precedence per term: `exact` (exact by
   construction) > `predictor_error` (the predictor failed for the decision) > `not_approved`; all three serve
   calibration, only the recorded reason differs.
   `ModelRegistry.store`'s loadability check (D2) still requires the artefacts of **every** learned member, approved
   or not, to load: stricter than serving needs, kept on purpose (a set version names all its members, and a set with
   an unreadable artefact is suspect as a whole). A set it rejects records `rule = unloadable_artefact` and empty
   `approved_terms`.
6. A set gated before D462 has no `approved_terms`: it serves every learned member, exactly as before. Existing
   rejected sets stay rejected (no retro-activation, no migration, no automatic re-judge); a retrain produces a new
   verdict. Training is deterministic, so an explicit operator retrain on the **same** dataset yields the **same**
   `set_version`: `ModelRegistry.store` then re-judges the existing rejected row in place (the old verdict is kept in
   `gate.history`). That is the intended activation path for a set rejected before D462. `pin()` still refuses a
   rejected set.
7. Visible everywhere: `compute explain` / plan JSON (`detail.ml.reason = not_approved`, "part not approved by the
   gate (calibration)"), `compute planner ml models` (`ML PARTS` column, `(cal)` marks, a `SERVES` column per
   member) and the dashboard Planner > Models (ML parts, Uses). API fields are only added
   (`MlSetOut.approved_terms`, `MlMemberOut.serves`, `MlMemberOut.gate_reason`).

The cost-model formula and every threshold are unchanged.

## Alternatives

- **Keep all-or-nothing** — simple, but throws away proven per-term gains; the planner already corrects per term, so
  the gate was stricter than the serving design (ADR-0021 §4).
- **Lower the coverage thresholds / make them configurable** — would activate `lane_env` with a poorly calibrated
  interval; hides the problem instead of isolating it.
- **Train a separate set per term** — more registry rows, pins and versions to reason about; one set with an
  approved subset keeps one version per decision and the existing pin/unpin workflow.

## Trade-offs

- A set can now be "active" while serving only some parts; operators must read `ML PARTS` (shown in CLI and
  dashboard) instead of assuming every learned member serves.
- Judging terms independently ignores interactions between terms in one decision; the regret check on the partial
  set covers this at decision level.
- Rejected members' artefacts are still stored (they are part of the set version), costing a little blob space.

## Consequences

- After deploy, L1 retrains (`compute planner ml train --wait`) to get a set with approved parts; on the
  2026-11-19 data `env` and `combine` are expected to serve ML while `compute`, `chunk_rate` and `lane_env` keep
  calibration.
- Decisions planned with a partial set record `not_approved` per term, so replays and evaluations explain exactly
  which source each part used.
