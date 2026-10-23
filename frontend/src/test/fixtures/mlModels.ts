/**
 * V0.15 G5b (D374): typed answers of the ML model routes for the Models tab
 * tests - shaped as `ml_model_routes.py` / `ml_prediction_routes.py` answer
 * (schema.d.ts types, so a contract change breaks the build here).
 */
import type { MlModelsOut, MlPredictionsOut, MlSetDetailOut, MlSetOut } from "../../pages/planner/mlApi";

export const SET_A = `sha256:${"a".repeat(64)}`;
export const SET_B = `sha256:${"b".repeat(64)}`;
export const SET_C = `sha256:${"c".repeat(64)}`;

const MEMBERS: MlSetOut["members"] = [
  {
    term: "compute",
    algorithm: "lightgbm",
    version: "m-compute",
    params: {},
    metrics: { rows: 120, families: 4, groups: 30, selected: "lightgbm", inner_male: 0.1234, inner_se: 0.0101, interval_width: 0.61 },
    reason: null,
    artefact_bytes: 2048,
  },
  {
    term: "transfer",
    algorithm: "none",
    version: "none-transfer",
    params: {},
    metrics: { rows: 9, families: 1, groups: 2 },
    reason: "insufficient_data: 9 rows, 1 families, 2 groups (needs >= 30 rows, >= 2 families, >= 2 groups)",
    artefact_bytes: null,
  },
];

function set(version: string, status: string, reason: string | null, created: string): MlSetOut {
  return {
    set_version: version,
    created_at: created,
    status_changed_at: created,
    status,
    reason,
    dataset_digest: `sha256:${"d".repeat(64)}`,
    feature_version: 2,
    members: MEMBERS,
  };
}

export const SETS: MlSetOut[] = [
  set(SET_C, "rejected", "regret_worse: mean regret 12.0 % > current 8.0 % x 1.05", "2026-10-23T10:00:00+00:00"),
  set(SET_B, "active", null, "2026-10-22T10:00:00+00:00"),
  set(SET_A, "superseded", null, "2026-10-21T10:00:00+00:00"),
];

export function models(over: Partial<MlModelsOut> = {}, inUse: Partial<MlModelsOut["in_use"]> = {}, retrain: Partial<MlModelsOut["retrain"]> = {}): MlModelsOut {
  return {
    in_use: {
      mode: "auto",
      pinned_config: null,
      kind: "active",
      in_use_set: SET_B,
      unavailable: null,
      ml_predict_last_ms: 1.234,
      ml_predictor_errors_total: 2,
      ...inUse,
    },
    serving_set: SET_B,
    sets: SETS,
    retrain: {
      available: true,
      missing: [],
      running: false,
      running_since: null,
      retrains: 3,
      failures: 0,
      last: null,
      interval_s: 21600,
      ...retrain,
    },
    ...over,
  };
}

export function detail(version: string, gate: MlSetDetailOut["gate"] = GATE): MlSetDetailOut {
  const base = SETS.find((s) => s.set_version === version) ?? SETS[1];
  if (base === undefined) throw new Error("fixture");
  return { ...base, gate, inputs: {} };
}

export const GATE: MlSetDetailOut["gate"] = {
  accepted: true,
  rule: null,
  rules_failed: [],
  reference: { kind: "calibration", set_version: null, unreadable_terms: {} },
  terms: {
    compute: {
      algorithm: "lightgbm",
      held_rows: 24,
      male_new: 0.0912,
      male_calibration: 0.2011,
      male_reference: 0.2011,
      coverage: 0.875,
      usable_rate: 0.9167,
      failed: [],
    },
    transfer: { algorithm: "none", held_rows: 0 },
  },
  regret: { status: "passed", groups: 14, candidate_mean_pct: 4.5, current_mean_pct: 6.25 },
};

export const REJECTED_GATE: MlSetDetailOut["gate"] = {
  ...GATE,
  accepted: false,
  rule: "regret_worse",
  rules_failed: ["regret_worse"],
  regret: { status: "rejected", groups: 12, reason: "candidate 12.0 % > current 8.0 % x 1.05" },
};

export const OLD_GATE: MlSetDetailOut["gate"] = { ...GATE, regret: "pending_g4" };

/** 6 compute points (4 ML-used, 1 OOD, 1 no set) and 2 env points. */
export const PREDICTIONS: MlPredictionsOut = {
  limit: 500,
  decisions_read: 7,
  term: null,
  set: null,
  points: [
    { decision_id: 11, term: "compute", node_id: "node_L1", family: "matmul", analytical_s: 10, calibrated_s: 12, ml_s: 9.5, used: true, reason: null, actual_s: 9.2, ml_model_version: SET_B },
    { decision_id: 12, term: "compute", node_id: "node_L2", family: "matmul", analytical_s: 20, calibrated_s: 25, ml_s: 18, used: true, reason: null, actual_s: 17.5, ml_model_version: SET_B },
    { decision_id: 13, term: "compute", node_id: "node_L1", family: "sort", analytical_s: 3, calibrated_s: 3.3, ml_s: 2.9, used: true, reason: null, actual_s: 3.1, ml_model_version: SET_B },
    { decision_id: 14, term: "compute", node_id: "node_L2", family: "sort", analytical_s: 5, calibrated_s: 6, ml_s: 5.1, used: true, reason: null, actual_s: 4.8, ml_model_version: SET_A },
    { decision_id: 15, term: "compute", node_id: "node_L3", family: "hash", analytical_s: 40, calibrated_s: 44, ml_s: null, used: false, reason: "ood", actual_s: 70, ml_model_version: SET_B },
    { decision_id: 16, term: "compute", node_id: "node_L3", family: "hash", analytical_s: 1, calibrated_s: 1.2, ml_s: null, used: false, reason: null, actual_s: 1.1, ml_model_version: null },
    { decision_id: 11, term: "env", node_id: "node_L1", family: "matmul", analytical_s: 0.5, calibrated_s: 0.6, ml_s: null, used: false, reason: "exact_by_construction", actual_s: 0.55, ml_model_version: SET_B },
    { decision_id: 12, term: "env", node_id: "node_L2", family: "matmul", analytical_s: 0, calibrated_s: 0, ml_s: null, used: false, reason: "exact_by_construction", actual_s: 0.4, ml_model_version: SET_B },
  ],
};
