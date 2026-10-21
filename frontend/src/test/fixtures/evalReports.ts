/**
 * V0.14 G5: typed fixtures of the D318 evaluation-report API (G4's
 * `planner_eval_models.py`, mirrored in pages/planner/plannerApi.ts) and of
 * `GET /v1/planner/calibration`, for the Planner page tests and the e2e route
 * stub. MERGE: once G4's routes are on main, the e2e test reads real reports
 * (the vitest fixtures stay: they pin the page's states).
 */
import type {
  CalibrationView,
  EvalReportListOut,
  EvalReportOut,
  EvalReportRecordOut,
  PolicyResultOut,
  VerdictOut,
} from "../../pages/planner/plannerApi";

function policy(key: string, kind: PolicyResultOut["kind"], mean: number | null, low: number | null, high: number | null): PolicyResultOut {
  const costModel = kind === "cost_model";
  return {
    key,
    policy: key.split(":")[0] ?? key,
    kind,
    calibrated: costModel ? key.includes(":calibrated:") : null,
    objective: costModel ? (key.split(":")[2] ?? null) : null,
    counted_groups: mean == null ? 0 : 12,
    unmeasured_groups: key === "random" ? 2 : 0,
    no_replay_groups: 1,
    mean_regret_pct: mean,
    ci_pct: { low, high },
    mean_regret_ms: mean == null ? null : mean * 140,
    within_threshold_pct: mean == null ? null : Math.max(0, 100 - mean * 2),
    zero_regret_groups: mean === 0 ? 12 : 3,
  };
}

const VERDICTS: VerdictOut[] = [
  { criterion: 1, status: "PASS", would_be: null, value: 83.3, threshold: 80, counted_groups: 12, detail: "10 of 12 groups within 15 % of the oracle" },
  { criterion: 2, status: "not_separable", would_be: null, value: 9.4, threshold: 6.1, counted_groups: 12, detail: "CI upper 9.4 % >= best rule policy's CI lower 6.1 %" },
];

function report(leaked: boolean): EvalReportOut {
  return {
    eval_version: 1,
    campaign_ids: ["camp_01JA0000000000000000000001", "camp_01JA0000000000000000000002"],
    holdout: leaked ? null : "cpu_sleep",
    leaked,
    settings: {
      regret_threshold_pct: 15,
      within_target_pct: 80,
      min_groups: 10,
      bootstrap_resamples: 10000,
      bootstrap_seed: 20261022,
      ci_level: 0.95,
      criterion_policy: "cost_model:calibrated:min_time",
    },
    counts: {
      groups: 15,
      evaluated_groups: 12,
      labels: { best: 9, tie: 3, insufficient: 2, pending: 1 },
      no_replay_groups: 1,
      records: 90,
      records_used: 84,
      records_skipped: { no_replay: 6 },
    },
    verdicts: leaked
      ? VERDICTS.map((v) => ({ ...v, status: "leaked", would_be: v.status }))
      : VERDICTS,
    policies: [
      policy("random", "rule", 41.5, 30.2, 55.0),
      policy("round_robin", "rule", 37.25, 26.0, 49.5),
      policy("least_loaded", "rule", 12.5, 6.1, 19.75),
      policy("cost_model:uncalibrated:min_time", "cost_model", 8.75, 3.5, 14.0),
      policy("cost_model:calibrated:min_time", "cost_model", 4.5, 1.25, 9.4),
      policy("cost_model:calibrated:prefer_local", "cost_model", null, null, null),
      policy("oracle", "oracle", 0, 0, 0),
    ],
    families: [
      { family: "cpu_sleep", key: "cost_model:calibrated:min_time", counted_groups: 7, unmeasured_groups: 0, no_replay_groups: 0, mean_regret_pct: 3.25, mean_regret_ms: 410, within_threshold_pct: 85.7 },
      { family: "split_hash", key: "cost_model:calibrated:min_time", counted_groups: 5, unmeasured_groups: 0, no_replay_groups: 1, mean_regret_pct: 6.25, mean_regret_ms: 1210, within_threshold_pct: 80 },
    ],
    calibrations: [
      { family: "cpu_sleep", source: leaked ? "active" : "refit", version: leaked ? 7 : null, fitted_runs: 40, excluded_runs: 22, factors: 9, digest: "ab".repeat(32) },
      { family: "split_hash", source: leaked ? "active" : "refit", version: leaked ? 7 : null, fitted_runs: 44, excluded_runs: 18, factors: 8, digest: "cd".repeat(32) },
    ],
    groups: [],
  };
}

