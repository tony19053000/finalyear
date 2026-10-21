/**
 * Pure helpers of the Plan tab (V0.14 G5, D300): term order, cell texts.
 *
 * Nothing here maps a term to a phase or rebuilds a sentence: the per-term
 * actuals (`actual_s`, `ratio`), the term notes and the margin sentence come
 * from the decisions answer as the controller computed them (D320), the same
 * strings `compute explain` prints.
 */
import type { CandidateOut, DecisionOut } from "../../pages/jobs/jobsApi";
import { formatDuration } from "./format";

export type TermOut = NonNullable<CandidateOut["estimate"]>["terms"][string];
export type MarginTestOut = NonNullable<CandidateOut["margin_test"]>;

/** The estimator's terms, in the order `compute explain` prints them. */
export const TERM_ORDER = [
  "queue",
  "transfer",
  "env",
  "compute",
  "output",
  "items_fetch",
  "chunk_overhead",
  "tail",
  "combine",
  "risk",
] as const;

/** The five measured phases of an attempt (ActualOut.phases_ms). */
export const PHASES = ["queue", "stage", "env", "run", "collect"] as const;

/** Seconds as the Jobs pages show durations ("—" when absent). */
export function seconds(s: number | null | undefined): string {
  return s == null ? "—" : formatDuration(s * 1000);
}

/** "×0.65" (actual / predicted). */
export function ratioText(ratio: number | null | undefined): string {
  return ratio == null || !Number.isFinite(ratio) ? "—" : `×${ratio.toFixed(2)}`;
}

/** Every term any candidate of the decision has: the known ones in order, then the rest by name. */
export function termNames(decision: DecisionOut): string[] {
  const present = new Set<string>();
  for (const c of decision.candidates) {
    for (const name of Object.keys(c.estimate?.terms ?? {})) present.add(name);
  }
  const known: string[] = TERM_ORDER.filter((t) => present.has(t));
  const rest = [...present].filter((t) => !(TERM_ORDER as readonly string[]).includes(t)).sort();
  return [...known, ...rest];
}

/** Candidates best first: ranked by rank, then unranked by node id. */
export function orderedCandidates(decision: DecisionOut): CandidateOut[] {
  return [...decision.candidates].sort((a, b) => {
    if (a.rank != null && b.rank != null) return a.rank - b.rank;
    if (a.rank != null) return -1;
    if (b.rank != null) return 1;
    return a.node_id.localeCompare(b.node_id);
  });
}

/** The candidate the decision placed (rank 1 on the chosen node), as `match` reads it. */
export function chosenCandidate(decision: DecisionOut): CandidateOut | undefined {
  return decision.candidates.find((c) => c.rank === 1 && c.node_id === decision.chosen_node_id);
}

/** The decision has rows of a split job (node sets, chunking, margin test). */
export function isSplitDecision(decision: DecisionOut): boolean {
  return decision.candidates.some((c) => c.node_set != null && c.node_set.length > 0);
}

/** No candidate could take the job (every row rejected). */
export function rejectedOnly(decision: DecisionOut): boolean {
  return decision.candidates.length > 0 && decision.candidates.every((c) => c.reject_code != null);
}

/** "12 × 250 items". */
export function chunksText(c: CandidateOut): string {
  return c.chunks == null || c.chunk_items == null ? "—" : `${String(c.chunks)} × ${String(c.chunk_items)} items`;
}

/** "5.75/s (history), first chunk at 3.5 s". */
export function rateText(c: CandidateOut): string {
  if (c.rate_items_s == null) return "—";
  const rate = `${String(Number(c.rate_items_s.toPrecision(4)))}/s (${c.rate_source ?? "—"})`;
  return c.start_s == null ? rate : `${rate}, first chunk at ${seconds(c.start_s)}`;
}

/** "pass: 41.0 s ≤ 46.8 s = (1 − 0.1) × 52.0 s (node_a), forced: node_set"; a rejected row: its code. */
export function marginText(c: CandidateOut): string {
  if (c.reject_code != null) return c.reject_code;
  const t = c.margin_test;
  if (t == null) return "—";
  const verdict = t.passed ? "pass" : "fail";
  const sign = t.passed ? "≤" : ">";
  const forced = t.forced != null && t.forced !== "" ? `, forced: ${t.forced}` : "";
  return (
    `${verdict}: ${seconds(t.set_s)} ${sign} ${seconds(t.threshold_s)} = (1 − ${String(t.margin)}) × ` +
    `${seconds(t.best_single_s)} (${t.best_single_node_id ?? "—"})${forced}`
  );
}

/** "×2.00 n=14 node" for a calibrated term. */
export function calibrationText(term: TermOut): string | null {
  const c = term.calibration;
  if (c == null) return null;
  return `×${c.factor.toFixed(2)} n=${c.n == null ? "?" : String(c.n)} ${c.key_level ?? "?"}`;
}
