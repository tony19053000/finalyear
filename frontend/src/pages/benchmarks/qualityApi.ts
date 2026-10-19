/**
 * The campaign data quality report (V0.9 G5c, roadmap 0.9.8, D220) as the
 * Benchmarks page reads it: `GET /v1/bench/campaigns/{id}/quality`.
 *
 * The shape is the generated `QualityOut` (schema.d.ts, from the controller's
 * OpenAPI). The body is still checked at run time before the tab shows it:
 * a v1 body that does not match `QualityOut` (a proxy page, a broken build)
 * is `invalid` and the tab prints no number at all - it never fills a gap
 * with a zero or shows a partial report whose totals would not add up.
 *
 * A controller without the route answers 404: `getQuality` returns
 * `{ kind: "unavailable" }` and the tab says so - never a crash, never numbers.
 */
import { apiRequest } from "../../api/client";
import { ApiError } from "../../api/errors";
import type { components } from "../../api/schema";

/** The report format this page understands (`quality_version = 1`). */
export const QUALITY_VERSION = 1;

export type QualityReport = components["schemas"]["QualityOut"];
export type QualitySummary = components["schemas"]["QualitySummaryOut"];
export type QualityConfig = components["schemas"]["ConfigQualityOut"];
export type QualityMetric = components["schemas"]["MetricStats"];

export type QualityResult =
  | { kind: "report"; report: QualityReport }
  | { kind: "unavailable" }
  | { kind: "unsupported"; version: unknown }
  | { kind: "invalid" };

/* ------------------------------------------------------------ validation */

type Obj = Record<string, unknown>;

