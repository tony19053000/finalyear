/**
 * The controller routes the Planner page reads (V0.14 G5, D300, D318).
 *
 * * `GET /v1/planner/calibration[?version=N]` (V0.14 G2) - the calibration in
 *   use, the active version's factors (each with `n` and its key level) and
 *   the latest versions' summaries. Typed from schema.d.ts.
 * * `GET /v1/planner/eval-reports` / `/{id}` (V0.14 G4, D318) - the offline
 *   evaluator's stored reports. Typed from schema.d.ts (D321: the D318 mirror
 *   was replaced by the generated types at the G4 + G5 merge).
 * * `POST /v1/planner/eval-reports` (admin; V0.15 G6b, D378) - start an
 *   evaluation: 202 `{report_id, status: running}`; the report's `status`
 *   (`running` / `done` / `failed` with a `reason`) is polled with `GET /{id}`.
 *
 * A controller without the evaluator answers 404 on these routes and the page
 * says "not available" (never fake data); a 404 carrying
 * `UNKNOWN_EVAL_REPORT_REASON` is a report id that does not exist.
 */
import { apiRequest } from "../../api/client";
import { ApiError } from "../../api/errors";
import type { components } from "../../api/schema";

type Schemas = components["schemas"];
export type CalibrationView = Schemas["CalibrationViewOut"];
export type CalibrationOut = Schemas["CalibrationOut"];
export type CalibrationSummary = Schemas["CalibrationSummaryOut"];
export type FactorOut = Schemas["FactorOut"];

// D318 contract (G4's planner_eval_models.py), generated into schema.d.ts.
export type VerdictStatus = Schemas["VerdictStatus"];
export type VerdictOut = Schemas["VerdictOut"];
export type PairedComparisonOut = Schemas["PairedComparisonOut"];
export type BootstrapCIOut = Schemas["BootstrapCIOut"];
export type PolicyResultOut = Schemas["PolicyResultOut"];
export type FamilyResultOut = Schemas["FamilyResultOut"];
export type CalibrationUsedOut = Schemas["CalibrationUsedOut"];
export type EvalSettingsOut = Schemas["EvalSettingsOut"];
export type EvalCountsOut = Schemas["EvalCountsOut"];
export type EvalConfigOut = Schemas["EvalConfigOut"];
export type GroupPickOut = Schemas["GroupPickOut"];
export type GroupResultOut = Schemas["GroupResultOut"];
export type LatencyOut = Schemas["LatencyOut"];
export type EvalReportOut = Schemas["EvalReportOut"];
export type EvalReportSummaryOut = Schemas["EvalReportSummaryOut"];
export type EvalReportListOut = Schemas["EvalReportListOut"];
export type EvalReportRecordOut = Schemas["EvalReportRecordOut"];
export type EvalReportStatus = Schemas["EvalReportStatus"];
export type EvalStartOut = Schemas["EvalStartOut"];
export type EvalRequestIn = Schemas["EvalRequestIn"];
/** A report that is `done`: its `report` is set. */
export type DoneEvalRecord = EvalReportRecordOut & { status: "done"; report: EvalReportOut };

export function isDone(record: EvalReportRecordOut): record is DoneEvalRecord {
  return record.status === "done" && record.report != null;
}

/** While a report runs, its view and the list are re-read this often (ms). */
export const EVAL_POLL_MS = 3000;

/** Why a report failed, in words (the API's `reason`; an unknown reason is shown verbatim). */
export const FAILED_REASON_TEXT: Readonly<Record<string, string>> = {
  controller_restart: "the controller stopped while it ran",
  invalid_input: "the request could not be evaluated",
  not_found: "a campaign no longer exists",
  database: "the database failed",
  crashed: "the evaluation crashed (a bug - see the controller log)",
};

export function failedText(reason: string | null): string {
  if (reason === null) return "failed";
  return FAILED_REASON_TEXT[reason] ?? reason;
}

/** The campaign ids typed into the Run-evaluation form: split on commas / white space, de-duplicated. */
export function parseCampaignIds(text: string): string[] {
  const out: string[] = [];
  for (const part of text.split(/[\s,]+/)) {
    if (part !== "" && !out.includes(part)) out.push(part);
  }
  return out;
}

/**
 * The machine `reason` of the controller's 404 for a report id it does not
 * have (planner_eval_routes `UNKNOWN_EVAL_REPORT_REASON`). An unmatched
 * route's 404 (a controller without the evaluator) is the plain `not_found`
 * envelope without a reason, or a non-envelope `http_404` behind a proxy; the
 * `code` alone cannot tell the two apart (D321).
 */
export const UNKNOWN_EVAL_REPORT_REASON = "unknown_eval_report";

/** A 404 meaning "this controller has no evaluation-report route". */
export function isEvalUnavailable(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404 && error.reason !== UNKNOWN_EVAL_REPORT_REASON;
}

/** A 404 meaning "the route exists, this report id does not". */
export function isEvalReportNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404 && error.reason === UNKNOWN_EVAL_REPORT_REASON;
}

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

/** `POST /v1/planner/eval-reports` (admin) - start an evaluation in the background (D378). */
export function startEvalReport(body: { campaign_ids: string[]; holdout: string | null; allow_leak: boolean }): Promise<EvalStartOut> {
  const json: EvalRequestIn = { ...body, background: true };
  return apiRequest<EvalStartOut>("/v1/planner/eval-reports", { method: "POST", json });
}

/** `GET /v1/planner/eval-reports/{id}` - one report: its status, and once done the report. */
export function getEvalReport(id: number, signal?: AbortSignal): Promise<EvalReportRecordOut> {
  return apiRequest<EvalReportRecordOut>(`/v1/planner/eval-reports/${String(id)}`, { signal });
}
