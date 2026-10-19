/**
 * The campaign data quality report (V0.9 G5c, roadmap 0.9.8, D220) as the
 * Benchmarks page reads it.
 *
 * `GET /v1/bench/campaigns/{id}/quality` is being built by G5c IN PARALLEL
 * with this page (D256), so its generated type is not in schema.d.ts yet. This
 * module is the ONE place that knows its shape: a local interface written from
 * the brief's §5g contract, and a parser that keeps only what the answer
 * really holds (a missing number stays `null` and is printed "—"; nothing is
 * filled in). The Lead swaps `QualityReport` for the generated type at merge.
 *
 * A controller without the route answers 404: `getQuality` returns
 * `{ kind: "unavailable" }` and the tab says so - never a crash, never numbers.
 */
import { apiRequest } from "../../api/client";
import { ApiError } from "../../api/errors";

/** The report format this page understands (§5g: `quality_version = 1`). */
export const QUALITY_VERSION = 1;

/** One cost of one configuration: median, CV and the median's CI (bench.stats `summarize`). */
export interface QualityMetric {
  median: number | null;
  cv: number | null;
  ci_low: number | null;
  ci_high: number | null;
}

/** One configuration within its group (§5g item 2 + 3). */
export interface QualityConfig {
  group_id: string;
  config_id: string;
  family: string | null;
  strategy: string | null;
  node_id: string | null;
  planned: number | null;
  done: number | null;
  /** Distinct repetitions with an ok record. */
  n: number | null;
  total_ms: QualityMetric | null;
  compute_per_iter_ns: QualityMetric | null;
  outliers: number | null;
  outlier_attempt_ids: string[];
  /** CV above `[controller.bench] quality_cv_threshold`. */
  cv_flag: boolean;
  /** n < 3 (criterion 3). */
  reps_flag: boolean;
  /** Null count per checked field, only fields with nulls kept. */
  missing: Record<string, number>;
}

/** The campaign summary (§5g item 4). */
export interface QualitySummary {
  runs_by_state: Record<string, number>;
  /** Infra failures / records, `owner_preempted` excluded (the same helper as `status`). */
  infra_failure_rate: number | null;
  owner_preempted: number | null;
  /** `condition_failed` records by condition. */
  condition_failures: Record<string, number>;
  /** Workload failures by family. */
  workload_failures: Record<string, number>;
  /** Filler / warm-up records: counted, never in a statistic. */
  fillers: number | null;
  /** Criterion 1: infra failure rate < 2 %. Null = pending (campaign not terminal) or not reported. */
  crit1_infra_ok: boolean | null;
  /** Criterion 3: every config n >= 3 with a CV. */
  crit3_reps_ok: boolean | null;
  /** The campaign is not terminal yet: verdicts are provisional. */
  pending: boolean;
}

export interface QualityReport {
  campaign_id: string;
  quality_version: number;
  cv_threshold: number | null;
  summary: QualitySummary;
  /** Sorted by (group_id, config_id) as the server sends them. */
  configs: QualityConfig[];
}

export type QualityResult =
  | { kind: "report"; report: QualityReport }
  | { kind: "unavailable" }
  | { kind: "unsupported"; version: unknown };

/* ------------------------------------------------------------ parsing */

type Obj = Record<string, unknown>;

function isObj(value: unknown): value is Obj {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function bool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

/** `{name: count}` with finite numeric counts only. */
function counts(value: unknown): Record<string, number> {
  const out: Record<string, number> = {};
  if (!isObj(value)) return out;
  for (const [key, raw] of Object.entries(value)) {
    const n = num(raw);
    if (n !== null) out[key] = n;
  }
  return out;
}

function metric(value: unknown): QualityMetric | null {
  if (!isObj(value)) return null;
  // The CI may come flat (ci_low/ci_high) or as a pair (`ci: [low, high]`).
  const ci = Array.isArray(value.ci) ? (value.ci as unknown[]) : [];
  return {
    median: num(value.median),
    cv: num(value.cv),
    ci_low: num(value.ci_low) ?? num(ci[0]),
    ci_high: num(value.ci_high) ?? num(ci[1]),
  };
}

function config(value: unknown): QualityConfig | null {
  if (!isObj(value)) return null;
  const groupId = str(value.group_id);
  const configId = str(value.config_id);
  if (groupId === null || configId === null) return null;
  const ids = Array.isArray(value.outlier_attempt_ids) ? (value.outlier_attempt_ids as unknown[]) : [];
  const missing = Object.fromEntries(Object.entries(counts(value.missing)).filter(([, n]) => n > 0));
  return {
    group_id: groupId,
    config_id: configId,
    family: str(value.family),
    strategy: str(value.strategy),
    node_id: str(value.node_id),
    planned: num(value.planned),
    done: num(value.done),
    n: num(value.n),
    total_ms: metric(value.total_ms),
    compute_per_iter_ns: metric(value.compute_per_iter_ns),
    outliers: num(value.outliers) ?? num(value.outlier_count),
    outlier_attempt_ids: ids.filter((id): id is string => typeof id === "string"),
    cv_flag: value.cv_flag === true,
    reps_flag: value.reps_flag === true,
    missing,
  };
}

function summary(value: unknown): QualitySummary {
  const s = isObj(value) ? value : {};
  return {
    runs_by_state: counts(s.runs_by_state ?? s.runs),
    infra_failure_rate: num(s.infra_failure_rate),
    owner_preempted: num(s.owner_preempted),
    condition_failures: counts(s.condition_failures),
    workload_failures: counts(s.workload_failures),
    fillers: num(s.fillers),
    crit1_infra_ok: bool(s.crit1_infra_ok),
    crit3_reps_ok: bool(s.crit3_reps_ok),
    pending: s.pending === true,
  };
}

/** The report from an answer body, or `unsupported` for another `quality_version`. */
export function parseQuality(body: unknown): QualityResult {
  if (!isObj(body) || body.quality_version !== QUALITY_VERSION) {
    return { kind: "unsupported", version: isObj(body) ? body.quality_version : undefined };
  }
  const raw = Array.isArray(body.configs) ? (body.configs as unknown[]) : [];
  const configs = raw.map(config).filter((c): c is QualityConfig => c !== null);
  return {
    kind: "report",
    report: {
      campaign_id: str(body.campaign_id) ?? "",
      quality_version: QUALITY_VERSION,
      cv_threshold: num(body.cv_threshold),
      summary: summary(body.summary),
      configs,
    },
  };
}

/** Flagged configurations first (CV or repetitions), then the server's order. */
export function flaggedFirst(configs: readonly QualityConfig[]): QualityConfig[] {
  const flagged = configs.filter((c) => c.cv_flag || c.reps_flag);
  const clean = configs.filter((c) => !c.cv_flag && !c.reps_flag);
  return [...flagged, ...clean];
}

/** `GET .../quality`; a controller without the route (404) is `unavailable`. */
export async function getQuality(campaignId: string, signal?: AbortSignal): Promise<QualityResult> {
  try {
    const body = await apiRequest<unknown>(`/v1/bench/campaigns/${encodeURIComponent(campaignId)}/quality`, { signal });
    return parseQuality(body);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return { kind: "unavailable" };
    throw err;
  }
}
