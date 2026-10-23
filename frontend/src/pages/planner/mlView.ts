/**
 * Pure helpers of the Planner page's Models tab (V0.15 G5b, D374): texts of
 * the in-use / retrain state, the per-term cross-validation and held-out
 * numbers, the activation gate (incl. its regret check, D371), and the
 * predicted-vs-actual scatter's points and fallback counts.
 *
 * Every number is the API's; nothing here fits, averages or rescales a
 * prediction - the scatter plots `calibrated_s` / `ml_s` against `actual_s`
 * exactly as the predictions route answered them. The gate document is an
 * open dict (it grew in G5a), so every read is tolerant: a missing or
 * malformed part is shown as "—", never an error.
 */
import { reasonText, shortVersion } from "../../components/jobs/mlText";
import type { MlInUseOut, MlMemberOut, MlModelsOut, MlPredictionPointOut, MlRetrainStateOut, MlSetOut } from "./mlApi";

const DASH = "—";

/** `planner/predictor.ML_TERMS`, in `compute explain` order. */
export const ML_TERM_ORDER = ["transfer", "env", "compute", "output", "lane_stage", "lane_env", "chunk_rate", "combine"] as const;

/** Terms whose prediction is seconds per item (scatter axis label). */
const PER_ITEM_TERMS: ReadonlySet<string> = new Set(["chunk_rate"]);

type Dict = Record<string, unknown>;

