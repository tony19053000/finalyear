/**
 * Pure helpers of the Planner page (V0.14 G5, D300): regret bars with their
 * CI whiskers, verdict tones, number cells. Every number is the report's -
 * nothing here computes a statistic.
 */
import type { StatusTone } from "../../lib/status";
import type { LatencyOut, PolicyResultOut, VerdictOut, VerdictStatus } from "./plannerApi";

export interface RegretBar {
  key: string;
  /** Mean relative regret (%). */
  y: number;
  /** Recharts ErrorBar: [y - CI low, CI high - y] (0 when the bound is unknown). */
  err: [number, number];
  low: number | null;
  high: number | null;
  kind: PolicyResultOut["kind"];
  counted: number;
}

/** One bar per policy that has a mean regret (a policy with no counted group has none). */
export function regretBars(policies: readonly PolicyResultOut[]): RegretBar[] {
  return policies
    .filter((p) => p.mean_regret_pct != null && Number.isFinite(p.mean_regret_pct))
    .map((p) => {
      const y = p.mean_regret_pct ?? 0;
      const low = p.ci_pct.low;
      const high = p.ci_pct.high;
      return {
        key: p.key,
        y,
        err: [low == null ? 0 : Math.max(0, y - low), high == null ? 0 : Math.max(0, high - y)],
        low,
        high,
        kind: p.kind,
        counted: p.counted_groups,
      };
    });
}

export const VERDICT_TONE: Record<VerdictStatus, StatusTone | "neutral"> = {
  PASS: "ok",
  FAIL: "failed",
  not_separable: "degraded",
  too_few_groups: "unknown",
  too_few_decisions: "unknown",
  leaked: "failed",
};

/** "criterion 1: PASS" / "criterion 2: leaked (would be FAIL)". */
export function verdictText(v: VerdictOut): string {
  const would = v.status === "leaked" && v.would_be != null ? ` (would be ${v.would_be})` : "";
  return `criterion ${String(v.criterion)}: ${v.status}${would}`;
}

/** A time in ms: "12.35 ms" or "—". */
export function ms(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : `${value.toFixed(2)} ms`;
}

/**
 * V0.14 G6 (D324): the report's decision-latency block as one line (criterion 3 reads the
 * `node_count` row). Every number is the report's.
 */
export function latencyText(l: LatencyOut): string {
  const parts = [
    `${String(l.decisions)} dispatched decisions timed (${String(l.without_latency)} without a time)`,
    `p50 ${ms(l.p50_ms)}`,
    `p99 ${ms(l.p99_ms)}`,
    `max ${ms(l.max_ms)}`,
    `${String(l.over_budget)} over the ${String(l.budget_ms)} ms budget`,
  ];
  return parts.join(" · ");
}

/** "3 nodes: 90 decisions, p99 4.20 ms" per snapshot size (the report's order). */
export function latencyBuckets(l: LatencyOut): string[] {
  return Object.entries(l.by_node_count).map(
    ([nodes, b]) => `${nodes} nodes: ${String(b.decisions)} decisions, p99 ${ms(b.p99_ms)}`,
  );
}

/** "12.3 %" or "—". */
export function pct(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : `${value.toFixed(1)} %`;
}

/** "[2.1, 9.8] %" or "—". */
export function ciText(low: number | null | undefined, high: number | null | undefined): string {
  if (low == null || high == null) return "—";
  return `[${low.toFixed(1)}, ${high.toFixed(1)}] %`;
}

/**
 * The report's CI level as a label: 0.95 -> "95 % CI", 0.975 -> "97.5 % CI"
 * (the level is the report's `settings.ci_level`, never assumed).
 */
export function ciLabel(level: number): string {
  if (!Number.isFinite(level) || level <= 0 || level >= 1) return "CI";
  return `${String(Number((level * 100).toFixed(1)))} % CI`;
}

/** A paired difference in percentage points: "8.0" / "-1.3" or "—". */
export function points(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "—" : value.toFixed(1);
}

/** "[-1.3, 15.5]" (percentage points) or "—". */
export function pointsCi(low: number | null | undefined, high: number | null | undefined): string {
  if (low == null || high == null || !Number.isFinite(low) || !Number.isFinite(high)) return "—";
  return `[${low.toFixed(1)}, ${high.toFixed(1)}]`;
}

/** A factor as "×1.30". */
export function factorText(value: number): string {
  return `×${value.toFixed(2)}`;
}