export const EMPTY_REPORTS: EvalReportListOut = { reports: [] };

export const REPORT_RECORD: EvalReportRecordOut = {
  id: 4,
  created_at: "2026-10-22T08:00:00Z",
  campaign_ids: report(false).campaign_ids,
  holdout: "cpu_sleep",
  leaked: false,
  report: report(false),
};

export const LEAKED_RECORD: EvalReportRecordOut = {
  id: 5,
  created_at: "2026-10-22T09:00:00Z",
  campaign_ids: report(true).campaign_ids,
  holdout: null,
  leaked: true,
  report: report(true),
};

function summary(record: EvalReportRecordOut): EvalReportListOut["reports"][number] {
  return {
    id: record.id,
    created_at: record.created_at,
    campaign_ids: record.campaign_ids,
    holdout: record.holdout,
    leaked: record.leaked,
    eval_version: record.report.eval_version,
    evaluated_groups: record.report.counts.evaluated_groups,
    verdicts: record.report.verdicts,
  };
}

export const ONE_REPORT: EvalReportListOut = { reports: [summary(REPORT_RECORD)] };
export const TWO_REPORTS: EvalReportListOut = { reports: [summary(LEAKED_RECORD), summary(REPORT_RECORD)] };

const IN_USE = { mode: "auto", pinned_version: null, in_use_version: 7 };
const TERM_PHASE = { transfer: "stage", env: "env", compute: "run", output: "collect" };
const NOT_CALIBRATED = {
  queue: "measured",
  items_fetch: "no_phase",
  chunk_overhead: "no_phase",
  tail: "no_phase",
  combine: "no_phase",
  risk: "derived",
};

export const NO_CALIBRATION: CalibrationView = {
  in_use: { mode: "auto", pinned_version: null, in_use_version: null },
  active_version: null,
  calibration: null,
  term_phase: TERM_PHASE,
  not_calibrated: NOT_CALIBRATED,
  recent: [],
};

export const ACTIVE_CALIBRATION: CalibrationView = {
  in_use: IN_USE,
  active_version: 7,
  calibration: {
    version: 7,
    created_at: "2026-10-22T07:30:00Z",
    status: "active",
    holdout_error_new: 0.112,
    holdout_error_active: 0.187,
    reason: null,
    fit_inputs: {},
    readable: true,
    factors: [
      { term: "compute", key_level: "global", node_id: null, family: null, factor: 1.08, n: 40, median: 1.1 },
      { term: "compute", key_level: "node", node_id: "node_01JA00000000000000000000L2", family: null, factor: 1.3, n: 14, median: 1.41 },
      { term: "transfer", key_level: "node_family", node_id: "node_01JA00000000000000000000L3", family: "split_hash", factor: 0.62, n: 6, median: 0.5 },
    ],
    families: { "sha256:aa": "split_hash" },
  },
  term_phase: TERM_PHASE,
  not_calibrated: NOT_CALIBRATED,
  recent: [
    { version: 8, created_at: "2026-10-22T08:00:00Z", status: "rejected", holdout_error_new: 0.31, holdout_error_active: 0.112, reason: "held-out error 0.310 > 1.05 x 0.112 (active v7)" },
    { version: 7, created_at: "2026-10-22T07:30:00Z", status: "active", holdout_error_new: 0.112, holdout_error_active: 0.187, reason: null },
    { version: 6, created_at: "2026-10-22T07:00:00Z", status: "superseded", holdout_error_new: 0.187, holdout_error_active: null, reason: null },
  ],
};

export const REJECTED_V8: CalibrationView = {
  ...ACTIVE_CALIBRATION,
  calibration: {
    version: 8,
    created_at: "2026-10-22T08:00:00Z",
    status: "rejected",
    holdout_error_new: 0.31,
    holdout_error_active: 0.112,
    reason: "held-out error 0.310 > 1.05 x 0.112 (active v7)",
    fit_inputs: {},
    readable: true,
    factors: [{ term: "compute", key_level: "global", node_id: null, family: null, factor: 2.4, n: 3, median: 3.1 }],
    families: {},
  },
};
