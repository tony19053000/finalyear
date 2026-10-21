/**
 * Pure helpers of the Planner page (V0.14 G5, D300): regret bars with their
 * CI whiskers, verdict tones, number cells. Every number is the report's -
 * nothing here computes a statistic.
 */
import type { StatusTone } from "../../lib/status";
import type { PolicyResultOut, VerdictOut, VerdictStatus } from "./plannerApi";

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
  leaked: "failed",
};

/** "criterion 1: PASS" / "criterion 2: leaked (would be FAIL)". */
export function verdictText(v: VerdictOut): string {
  const would = v.status === "leaked" && v.would_be != null ? ` (would be ${v.would_be})` : "";
  return `criterion ${String(v.criterion)}: ${v.status}${would}`;
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

/** A factor as "×1.30". */
export function factorText(value: number): string {
  return `×${value.toFixed(2)}`;
}
