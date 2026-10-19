/**
 * A `QualityOut` body as the controller sends it (V0.9 G5c,
 * `bench_quality_models.py`), for the Benchmarks Quality tab tests. Typed as
 * the generated schema, so a contract change breaks the build here first.
 */
import type { QualityConfig, QualityMetric, QualityReport, QualitySummary } from "../pages/benchmarks/qualityApi";

export function qualityMetric(median: number, over: Partial<QualityMetric> = {}): QualityMetric {
  return { n: 3, median, mean: median, std: 1, cv: 0.02, ci_low: median - 20, ci_high: median + 50, ci_coverage: 0.75, outliers: 0, ...over };
}

const NO_MISSING: QualityConfig["missing"] = {
  total_ms: 0,
  compute_ns: 0,
  compute_iterations: 0,
  bytes_in: 0,
  bytes_out: 0,
  queue_ms: 0,
  run_ms: 0,
  node_state: 0,
  node_cpu_percent: 0,
  planner_node_id: 0,
};

export function qualityConfig(configId: string, over: Partial<QualityConfig> = {}): QualityConfig {
  return {
    group_id: "grp_1",
    config_id: configId,
    family: "cpu.primes",
    condition: "idle",
    strategy: "local",
    node_id: "node_a",
    state: "measured",
    planned: 3,
    done: 3,
    n: 3,
    total_ms: qualityMetric(900),
    compute_per_iter_ns: qualityMetric(5.5),
    outlier_attempt_ids: [],
    cv_flag: false,
    reps_flag: false,
    flagged: false,
    records_checked: 3,
    ...over,
    missing: { ...NO_MISSING, ...over.missing },
  };
}

/**
 * Buckets: 6 measured + 1 infra + 2 pre-empted + 1 condition + 1 workload +
 * 2 rejected + 1 other = 14 records (fillers apart).
 */
export function qualitySummary(over: Partial<QualitySummary> = {}): QualitySummary {
  return {
    campaign_state: "done",
    pending: false,
    runs_by_state: { planned: 0, submitted: 0, done: 6, failed: 1, skipped: 0 },
    records: 14,
    bench_role_records: { filler: 2, warmup: 1 },
    measured_records: 6,
    infra_failures: 1,
    owner_preempted: 2,
    infra_failure_rate: 0.0833,
    condition_failures: { "cpu_load:2": 1 },
    workload_failures: { "cpu.primes": 1 },
    rejected_ok: { check_failed: 1, condition_unverified: 1 },
    other_failures: { unavailable: 1 },
    crit1_infra_ok: false,
    crit3_reps_ok: false,
    configs: 2,
    flagged_configs: 1,
    cv_threshold: 0.1,
    ...over,
  };
}

export function qualityReport(campaignId: string, over: Partial<QualityReport> = {}): QualityReport {
  return {
    campaign_id: campaignId,
    quality_version: 1,
    summary: qualitySummary(),
    configs: [
      qualityConfig("cfg_a"),
      qualityConfig("cfg_b", {
        n: 2,
        done: 2,
        total_ms: qualityMetric(700, { n: 2, cv: 0.3, outliers: 1 }),
        compute_per_iter_ns: null,
        outlier_attempt_ids: ["att_1"],
        cv_flag: true,
        reps_flag: true,
        flagged: true,
        missing: { ...NO_MISSING, planner_node_id: 2 },
      }),
    ],
    ...over,
  };
}