function isObj(value: unknown): value is Obj {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNum(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** A count: a finite, non-negative integer. */
function isCount(value: unknown): value is number {
  return isNum(value) && Number.isInteger(value) && value >= 0;
}

function isNumOrNull(value: unknown): value is number | null {
  return value === null || isNum(value);
}

function isStr(value: unknown): value is string {
  return typeof value === "string";
}

function isBoolOrNull(value: unknown): value is boolean | null {
  return value === null || typeof value === "boolean";
}

/** `{name: count}` with every value a count. */
function isCounts(value: unknown): value is Record<string, number> {
  return isObj(value) && Object.values(value).every(isCount);
}

function hasCounts(value: unknown, keys: readonly string[]): boolean {
  return isObj(value) && keys.every((k) => isCount(value[k]));
}

const RUN_STATES = ["planned", "submitted", "done", "failed", "skipped"] as const;

/** `MissingFields` (bench.quality.REQUIRED_FIELDS). */
export const MISSING_FIELDS = [
  "total_ms",
  "compute_ns",
  "compute_iterations",
  "bytes_in",
  "bytes_out",
  "queue_ms",
  "run_ms",
  "node_state",
  "node_cpu_percent",
  "planner_node_id",
] as const;

function isMetric(value: unknown): value is QualityMetric {
  return (
    isObj(value) &&
    isCount(value.n) &&
    isNum(value.median) &&
    isNum(value.mean) &&
    isNumOrNull(value.std) &&
    isNumOrNull(value.cv) &&
    isNum(value.ci_low) &&
    isNum(value.ci_high) &&
    isNum(value.ci_coverage) &&
    isCount(value.outliers)
  );
}

function isMetricOrNull(value: unknown): value is QualityMetric | null {
  return value === null || isMetric(value);
}

function isConfig(value: unknown): value is QualityConfig {
  return (
    isObj(value) &&
    isStr(value.group_id) &&
    isStr(value.config_id) &&
    isStr(value.family) &&
    isStr(value.condition) &&
    isStr(value.strategy) &&
    isStr(value.node_id) &&
    (value.state === "measured" || value.state === "unsupported") &&
    isCount(value.planned) &&
    isCount(value.done) &&
    isCount(value.n) &&
    isMetricOrNull(value.total_ms) &&
    isMetricOrNull(value.compute_per_iter_ns) &&
    Array.isArray(value.outlier_attempt_ids) &&
    value.outlier_attempt_ids.every(isStr) &&
    typeof value.cv_flag === "boolean" &&
    typeof value.reps_flag === "boolean" &&
    typeof value.flagged === "boolean" &&
    isCount(value.records_checked) &&
    hasCounts(value.missing, MISSING_FIELDS)
  );
}

function isSummary(value: unknown): value is QualitySummary {
  return (
    isObj(value) &&
    isStr(value.campaign_state) &&
    typeof value.pending === "boolean" &&
    hasCounts(value.runs_by_state, RUN_STATES) &&
    isCount(value.records) &&
    isCounts(value.bench_role_records) &&
    isCount(value.measured_records) &&
    isCount(value.infra_failures) &&
    isCount(value.owner_preempted) &&
    isNumOrNull(value.infra_failure_rate) &&
    isCounts(value.condition_failures) &&
    isCounts(value.workload_failures) &&
    isCounts(value.rejected_ok) &&
    isCounts(value.other_failures) &&
    isBoolOrNull(value.crit1_infra_ok) &&
    isBoolOrNull(value.crit3_reps_ok) &&
    isCount(value.configs) &&
    isCount(value.flagged_configs) &&
    isNum(value.cv_threshold)
  );
}

/**
 * The report from an answer body: `unsupported` for another `quality_version`
 * (or a body that is not an object), `invalid` for a v1 body that is not a
 * `QualityOut`. Never throws.
 */
export function parseQuality(body: unknown): QualityResult {
  if (!isObj(body) || body.quality_version !== QUALITY_VERSION) {
    return { kind: "unsupported", version: isObj(body) ? body.quality_version : undefined };
  }
  if (!isStr(body.campaign_id) || !isSummary(body.summary) || !Array.isArray(body.configs) || !body.configs.every(isConfig)) {
    return { kind: "invalid" };
  }
  return {
    kind: "report",
    report: { campaign_id: body.campaign_id, quality_version: QUALITY_VERSION, summary: body.summary, configs: body.configs },
  };
}

/* ------------------------------------------------------------ derived */

function sum(counts: Readonly<Record<string, number>>): number {
  return Object.values(counts).reduce((a, b) => a + b, 0);
}

/**
 * The summary's buckets in reading order. Every run record is in exactly one
 * (docs/TELEMETRY.md "Quality report"), so they add up to `summary.records`;
 * the tab prints the sum next to `records` and says so when they differ.
 */
export function recordBuckets(s: QualitySummary): { id: string; label: string; count: number; by?: Record<string, number> }[] {
  return [
    { id: "measured", label: "Measured (used by the statistics)", count: s.measured_records },
    { id: "infra", label: "Infrastructure failures", count: s.infra_failures },
    { id: "preempted", label: "Stopped by owner", count: s.owner_preempted },
    { id: "condition", label: "Condition failures", count: sum(s.condition_failures), by: s.condition_failures },
    { id: "workload", label: "Workload failures", count: sum(s.workload_failures), by: s.workload_failures },
    { id: "rejected", label: "Ok but rejected by the label filter", count: sum(s.rejected_ok), by: s.rejected_ok },
    { id: "other", label: "Other failures", count: sum(s.other_failures), by: s.other_failures },
  ];
}

/** Flagged configurations first (`flagged`: CV, repetitions or missing fields), then the server's order. */
export function flaggedFirst(configs: readonly QualityConfig[]): QualityConfig[] {
  return [...configs.filter((c) => c.flagged), ...configs.filter((c) => !c.flagged)];
}

/** Required fields with at least one null, `{field: count}`. */
export function missingFields(config: QualityConfig): Record<string, number> {
  return Object.fromEntries(MISSING_FIELDS.map((f) => [f, config.missing[f]] as const).filter(([, n]) => n > 0));
}

/* ------------------------------------------------------------ request */

/**
 * The machine `reason` the controller puts on its 404 for an unknown campaign
 * (bench_routes `_synced`). A controller that HAS the quality route sends it;
 * an unmatched route's 404 (a controller older than the route) is the plain
 * `not_found` envelope without a reason, or a non-envelope body behind a
 * proxy (`http_404`). The `code` alone cannot tell the two apart (both are
 * `not_found`), and matching message text would break on a reworded message.
 */
export const UNKNOWN_CAMPAIGN_REASON = "unknown_campaign";

/**
 * `GET .../quality`. A 404 without the unknown-campaign reason is
 * `unavailable` (the controller has no such route); a 404 WITH it is a real
 * error (the campaign is gone), as is any other failure.
 */
export async function getQuality(campaignId: string, signal?: AbortSignal): Promise<QualityResult> {
  try {
    const body = await apiRequest<unknown>(`/v1/bench/campaigns/${encodeURIComponent(campaignId)}/quality`, { signal });
    return parseQuality(body);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404 && err.reason !== UNKNOWN_CAMPAIGN_REASON) return { kind: "unavailable" };
    throw err;
  }
}
