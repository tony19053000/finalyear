/**
 * The controller routes the Planner page reads (V0.14 G5, D300, D318).
 *
 * * `GET /v1/planner/calibration[?version=N]` (V0.14 G2) - the calibration in
 *   use, the active version's factors (each with `n` and its key level) and
 *   the latest versions' summaries. Typed from schema.d.ts.
 * * `GET /v1/planner/eval-reports` / `/{id}` (V0.14 G4, D318) - the offline
 *   evaluator's stored reports.
 *
 * MERGE (Lead, G4 + G5): the eval-report types below are a hand-written
 * mirror of G4's `controller/api/planner_eval_models.py` (commit 2423f476,
 * D318 contract). When G4's models are in openapi.json, replace this block by
 * `Schemas["EvalReportListOut"]` etc. (same field names) and delete the
 * mirror; `npm run typecheck` then proves the page against the real contract.
 * Until G4's routes exist the controller answers 404 and the page says so
 * (never fake data); tests use `src/test/fixtures/evalReports.ts`.
 */
import { apiRequest } from "../../api/client";
import type { components } from "../../api/schema";

type Schemas = components["schemas"];
export type CalibrationView = Schemas["CalibrationViewOut"];
export type CalibrationOut = Schemas["CalibrationOut"];
export type CalibrationSummary = Schemas["CalibrationSummaryOut"];
export type FactorOut = Schemas["FactorOut"];

// ---- BEGIN D318 mirror (replace with Schemas[...] at the G4 merge) ----------
export type VerdictStatus = "PASS" | "FAIL" | "not_separable" | "too_few_groups" | "leaked";

export interface VerdictOut {
  criterion: 1 | 2;
  status: VerdictStatus;
  would_be: VerdictStatus | null;
  value: number | null;
  threshold: number | null;
  counted_groups: number;
  detail: string;
}

export interface BootstrapCIOut {
  low: number | null;
  high: number | null;
}

export interface PolicyResultOut {
  key: string;
  policy: string;
  kind: "rule" | "cost_model" | "oracle";
  calibrated: boolean | null;
  objective: string | null;
  counted_groups: number;
  unmeasured_groups: number;
  no_replay_groups: number;
  mean_regret_pct: number | null;
  ci_pct: BootstrapCIOut;
  mean_regret_ms: number | null;
  within_threshold_pct: number | null;
  zero_regret_groups: number;
}

export interface FamilyResultOut {
  family: string;
  key: string;
  counted_groups: number;
  unmeasured_groups: number;
  no_replay_groups: number;
  mean_regret_pct: number | null;
  mean_regret_ms: number | null;
  within_threshold_pct: number | null;
}

export interface CalibrationUsedOut {
  family: string;
  source: "refit" | "active" | "none";
  version: number | null;
  fitted_runs: number;
  excluded_runs: number;
  factors: number;
  digest: string | null;
}

export interface EvalSettingsOut {
  regret_threshold_pct: number;
  within_target_pct: number;
  min_groups: number;
  bootstrap_resamples: number;
  bootstrap_seed: number;
  ci_level: number;
  criterion_policy: string;
}

export interface EvalCountsOut {
  groups: number;
  evaluated_groups: number;
  labels: Record<string, number>;
  no_replay_groups: number;
  records: number;
  records_used: number;
  records_skipped: Record<string, number>;
}

export interface EvalConfigOut {
  config_id: string;
  strategy: string;
  node_id: string;
  origin_node_id: string;
  node_set: string[];
  state: "measured" | "unsupported";
  median_ms: number | null;
  repetitions: number;
}

export interface GroupPickOut {
  key: string;
  status: "counted" | "unmeasured" | "no_replay";
  config_id: string | null;
  regret_pct: number | null;
  regret_ms: number | null;
  votes: Record<string, number>;
}

export interface GroupResultOut {
  campaign_id: string;
  group_id: string;
  family: string;
  condition: string;
  label: "best" | "tie" | "insufficient" | "pending";
  best_config_id: string | null;
  tied_config_ids: string[];
  best_median_ms: number | null;
  status: "evaluated" | "excluded" | "no_replay";
  excluded_reason: string | null;
  records: number;
  records_used: number;
  configs: EvalConfigOut[];
  picks: GroupPickOut[];
}

export interface EvalReportOut {
  eval_version: number;
  campaign_ids: string[];
  holdout: string | null;
  leaked: boolean;
  settings: EvalSettingsOut;
  counts: EvalCountsOut;
  verdicts: VerdictOut[];
  policies: PolicyResultOut[];
  families: FamilyResultOut[];
  calibrations: CalibrationUsedOut[];
  groups: GroupResultOut[];
}

export interface EvalReportSummaryOut {
  id: number;
  created_at: string;
  campaign_ids: string[];
  holdout: string | null;
  leaked: boolean;
  eval_version: number;
  evaluated_groups: number;
  verdicts: VerdictOut[];
}

export interface EvalReportListOut {
  reports: EvalReportSummaryOut[];
}

export interface EvalReportRecordOut {
  id: number;
  created_at: string;
  campaign_ids: string[];
  holdout: string | null;
  leaked: boolean;
  report: EvalReportOut;
}
// ---- END D318 mirror ---------------------------------------------------------

/** `GET /v1/planner/calibration` - the active version (or `version`). */
export function getCalibration(version: number | null, signal?: AbortSignal): Promise<CalibrationView> {
  return apiRequest<CalibrationView>("/v1/planner/calibration", {
    signal,
    query: version === null ? {} : { version },
  });
}

/** Newest reports first (summaries: verdicts, evaluated groups). */
export const EVAL_LIST_LIMIT = 20;

/** `GET /v1/planner/eval-reports`. */
export function listEvalReports(signal?: AbortSignal): Promise<EvalReportListOut> {
  return apiRequest<EvalReportListOut>("/v1/planner/eval-reports", { signal, query: { limit: EVAL_LIST_LIMIT } });
}

/** `GET /v1/planner/eval-reports/{id}` - one full report. */
export function getEvalReport(id: number, signal?: AbortSignal): Promise<EvalReportRecordOut> {
  return apiRequest<EvalReportRecordOut>(`/v1/planner/eval-reports/${String(id)}`, { signal });
}