function isDict(value: unknown): value is Dict {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/** A metric with 3 decimals, `—` when absent. */
export function metric(value: unknown, digits = 3): string {
  const n = num(value);
  return n === null ? DASH : n.toFixed(digits);
}

/** A share 0..1 as `83.3 %`. */
export function share(value: unknown): string {
  const n = num(value);
  return n === null ? DASH : `${(n * 100).toFixed(1)} %`;
}

/** An integer count, `—` when absent. */
export function count(value: unknown): string {
  const n = num(value);
  return n === null ? DASH : String(Math.round(n));
}

/** `sha256:3a1f00aa…` -> the 12 characters the CLI and the Plan tab show. */
export const setLabel = (version: string | null | undefined): string => (version == null ? "none" : shortVersion(version));

/* ------------------------------------------------------------ state */

export type MlTabState = "unavailable_extra" | "off" | "no_model" | "not_loaded" | "in_use";

/**
 * What the planner does with ML now: `off` (configured off), `in_use`, the
 * serving set `not_loaded` (artefacts gone, library missing), `no_model`.
 * `unavailable_extra` = no `ml` extra on the controller and no set in use.
 */
export function mlState(models: MlModelsOut): MlTabState {
  const u = models.in_use;
  if (u.mode === "off") return "off";
  if (u.in_use_set != null) return "in_use";
  if (!models.retrain.available) return "unavailable_extra";
  if (u.unavailable != null) return "not_loaded";
  return "no_model";
}

/** The in-use line, as `compute planner ml models` prints it. */
export function inUseText(models: MlModelsOut): string {
  const u: MlInUseOut = models.in_use;
  if (u.mode === "off") return "ML is off: the planner uses the calibration only.";
  if (u.in_use_set != null) return `Planning with set ${setLabel(u.in_use_set)} (${u.kind ?? "?"}, mode ${u.mode}).`;
  if (u.unavailable != null) {
    return `Mode ${u.mode}: serving set ${setLabel(models.serving_set)} is not loaded (${u.unavailable}); calibration only.`;
  }
  return `Mode ${u.mode}: no model set in use (calibration only).`;
}

/** The dispatcher's live numbers (G4 reviewer fold-in). */
export function liveText(u: MlInUseOut): string {
  const last = num(u.ml_predict_last_ms);
  const errors = num(u.ml_predictor_errors_total);
  return (
    `last predict ${last === null ? DASH : `${last.toFixed(2)} ms`} (wall clock) · ` +
    `predictor errors ${errors === null ? DASH : String(errors)} since start`
  );
}

/** The retrain task in one line (`compute planner ml models`). */
export function retrainText(r: MlRetrainStateOut): string {
  if (!r.available) return `Training unavailable: the controller has no ml extra (missing ${r.missing.join(", ") || "?"}).`;
  if (r.running) return `Retrain running since ${r.running_since ?? "?"}.`;
  const last = r.last;
  if (last == null) return "No retrain yet.";
  if (last.error != null) return `Last retrain FAILED (${last.error}${last.detail != null ? `: ${last.detail}` : ""}).`;
  if (last.created_set != null) {
    return `Last retrain: set ${setLabel(last.created_set)} ${last.status ?? "?"}${last.reason != null ? ` - ${last.reason}` : ""}.`;
  }
  return `Last retrain wrote nothing (${last.skipped ?? "?"}).`;
}

/** `compute=lightgbm, env=ridge` - the learned terms of a set. */
export function learnedText(members: readonly MlMemberOut[]): string {
  const learned = members.filter((m) => m.algorithm !== "none").map((m) => `${m.term}=${m.algorithm}`);
  return learned.length === 0 ? "none" : learned.join(", ");
}

export type SetTone = "ok" | "failed" | "accent" | "neutral";

export function setTone(status: string): SetTone {
  if (status === "active") return "ok";
  if (status === "rejected") return "failed";
  if (status === "pinned") return "accent";
  return "neutral";
}

/** Pin is offered for a set that is neither rejected nor already pinned (the server refuses the rest too). */
export function canPin(set: MlSetOut): boolean {
  return set.status !== "rejected" && set.status !== "pinned";
}

export function canUnpin(set: MlSetOut): boolean {
  return set.status === "pinned";
}

/* ------------------------------------------------------------ per-term metrics */

export interface TermMetricsRow {
  term: string;
  algorithm: string;
  /** Cross-validation (members[].metrics, G3 fit). */
  rows: string;
  families: string;
  selected: string;
  innerMale: string;
  intervalWidth: string;
  /** Held-out slice (gate.terms[term], D329 slice). */
  heldRows: string;
  maleModel: string;
  maleCalibration: string;
  maleReference: string;
  coverage: string;
  usable: string;
  failed: string;
  reason: string;
}

/** One row per member: its CV numbers and, when the gate ran, the held-out ones. */
export function termMetricRows(members: readonly MlMemberOut[], gate: unknown): TermMetricsRow[] {
  const terms = isDict(gate) && isDict(gate.terms) ? gate.terms : {};
  const order = (t: string) => {
    const i = (ML_TERM_ORDER as readonly string[]).indexOf(t);
    return i === -1 ? ML_TERM_ORDER.length : i;
  };
  return [...members]
    .sort((a, b) => order(a.term) - order(b.term) || a.term.localeCompare(b.term))
    .map((m) => {
      const cv = m.metrics;
      const held = isDict(terms[m.term]) ? (terms[m.term] as Dict) : {};
      const se = num(cv.inner_se);
      const failed = Array.isArray(held.failed) ? held.failed.filter((f): f is string => typeof f === "string") : [];
      return {
        term: m.term,
        algorithm: m.algorithm,
        rows: count(cv.rows),
        families: count(cv.families),
        selected: str(cv.selected) ?? DASH,
        innerMale: num(cv.inner_male) === null ? DASH : `${metric(cv.inner_male)}${se === null ? "" : ` ± ${metric(se)}`}`,
        intervalWidth: metric(cv.interval_width),
        heldRows: count(held.held_rows),
        maleModel: metric(held.male_new),
        maleCalibration: metric(held.male_calibration),
        maleReference: metric(held.male_reference),
        coverage: share(held.coverage),
        usable: share(held.usable_rate),
        failed: failed.length === 0 ? DASH : failed.join(", "),
        reason: m.reason ?? DASH,
      };
    });
}

/* ------------------------------------------------------------ gate */

/**
 * The regret part of the gate (D371). G3 wrote `"pending_g4"` (old rows keep
 * it); G5a writes `{status: passed|rejected|skipped|not_run, reason?,
 * groups?, ...means}`. Unknown keys are shown as they are.
 */
export function regretText(regret: unknown): string {
  if (regret === "pending_g4") return "not checked (set made before the regret check existed)";
  if (typeof regret === "string") return regret;
  if (!isDict(regret)) return DASH;
  const status = str(regret.status) ?? "?";
  const parts: string[] = [];
  for (const [key, value] of Object.entries(regret)) {
    if (key === "status" || key === "reason") continue;
    const n = num(value);
    if (n !== null) parts.push(`${key.replaceAll("_", " ")} ${Number.isInteger(n) ? String(n) : n.toFixed(3)}`);
    else if (typeof value === "string" && value !== "") parts.push(`${key.replaceAll("_", " ")} ${value}`);
  }
  const reason = str(regret.reason);
  return [status, ...(parts.length > 0 ? [parts.join(", ")] : []), ...(reason !== null ? [reason] : [])].join(" - ");
}

/** The gate in one line: accepted / rejected (rule), the reference, the rules failed, regret. */
export function gateText(gate: unknown): string {
  if (!isDict(gate) || Object.keys(gate).length === 0) return "No activation gate recorded.";
  const verdict = gate.accepted === true ? "accepted" : "rejected";
  const rule = str(gate.rule);
  const reference = isDict(gate.reference) ? (str(gate.reference.kind) ?? "?") : "?";
  const failed = Array.isArray(gate.rules_failed) ? gate.rules_failed.filter((r): r is string => typeof r === "string") : [];
  let out = `Gate ${verdict}${rule !== null ? ` (${rule})` : ""} against ${reference}`;
  if (failed.length > 0) out += `; failed ${failed.join(", ")}`;
  return `${out}. Regret: ${regretText(gate.regret)}.`;
}

/* ------------------------------------------------------------ predictions */

/** The terms present in the points, in `ML_TERM_ORDER` (unknown ones by name after). */
export function termsOf(points: readonly MlPredictionPointOut[]): string[] {
  const present = new Set(points.map((p) => p.term));
  const known: string[] = ML_TERM_ORDER.filter((t) => present.has(t));
  const rest = [...present].filter((t) => !(ML_TERM_ORDER as readonly string[]).includes(t)).sort();
  return [...known, ...rest];
}

/** The term the scatter opens on: the one with the most points (ties: `ML_TERM_ORDER`). */
export function busiestTerm(points: readonly MlPredictionPointOut[]): string | null {
  const counts = new Map<string, number>();
  for (const p of points) counts.set(p.term, (counts.get(p.term) ?? 0) + 1);
  let best: string | null = null;
  for (const t of termsOf(points)) if (best === null || (counts.get(t) ?? 0) > (counts.get(best) ?? 0)) best = t;
  return best;
}

export function unitOf(term: string): string {
  return PER_ITEM_TERMS.has(term) ? "s per item" : "s";
}

export interface ScatterPoint {
  /** Predicted (calibrated, or ML where it corrected the term). */
  x: number;
  /** Actual. */
  y: number;
  decisionId: number;
  nodeId: string;
  family: string | null;
}

export interface Scatter {
  term: string;
  /** Every point's calibrated prediction vs actual. */
  calibration: ScatterPoint[];
  /** The ML prediction vs actual, only where ML corrected the term. */
  ml: ScatterPoint[];
  /** Points of this term that cannot sit on log axes (a null or non-positive value). */
  dropped: number;
  /** Shared square log domain, so the y = x line is the diagonal; null without points. */
  domain: [number, number] | null;
}

const positive = (v: number | null | undefined): v is number => v != null && Number.isFinite(v) && v > 0;

/** The scatter of one term: calibration and ML points vs actual (filter the points by set first if wanted). */
export function scatterOf(points: readonly MlPredictionPointOut[], term: string): Scatter {
  const calibration: ScatterPoint[] = [];
  const ml: ScatterPoint[] = [];
  let dropped = 0;
  for (const p of points) {
    if (p.term !== term) continue;
    const base = { decisionId: p.decision_id, nodeId: p.node_id, family: p.family };
    const calOk = positive(p.calibrated_s) && positive(p.actual_s);
    const mlOk = p.used && positive(p.ml_s) && positive(p.actual_s);
    if (calOk) calibration.push({ x: p.calibrated_s as number, y: p.actual_s, ...base });
    if (mlOk) ml.push({ x: p.ml_s as number, y: p.actual_s, ...base });
    if (!calOk && !mlOk) dropped += 1;
  }
  const all = [...calibration, ...ml].flatMap((p) => [p.x, p.y]);
  const domain: [number, number] | null = all.length === 0 ? null : [Math.min(...all), Math.max(...all)];
  if (domain !== null && domain[0] === domain[1]) {
    domain[0] /= 2;
    domain[1] *= 2;
  }
  return { term, calibration, ml, dropped, domain };
}

export interface FallbackRow {
  /** `used`, a fallback reason, `no_model_set` (decision planned without a model) or `no_answer`. */
  key: string;
  label: string;
  count: number;
  share: number;
}

/** How often ML acted, and why not, over the listed points (optionally one term). */
export function fallbackRows(points: readonly MlPredictionPointOut[], term: string | null = null): FallbackRow[] {
  const counts = new Map<string, number>();
  let total = 0;
  for (const p of points) {
    if (term !== null && p.term !== term) continue;
    total += 1;
    const key = p.used ? "used" : p.ml_model_version == null ? "no_model_set" : (p.reason ?? "no_answer");
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const label = (key: string) =>
    key === "used"
      ? "ML used"
      : key === "no_model_set"
        ? "planned without a model set"
        : key === "no_answer"
          ? "not used (no reason recorded)"
          : `not used: ${reasonText(key)}`;
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([key, n]) => ({ key, label: label(key), count: n, share: total === 0 ? 0 : n / total }));
}

/** Seconds as the scatter shows them (`0.0123 s` / `12.3 s`), with the term's unit. */
export function secondsText(value: number | null | undefined, unit = "s"): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return `${String(Number(value.toPrecision(3)))} ${unit}`;
}
