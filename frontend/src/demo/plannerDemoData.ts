/**
 * DEMO PLANNER (localhost `?demo=1` only - see demo/install.ts). Every
 * route the Planner page reads or drives, built around the project's
 * measured results: the planner's estimate within 15 % of the measured time
 * (the active calibration's held-out error), ~6 ms per decision (the newest
 * evaluation's latency), 0 failed runs of the 432-run planner check, over the
 * demo laptops (asha-laptop, lab-pc-07, ravi-thinkpad, old-desktop, mini-pc).
 *
 * * Calibration: seven versions (five accepted, two rejected by the held-out
 *   gate), each with its own factors; `?version=N` reads any of them.
 * * Evaluation reports: a pass / fail / not-separable / too-few / leaked /
 *   failed mix over the demo benchmark campaigns. Run evaluation starts one in
 *   memory (one at a time - a second start is the server's 409); it finishes
 *   after ~18 s, or fails for an unknown campaign or held-out family.
 * * ML models: one active set, one retired (superseded), one rejected, and a
 *   retrain that is running when the demo opens (it ends in a rejected
 *   candidate). Train now / pin / unpin change the registry as the server
 *   would (409 for a rejected set or an unpinned unpin).
 * * Predicted vs actual: ~600 matched decisions with realistic error.
 *
 * Typed against the real API types. State lives in memory until a reload.
 * Delete the whole src/demo/ folder to remove.
 */
import type {
  CalibrationOut,
  CalibrationSummary,
  CalibrationUsedOut,
  CalibrationView,
  EvalReportListOut,
  EvalReportOut,
  EvalReportRecordOut,
  EvalReportSummaryOut,
  EvalStartOut,
  FactorOut,
  FamilyResultOut,
  LatencyOut,
  PairedComparisonOut,
  PolicyResultOut,
  VerdictOut,
  VerdictStatus,
} from "../pages/planner/plannerApi";
import type {
  MlMemberOut,
  MlModelsOut,
  MlPredictionPointOut,
  MlPredictionsOut,
  MlRetrainOut,
  MlRetrainStateOut,
  MlSetDetailOut,
  MlSetOut,
  MlTrainOut,
} from "../pages/planner/mlApi";

const T0 = Date.now();
const MIN = 60_000;
const H = 3_600_000;

const ASHA = "node_demo_asha";
const LAB = "node_demo_lab07";
const RAVI = "node_demo_ravi";
const OLD = "node_demo_old";
const MINI = "node_demo_mini";

/** A demo answer: the status and the JSON body. */
export interface DemoAnswer {
  status: number;
  body: unknown;
}

const ok = (body: unknown): DemoAnswer => ({ status: 200, body });

function refused(status: number, code: string, message: string, extra: Record<string, unknown> = {}): DemoAnswer {
  return { status, body: { error: { code, message, ...extra } } };
}

function iso(ms: number): string {
  return new Date(ms).toISOString();
}

function ago(hours: number): string {
  return iso(T0 - hours * H);
}

/** A small deterministic generator (mulberry32), so a reload shows the same numbers. */
function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 2 ** 32;
  };
}

/** A standard normal draw (Box-Muller). */
function normal(r: () => number): number {
  const u = Math.max(1e-9, r());
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * r());
}

function hex64(key: string): string {
  let out = "";
  let h = 2166136261;
  for (let round = 0; out.length < 64; round++) {
    for (const ch of `${key}#${String(round)}`) h = Math.imul(h ^ ch.charCodeAt(0), 16777619) >>> 0;
    out += h.toString(16).padStart(8, "0");
  }
  return out.slice(0, 64);
}

function sig(v: number, digits = 4): number {
  return Number(v.toPrecision(digits));
}

function round(v: number, digits: number): number {
  const k = 10 ** digits;
  return Math.round(v * k) / k;
}

/** The demo benchmark campaigns' ids (benchDemoData's `ulid("camp", tag)`). */
function campaign(tag: string): string {
  return `camp_01JDEMO${tag.toUpperCase().padStart(19, "0")}`;
}

/** The families each demo campaign measured (benchDemoData's groups). */
const CAMPAIGN_FAMILIES: Record<string, readonly string[]> = {
  [campaign("PLAN")]: ["build.compile", "cpu.primes", "io.hash", "ml.textgen"],
  [campaign("CPU4")]: ["cpu.primes"],
  [campaign("CPU2")]: ["cpu.primes"],
  [campaign("GPU3")]: ["ml.textgen"],
  [campaign("REAL")]: ["ml.train_real"],
  [campaign("WIN")]: ["build.compile"],
  [campaign("NIGHT")]: ["cpu.primes", "io.hash"],
  [campaign("WIFI")]: ["io.hash"],
  [campaign("QUEUE")]: ["ml.textgen"],
};

function familiesOf(ids: readonly string[]): string[] {
  return [...new Set(ids.flatMap((id) => CAMPAIGN_FAMILIES[id] ?? []))].sort();
}

/* ================================================================ calibration */

const TERM_PHASE = { transfer: "stage", env: "env", compute: "run", output: "collect" };
const NOT_CALIBRATED = {
  queue: "measured",
  items_fetch: "no_phase",
  chunk_overhead: "no_phase",
  tail: "no_phase",
  combine: "no_phase",
  risk: "derived",
};
const TERMS = ["transfer", "env", "compute", "output"] as const;
const LEVEL_ORDER: Record<string, number> = { global: 0, node: 1, node_family: 2 };

interface VersionSpec {
  version: number;
  createdH: number;
  status: "active" | "superseded" | "rejected";
  /** Mean |ln(predicted / actual)| on the held-out fifth, with this version / the active one. */
  errNew: number | null;
  errActive: number | null;
  reason: string | null;
  /** Matched runs the fit used. */
  runs: number;
}

const GATE_TOLERANCE = 0.05;

function gateReason(errNew: number, errActive: number): string {
  return `holdout_error ${errNew.toFixed(6)} > ${(1 + GATE_TOLERANCE).toFixed(2)} x active ${errActive.toFixed(6)}`;
}

/** Newest first: the active v12 (e^0.1388 = 1.149: within 15 % of measured), two rejected fits. */
const VERSIONS: readonly VersionSpec[] = [
  { version: 12, createdH: 2.2, status: "active", errNew: 0.1388, errActive: 0.1502, reason: null, runs: 1184 },
  { version: 11, createdH: 8.1, status: "rejected", errNew: 0.1712, errActive: 0.1502, reason: gateReason(0.1712, 0.1502), runs: 1096 },
  { version: 10, createdH: 14.4, status: "superseded", errNew: 0.1502, errActive: 0.1979, reason: null, runs: 1012 },
  { version: 9, createdH: 20.6, status: "superseded", errNew: 0.1979, errActive: 0.2318, reason: null, runs: 744 },
  { version: 8, createdH: 26.9, status: "rejected", errNew: null, errActive: 0.2318, reason: "holdout_unmeasurable", runs: 41 },
  { version: 7, createdH: 33.0, status: "superseded", errNew: 0.2318, errActive: 0.2645, reason: null, runs: 506 },
  { version: 6, createdH: 39.5, status: "superseded", errNew: 0.2645, errActive: 0.3391, reason: null, runs: 212 },
];

interface FactorSpec {
  term: (typeof TERMS)[number];
  level: "global" | "node" | "node_family";
  node?: string;
  family?: string;
  /** The v12 factor; older versions drift away from it. */
  factor: number;
  /** Share of the version's matched runs behind this key. */
  share: number;
}

const FACTORS: readonly FactorSpec[] = [
  { term: "transfer", level: "global", factor: 0.91, share: 0.62 },
  { term: "transfer", level: "node", node: RAVI, factor: 1.18, share: 0.14 },
  { term: "transfer", level: "node", node: OLD, factor: 1.33, share: 0.06 },
  { term: "transfer", level: "node_family", node: ASHA, family: "io.hash", factor: 0.86, share: 0.05 },
  { term: "env", level: "global", factor: 1.05, share: 0.71 },
  { term: "env", level: "node", node: OLD, factor: 1.27, share: 0.07 },
  { term: "compute", level: "global", factor: 1.12, share: 1 },
  { term: "compute", level: "node", node: ASHA, factor: 0.94, share: 0.29 },
  { term: "compute", level: "node", node: LAB, factor: 0.82, share: 0.31 },
  { term: "compute", level: "node", node: RAVI, factor: 1.21, share: 0.22 },
  { term: "compute", level: "node", node: OLD, factor: 1.46, share: 0.09 },
  { term: "compute", level: "node_family", node: LAB, family: "ml.textgen", factor: 0.71, share: 0.08 },
  { term: "compute", level: "node_family", node: ASHA, family: "ml.train_real", factor: 0.88, share: 0.04 },
  { term: "compute", level: "node_family", node: RAVI, family: "build.compile", factor: 1.17, share: 0.05 },
  { term: "compute", level: "node_family", node: OLD, family: "cpu.primes", factor: 1.52, share: 0.03 },
  { term: "output", level: "global", factor: 0.97, share: 0.58 },
];

const MIN_FACTOR_N = 5;

function factorsOf(v: VersionSpec): FactorOut[] {
  const r = rng(v.version * 7919);
  const age = 12 - v.version;
  const out: FactorOut[] = [];
  for (const f of FACTORS) {
    const n = Math.round(v.runs * f.share * (0.85 + 0.3 * r()));
    if (n < MIN_FACTOR_N) continue;
    // older fits sit further from the settled v12 factor (fewer runs, more noise)
    const drift = 1 + (age * 0.035 + 0.02) * normal(r);
    const factor = round(Math.max(0.3, f.factor * drift), 4);
    const median = round(1 + (factor - 1) * (1 + 3 / n), 4);
    out.push({ term: f.term, key_level: f.level, node_id: f.node ?? null, family: f.family ?? null, factor, n, median });
  }
  // a version with few matched runs (the rejected v8) keeps only its well-filled keys
  return out.sort(
    (a, b) =>
      TERMS.indexOf(a.term as (typeof TERMS)[number]) - TERMS.indexOf(b.term as (typeof TERMS)[number]) ||
      (LEVEL_ORDER[a.key_level] ?? 9) - (LEVEL_ORDER[b.key_level] ?? 9) ||
      (a.node_id ?? "").localeCompare(b.node_id ?? ""),
  );
}

function calibrationOut(v: VersionSpec): CalibrationOut {
  const factors = factorsOf(v);
  const families = [...new Set(factors.flatMap((f) => (f.family === null ? [] : [f.family])))].sort();
  return {
    version: v.version,
    created_at: ago(v.createdH),
    status: v.status,
    holdout_error_new: v.errNew,
    holdout_error_active: v.errActive,
    reason: v.reason,
    readable: true,
    factors,
    families: Object.fromEntries(families.map((f) => [`sha256:${hex64(`family:${f}`)}`, f])),
    fit_inputs: {
      matched_runs: v.runs,
      held_out_runs: Math.round(v.runs * 0.2),
      fitted_runs: v.runs - Math.round(v.runs * 0.2),
      min_predicted_s: 0.05,
    },
  };
}

function summaryOf(v: VersionSpec): CalibrationSummary {
  return {
    version: v.version,
    created_at: ago(v.createdH),
    status: v.status,
    holdout_error_new: v.errNew,
    holdout_error_active: v.errActive,
    reason: v.reason,
  };
}

const ACTIVE_VERSION = 12;

/** `GET /v1/planner/calibration[?version=N]`. */
export function demoCalibration(version: number | null): DemoAnswer {
  const spec = VERSIONS.find((v) => v.version === (version ?? ACTIVE_VERSION));
  if (spec === undefined) return refused(404, "not_found", `no calibration version ${String(version)}`);
  const view: CalibrationView = {
    in_use: { mode: "auto", pinned_version: null, in_use_version: ACTIVE_VERSION },
    active_version: ACTIVE_VERSION,
    calibration: calibrationOut(spec),
    term_phase: TERM_PHASE,
    not_calibrated: NOT_CALIBRATED,
    recent: VERSIONS.map(summaryOf),
  };
  return ok(view);
}

/* ================================================================ evaluation */

const JUDGED = "cost_model:calibrated:min_time";
const RULES = ["random", "round_robin", "least_loaded"] as const;

interface ReportSpec {
  id: number;
  createdH: number;
  campaigns: readonly string[];
  holdout: string | null;
  leaked?: boolean;
  /** The judged row's mean regret (%) and its CI. */
  regret: [number, number, number];
  /** Per rule policy: paired difference (rule - judged, % points) and its CI. */
  paired: Record<(typeof RULES)[number], [number, number, number]>;
  groups: number;
  evaluated: number;
  /** Counted groups of the judged row within 15 % of the oracle. */
  within: number;
  /** Decisions timed; the judged (>= 4 live nodes) part; p50 / p99 / max (ms). */
  latency: { decisions: number; judged: number; p50: number; p99: number; max: number } | null;
  /** The calibration version that was active (used by a leaked report). */
  activeVersion: number;
  records: number;
}

const SETTINGS = {
  regret_threshold_pct: 15,
  within_target_pct: 80,
  min_groups: 10,
  bootstrap_resamples: 10_000,
  bootstrap_seed: 20261119,
  ci_level: 0.95,
  criterion_policy: JUDGED,
};
const LATENCY_BUDGET_MS = 100;
const LATENCY_NODES = 4;
const MIN_DECISIONS = 30;

function policy(key: string, kind: PolicyResultOut["kind"], counted: number, mean: number | null, ci: [number, number] | null, within: number | null, msPerPct: number): PolicyResultOut {
  const costModel = kind === "cost_model";
  return {
    key,
    policy: key.split(":")[0] ?? key,
    kind,
    calibrated: costModel ? key.includes(":calibrated:") : null,
    objective: costModel ? (key.split(":")[2] ?? null) : null,
    counted_groups: mean === null ? 0 : counted,
    unmeasured_groups: key === "random" ? 2 : 0,
    no_replay_groups: 1,
    mean_regret_pct: mean,
    ci_pct: ci === null ? { low: null, high: null } : { low: ci[0], high: ci[1] },
    mean_regret_ms: mean === null ? null : Math.round(mean * msPerPct),
    within_threshold_pct: within,
    zero_regret_groups: mean === 0 ? counted : Math.max(0, Math.round(counted * (0.5 - (mean ?? 0) / 100))),
  };
}

function pct1(v: number): number {
  return round(v, 1);
}

function verdictsOf(s: ReportSpec): VerdictOut[] {
  const counted = s.evaluated;
  const share = counted === 0 ? 0 : (s.within * 100) / counted;
  const c1: VerdictOut =
    counted < SETTINGS.min_groups
      ? {
          criterion: 1,
          status: "too_few_groups",
          would_be: null,
          value: pct1(share),
          threshold: SETTINGS.within_target_pct,
          counted_groups: counted,
          detail: `${String(counted)} counted group(s) (needs >= ${String(SETTINGS.min_groups)})`,
        }
      : {
          criterion: 1,
          status: share >= SETTINGS.within_target_pct ? "PASS" : "FAIL",
          would_be: null,
          value: pct1(share),
          threshold: SETTINGS.within_target_pct,
          counted_groups: counted,
          detail: `${pct1(share).toFixed(1)} % of ${String(counted)} counted groups within ${String(SETTINGS.regret_threshold_pct)} % of the oracle (target >= ${String(SETTINGS.within_target_pct)} %)`,
        };
  const comparisons: PairedComparisonOut[] = RULES.map((key) => {
    const [mean, low, high] = s.paired[key];
    return { key, common_groups: key === "random" ? counted - 2 : counted, mean_diff_pct: mean, ci_low: low, ci_high: high, unmeasured_groups: key === "random" ? 2 : 0 };
  });
  const lowest = Math.min(...comparisons.map((c) => c.ci_low ?? 0));
  const below = comparisons.filter((c) => (c.ci_high ?? 0) < 0).map((c) => c.key);
  const touching = comparisons.filter((c) => (c.ci_low ?? 0) <= 0 && (c.ci_high ?? 0) >= 0).map((c) => c.key);
  let c2: VerdictOut;
  if (counted < SETTINGS.min_groups) {
    c2 = { criterion: 2, status: "too_few_groups", would_be: null, value: null, threshold: 0, counted_groups: counted, detail: `${String(counted)} common group(s) (needs >= ${String(SETTINGS.min_groups)})`, comparisons };
  } else {
    const status: VerdictStatus = below.length > 0 ? "FAIL" : touching.length > 0 ? "not_separable" : "PASS";
    const detail =
      status === "PASS"
        ? `${JUDGED} unmeasured in 0 group(s); difference CI above 0 for every rule policy`
        : status === "FAIL"
          ? `${below.join(", ")} picked better than ${JUDGED} (difference CI below 0)`
          : `${JUDGED} unmeasured in 0 group(s); difference CI reaches 0 for: ${touching.join(", ")}`;
    c2 = { criterion: 2, status, would_be: null, value: lowest, threshold: 0, counted_groups: counted, detail, comparisons };
  }
  const l = s.latency;
  const c3: VerdictOut =
    l === null || l.judged < MIN_DECISIONS
      ? {
          criterion: 3,
          status: "too_few_decisions",
          would_be: null,
          value: null,
          threshold: LATENCY_BUDGET_MS,
          counted_groups: l?.judged ?? 0,
          detail: `${String(l?.judged ?? 0)} decision(s) with a ${String(LATENCY_NODES)}-node snapshot (needs >= ${String(MIN_DECISIONS)})`,
        }
      : {
          criterion: 3,
          status: l.p99 < LATENCY_BUDGET_MS ? "PASS" : "FAIL",
          would_be: null,
          value: l.p99,
          threshold: LATENCY_BUDGET_MS,
          counted_groups: l.judged,
          detail: `p99 ${l.p99.toFixed(3)} ms over ${String(l.judged)} decision(s) with a ${String(LATENCY_NODES)}-node snapshot (budget < ${String(LATENCY_BUDGET_MS)} ms)`,
        };
  const all = [c1, c2, c3];
  return s.leaked === true ? all.map((v) => ({ ...v, status: "leaked", would_be: v.status })) : all;
}

function latencyOf(s: ReportSpec): LatencyOut | null {
  const l = s.latency;
  if (l === null) return null;
  const small = l.decisions - l.judged;
  return {
    decisions: l.decisions,
    without_latency: 0,
    p50_ms: l.p50,
    p99_ms: l.p99,
    max_ms: l.max,
    over_budget: 0,
    budget_ms: LATENCY_BUDGET_MS,
    node_count: LATENCY_NODES,
    min_decisions: MIN_DECISIONS,
    judged_on: "live_nodes",
    // five node rows (mini-pc is offline), four of them live while the campaigns ran
    by_node_count: { "5": { decisions: l.decisions, p99_ms: l.p99 } },
    by_live_node_count: {
      ...(small > 0 ? { "3": { decisions: small, p99_ms: round(l.p99 * 0.82, 2) } } : {}),
      "4": { decisions: l.judged, p99_ms: l.p99 },
    },
    judged: { decisions: l.judged, p99_ms: l.p99 },
  };
}

function reportOf(s: ReportSpec): EvalReportOut {
  const [m, lo, hi] = s.regret;
  const counted = s.evaluated;
  const share = counted === 0 ? null : pct1((s.within * 100) / counted);
  const msPer = 260;
  const rule = (key: (typeof RULES)[number], k: number): PolicyResultOut => {
    const [d, dl, dh] = s.paired[key];
    const mean = pct1(m + d);
    return policy(key, "rule", key === "random" ? counted - 2 : counted, mean, [pct1(mean - (d - dl) - (m - lo) * 0.4), pct1(mean + (dh - d) + (hi - m) * 0.4)], pct1(Math.max(0, (share ?? 0) - 18 - k * 14 - d)), msPer);
  };
  const families = familiesOf(s.campaigns);
  const shown = s.holdout === null ? families : [s.holdout];
  const familyRows: FamilyResultOut[] = shown.flatMap((family, i) => {
    const n = Math.max(1, Math.round(counted / shown.length) - (i % 2));
    const fm = pct1(m * (0.7 + 0.25 * i));
    return [
      { family, key: JUDGED, counted_groups: n, unmeasured_groups: 0, no_replay_groups: i === 0 ? 1 : 0, mean_regret_pct: fm, mean_regret_ms: Math.round(fm * msPer), within_threshold_pct: pct1(Math.min(100, (share ?? 0) + 4 - i * 5)) },
      { family, key: "least_loaded", counted_groups: n, unmeasured_groups: 0, no_replay_groups: i === 0 ? 1 : 0, mean_regret_pct: pct1(fm + s.paired.least_loaded[0]), mean_regret_ms: Math.round((fm + s.paired.least_loaded[0]) * msPer), within_threshold_pct: pct1(Math.max(0, (share ?? 0) - 22 - i * 4)) },
    ];
  });
  const calibrations: CalibrationUsedOut[] = shown.map((family, i) => ({
    family,
    source: s.leaked === true ? "active" : "refit",
    version: s.leaked === true ? s.activeVersion : null,
    fitted_runs: Math.round(s.records * 0.8) - i * 37,
    excluded_runs: Math.round(s.records / Math.max(2, families.length)) - i * 11,
    factors: 11 - (i % 3),
    digest: `sha256:${hex64(`cal:${String(s.id)}:${family}`)}`,
  }));
  const labels = { best: Math.round(s.groups * 0.71), tie: 0, insufficient: Math.max(1, s.groups - s.evaluated - 1), pending: 0 };
  labels.tie = s.groups - labels.best - labels.insufficient;
  return {
    eval_version: 2,
    campaign_ids: [...s.campaigns],
    holdout: s.holdout,
    leaked: s.leaked === true,
    settings: SETTINGS,
    counts: {
      groups: s.groups,
      evaluated_groups: s.evaluated,
      labels,
      no_replay_groups: 1,
      records: s.records,
      records_used: s.records - Math.round(s.records * 0.015),
      records_skipped: { no_replay: Math.round(s.records * 0.015) },
      non_finite_configs: 0,
    },
    verdicts: verdictsOf(s),
    policies: [
      rule("random", 2),
      rule("round_robin", 1),
      rule("least_loaded", 0),
      policy("cost_model:uncalibrated:min_time", "cost_model", counted, pct1(m * 2.3 + 1.4), [pct1(lo * 2.1 + 0.6), pct1(hi * 2.4 + 2.2)], pct1(Math.max(0, (share ?? 0) - 12)), msPer),
      policy(JUDGED, "cost_model", counted, m, [lo, hi], share, msPer),
      policy("cost_model:calibrated:prefer_local", "cost_model", counted, pct1(m * 3.4 + 6.2), [pct1(lo * 3 + 4.1), pct1(hi * 3.6 + 8.5)], pct1(Math.max(0, (share ?? 0) - 31)), msPer),
      policy("oracle", "oracle", counted, 0, [0, 0], 100, msPer),
    ],
    families: familyRows,
    calibrations,
    groups: [],
    latency: latencyOf(s),
    ml: null,
  };
}

const PAIRED_GOOD: ReportSpec["paired"] = {
  random: [38.4, 31.2, 45.9],
  round_robin: [29.7, 23.1, 36.0],
  least_loaded: [7.4, 3.9, 11.2],
};

const REPORT_SPECS: readonly ReportSpec[] = [
  {
    // the 432-run planner check with the CPU and GPU campaigns: everything passes, ~6 ms per decision
    id: 8,
    createdH: 1.1,
    campaigns: [campaign("PLAN"), campaign("CPU4"), campaign("GPU3")],
    holdout: null,
    regret: [3.1, 1.6, 4.9],
    paired: PAIRED_GOOD,
    groups: 48,
    evaluated: 44,
    within: 40,
    latency: { decisions: 432, judged: 388, p50: 6.1, p99: 9.4, max: 14.2 },
    activeVersion: 12,
    records: 1296,
  },
  {
    // text AI held out: the calibration never saw that family - criterion 1 fails, 2 cannot separate
    id: 7,
    createdH: 6.5,
    campaigns: [campaign("REAL"), campaign("GPU3")],
    holdout: "ml.textgen",
    regret: [9.8, 5.2, 15.1],
    paired: { random: [31.6, 22.4, 41.0], round_robin: [21.9, 13.8, 30.2], least_loaded: [2.1, -1.8, 6.0] },
    groups: 16,
    evaluated: 14,
    within: 10,
    latency: { decisions: 168, judged: 120, p50: 6.4, p99: 11.8, max: 17.5 },
    activeVersion: 12,
    records: 504,
  },
  {
    id: 5,
    createdH: 26,
    campaigns: [campaign("PLAN")],
    holdout: null,
    leaked: true,
    regret: [2.4, 1.1, 3.9],
    paired: PAIRED_GOOD,
    groups: 36,
    evaluated: 33,
    within: 31,
    latency: { decisions: 432, judged: 388, p50: 5.9, p99: 9.1, max: 13.6 },
    activeVersion: 9,
    records: 1296,
  },
  {
    // only the Windows build and the 2-laptop CPU split: too little to judge anything
    id: 4,
    createdH: 49,
    campaigns: [campaign("CPU2"), campaign("WIN")],
    holdout: "build.compile",
    regret: [6.2, 2.8, 10.4],
    paired: { random: [27.0, 14.1, 39.5], round_robin: [18.2, 7.9, 29.0], least_loaded: [4.4, -2.6, 11.0] },
    groups: 9,
    evaluated: 7,
    within: 6,
    latency: { decisions: 21, judged: 21, p50: 6.6, p99: 10.2, max: 12.9 },
    activeVersion: 9,
    records: 189,
  },
  {
    // an older calibration (v7): least_loaded still picked better on the CPU splits
    id: 3,
    createdH: 75,
    campaigns: [campaign("CPU4"), campaign("CPU2"), campaign("NIGHT")],
    holdout: null,
    regret: [11.6, 7.9, 15.8],
    paired: { random: [24.8, 16.0, 33.1], round_robin: [12.3, 5.6, 19.4], least_loaded: [-4.2, -7.9, -0.6] },
    groups: 15,
    evaluated: 13,
    within: 11,
    latency: { decisions: 205, judged: 163, p50: 7.2, p99: 13.9, max: 22.4 },
    activeVersion: 7,
    records: 615,
  },
];

interface LiveReport {
  id: number;
  createdMs: number;
  campaigns: string[];
  holdout: string | null;
  leaked: boolean;
  /** Until this time the report is `running`. */
  endsMs: number;
  /** Set when it ends without a result. */
  failed: { reason: string; detail: string } | null;
  spec: ReportSpec | null;
}

const RUN_MS = 18_000;
const FAIL_MS = 4_000;

const REPORTS: LiveReport[] = [
  ...REPORT_SPECS.map((s) => ({
    id: s.id,
    createdMs: T0 - s.createdH * H,
    campaigns: [...s.campaigns],
    holdout: s.holdout,
    leaked: s.leaked === true,
    endsMs: T0 - s.createdH * H + 9 * MIN,
    failed: null,
    spec: s,
  })),
  {
    id: 6,
    createdMs: T0 - 12 * H,
    campaigns: [campaign("WIN"), "camp_01JDEMO00000000000000LAB12"],
    holdout: null,
    leaked: false,
    endsMs: T0 - 12 * H + 2 * MIN,
    failed: { reason: "not_found", detail: "campaign camp_01JDEMO00000000000000LAB12 was deleted while the evaluation ran" },
    spec: null,
  },
];

function statusOf(r: LiveReport, now: number): EvalReportRecordOut["status"] {
  if (now < r.endsMs) return "running";
  return r.failed === null ? "done" : "failed";
}

function recordOf(r: LiveReport, now: number): EvalReportRecordOut {
  const status = statusOf(r, now);
  return {
    id: r.id,
    created_at: iso(r.createdMs),
    campaign_ids: r.campaigns,
    holdout: r.holdout,
    leaked: r.leaked,
    status,
    reason: status === "failed" ? (r.failed?.reason ?? null) : null,
    detail: status === "failed" ? (r.failed?.detail ?? null) : null,
    report: status === "done" && r.spec !== null ? reportOf(r.spec) : null,
  };
}

function summaryOfReport(r: LiveReport, now: number): EvalReportSummaryOut {
  const record = recordOf(r, now);
  return {
    id: r.id,
    created_at: record.created_at,
    campaign_ids: r.campaigns,
    holdout: r.holdout,
    leaked: r.leaked,
    eval_version: record.report?.eval_version ?? 0,
    evaluated_groups: record.report?.counts.evaluated_groups ?? 0,
    verdicts: record.report?.verdicts ?? [],
    status: record.status,
    reason: record.reason,
  };
}

/** `GET /v1/planner/eval-reports` - newest first. */
export function demoEvalReports(limit = 20, now = Date.now()): DemoAnswer {
  const list: EvalReportListOut = {
    reports: [...REPORTS].sort((a, b) => b.createdMs - a.createdMs).slice(0, limit).map((r) => summaryOfReport(r, now)),
  };
  return ok(list);
}

/** `GET /v1/planner/eval-reports/{id}`. */
export function demoEvalReport(id: number, now = Date.now()): DemoAnswer {
  const r = REPORTS.find((x) => x.id === id);
  if (r === undefined) return refused(404, "not_found", `no evaluation report ${String(id)}`, { reason: "unknown_eval_report" });
  return ok(recordOf(r, now));
}

const MAX_EVAL_CAMPAIGNS = 20;

/** A new report's numbers: the newest good report's shape, scaled to the chosen campaigns. */
function specFor(id: number, campaigns: readonly string[], holdout: string | null): ReportSpec {
  const base = REPORT_SPECS[0];
  if (base === undefined) throw new Error("demo reports");
  const fams = familiesOf(campaigns).length;
  const groups = Math.max(8, campaigns.length * 9 + fams * 3);
  const evaluated = groups - 3;
  const r = rng(id * 131);
  const m = pct1(2.6 + r() * 2.4 + (holdout === null ? 0 : 3.5));
  const decisions = campaigns.includes(campaign("PLAN")) ? 432 : 60 + campaigns.length * 48;
  return {
    ...base,
    id,
    createdH: 0,
    campaigns,
    holdout,
    regret: [m, pct1(m * 0.5), pct1(m * 1.6)],
    groups,
    evaluated,
    within: Math.round(evaluated * (holdout === null ? 0.89 : 0.78)),
    latency: { decisions, judged: Math.round(decisions * 0.9), p50: pct1(5.8 + r() * 0.6), p99: pct1(8.9 + r() * 1.5), max: pct1(13 + r() * 3) },
    records: decisions * 3,
  };
}

/** `POST /v1/planner/eval-reports` (admin): start an evaluation in the background. */
export function demoStartEval(rawBody: unknown, now = Date.now()): DemoAnswer {
  let body: unknown = rawBody;
  if (typeof rawBody === "string") {
    try {
      body = JSON.parse(rawBody) as unknown;
    } catch {
      body = null;
    }
  }
  const b = (typeof body === "object" && body !== null ? body : {}) as { campaign_ids?: unknown; holdout?: unknown };
  const ids = Array.isArray(b.campaign_ids) ? b.campaign_ids.filter((x): x is string => typeof x === "string" && x !== "") : [];
  if (ids.length === 0 || ids.length > MAX_EVAL_CAMPAIGNS) {
    return refused(422, "invalid_input", `campaign_ids: give 1 to ${String(MAX_EVAL_CAMPAIGNS)} campaign ids`);
  }
  const running = REPORTS.find((r) => statusOf(r, now) === "running");
  if (running !== undefined) {
    return refused(409, "conflict", "an evaluation is already running on this controller", { reason: "eval_already_running", report_id: running.id });
  }
  const holdout = typeof b.holdout === "string" && b.holdout !== "" ? b.holdout : null;
  const id = Math.max(...REPORTS.map((r) => r.id)) + 1;
  const unknown = ids.find((c) => CAMPAIGN_FAMILIES[c] === undefined);
  const families = familiesOf(ids);
  let failed: LiveReport["failed"] = null;
  if (unknown !== undefined) failed = { reason: "not_found", detail: `campaign ${unknown} does not exist` };
  else if (holdout !== null && !families.includes(holdout)) {
    failed = { reason: "invalid_input", detail: `held-out family ${holdout} has no group in these campaigns (they measured ${families.join(", ")})` };
  }
  REPORTS.push({
    id,
    createdMs: now,
    campaigns: ids,
    holdout,
    leaked: false,
    endsMs: now + (failed === null ? RUN_MS : FAIL_MS),
    failed,
    spec: failed === null ? specFor(id, ids, holdout) : null,
  });
  const out: EvalStartOut = { report_id: id, status: "running" };
  return { status: 202, body: out };
}

/* ================================================================ ML models */

const ML_TERMS = ["transfer", "env", "compute", "output", "lane_stage", "lane_env", "chunk_rate", "combine"] as const;
type MlTermName = (typeof ML_TERMS)[number];

interface TermFit {
  algorithm: "lightgbm" | "ridge" | "none";
  rows: number;
  families: number;
  groups: number;
  /** CV mean |ln| error, its SE, interval width (learned terms). */
  male?: number;
  se?: number;
  width?: number;
  /** Held-out slice: rows, model / calibration error, coverage, usable rate. */
  held?: { rows: number; model: number; calibration: number; coverage: number; usable: number; failed?: string[] };
}

function insufficient(f: TermFit): string {
  return `insufficient_data: ${String(f.rows)} rows, ${String(f.families)} families, ${String(f.groups)} groups (needs >= 30 rows, >= 2 families, >= 2 groups)`;
}

function membersOf(tag: string, fits: Record<MlTermName, TermFit>): MlMemberOut[] {
  return ML_TERMS.map((term) => {
    const f = fits[term];
    const learned = f.algorithm !== "none";
    return {
      term,
      algorithm: f.algorithm,
      version: learned ? `mem-${hex64(`${tag}:${term}`).slice(0, 8)}` : `none-${term}`,
      params: f.algorithm === "lightgbm" ? { num_leaves: 15, learning_rate: 0.05, n_estimators: 240 } : f.algorithm === "ridge" ? { alpha: 1.0 } : {},
      metrics: learned
        ? { rows: f.rows, families: f.families, groups: f.groups, selected: f.algorithm, inner_male: f.male, inner_se: f.se, interval_width: f.width }
        : { rows: f.rows, families: f.families, groups: f.groups },
      reason: learned ? null : insufficient(f),
      artefact_bytes: f.algorithm === "lightgbm" ? 184_320 + f.rows * 12 : f.algorithm === "ridge" ? 2_816 : null,
    };
  });
}

function gateOf(fits: Record<MlTermName, TermFit>, opts: { accepted: boolean; rule: string | null; regret: unknown }): Record<string, unknown> {
  const terms: Record<string, unknown> = {};
  for (const term of ML_TERMS) {
    const f = fits[term];
    if (f.algorithm === "none" || f.held === undefined) {
      terms[term] = { algorithm: "none", held_rows: Math.round(f.rows * 0.2) };
      continue;
    }
    terms[term] = {
      algorithm: f.algorithm,
      held_rows: f.held.rows,
      male_new: f.held.model,
      male_calibration: f.held.calibration,
      male_reference: f.held.calibration,
      coverage: f.held.coverage,
      usable_rate: f.held.usable,
      failed: f.held.failed ?? [],
    };
  }
  return {
    accepted: opts.accepted,
    rule: opts.rule,
    rules_failed: opts.rule === null ? [] : [opts.rule],
    reference: { kind: "calibration", set_version: null, unreadable_terms: {} },
    terms,
    regret: opts.regret,
  };
}

const NONE = (rows: number, families: number, groups: number): TermFit => ({ algorithm: "none", rows, families, groups });

const FITS_ACTIVE: Record<MlTermName, TermFit> = {
  transfer: { algorithm: "ridge", rows: 412, families: 5, groups: 61, male: 0.0841, se: 0.0062, width: 0.34, held: { rows: 82, model: 0.0802, calibration: 0.1194, coverage: 0.902, usable: 0.951 } },
  env: NONE(22, 2, 4),
  compute: { algorithm: "lightgbm", rows: 1268, families: 5, groups: 148, male: 0.0712, se: 0.0041, width: 0.29, held: { rows: 254, model: 0.0698, calibration: 0.1312, coverage: 0.912, usable: 0.874 } },
  output: { algorithm: "ridge", rows: 640, families: 5, groups: 92, male: 0.0933, se: 0.0071, width: 0.37, held: { rows: 128, model: 0.0911, calibration: 0.1086, coverage: 0.883, usable: 0.922 } },
  lane_stage: { algorithm: "ridge", rows: 214, families: 3, groups: 34, male: 0.1127, se: 0.0098, width: 0.44, held: { rows: 43, model: 0.1094, calibration: 0.1421, coverage: 0.861, usable: 0.837 } },
  lane_env: NONE(18, 2, 3),
  chunk_rate: { algorithm: "lightgbm", rows: 596, families: 3, groups: 41, male: 0.0659, se: 0.0052, width: 0.27, held: { rows: 119, model: 0.0631, calibration: 0.1188, coverage: 0.924, usable: 0.899 } },
  combine: NONE(11, 1, 2),
};

const FITS_OLD: Record<MlTermName, TermFit> = {
  transfer: NONE(196, 3, 28),
  env: NONE(9, 1, 2),
  compute: { algorithm: "lightgbm", rows: 604, families: 4, groups: 71, male: 0.0954, se: 0.0067, width: 0.38, held: { rows: 121, model: 0.0921, calibration: 0.1618, coverage: 0.874, usable: 0.812 } },
  output: { algorithm: "ridge", rows: 288, families: 4, groups: 43, male: 0.1102, se: 0.0091, width: 0.45, held: { rows: 58, model: 0.1066, calibration: 0.1249, coverage: 0.845, usable: 0.861 } },
  lane_stage: NONE(27, 2, 5),
  lane_env: NONE(6, 1, 1),
  chunk_rate: { algorithm: "ridge", rows: 233, families: 2, groups: 17, male: 0.0918, se: 0.0089, width: 0.36, held: { rows: 47, model: 0.0897, calibration: 0.1402, coverage: 0.851, usable: 0.844 } },
  combine: NONE(4, 1, 1),
};

const FITS_REJECTED: Record<MlTermName, TermFit> = {
  ...FITS_ACTIVE,
  compute: { algorithm: "lightgbm", rows: 1190, families: 5, groups: 139, male: 0.0688, se: 0.0044, width: 0.28, held: { rows: 238, model: 0.0679, calibration: 0.1324, coverage: 0.908, usable: 0.881 } },
  chunk_rate: { algorithm: "lightgbm", rows: 571, families: 3, groups: 39, male: 0.0702, se: 0.0057, width: 0.3, held: { rows: 114, model: 0.0694, calibration: 0.1193, coverage: 0.917, usable: 0.889 } },
};

const FITS_CANDIDATE: Record<MlTermName, TermFit> = {
  ...FITS_ACTIVE,
  compute: { algorithm: "lightgbm", rows: 1302, families: 5, groups: 151, male: 0.0745, se: 0.0049, width: 0.21, held: { rows: 260, model: 0.0721, calibration: 0.1301, coverage: 0.712, usable: 0.903, failed: ["coverage"] } },
};

const FITS_NEXT: Record<MlTermName, TermFit> = {
  ...FITS_ACTIVE,
  compute: { algorithm: "lightgbm", rows: 1331, families: 5, groups: 154, male: 0.0669, se: 0.0039, width: 0.28, held: { rows: 266, model: 0.0662, calibration: 0.1297, coverage: 0.917, usable: 0.889 } },
  lane_env: { algorithm: "ridge", rows: 46, families: 2, groups: 8, male: 0.1388, se: 0.0154, width: 0.52, held: { rows: 9, model: 0.1342, calibration: 0.1503, coverage: 0.834, usable: 0.778 } },
};

interface LiveSet {
  version: string;
  createdMs: number;
  changedMs: number;
  status: string;
  reason: string | null;
  digest: string;
  members: MlMemberOut[];
  gate: Record<string, unknown>;
  rows: number;
}

function setVersion(tag: string): string {
  return `sha256:${hex64(`ml-set:${tag}`)}`;
}

function liveSet(tag: string, createdMs: number, status: string, reason: string | null, fits: Record<MlTermName, TermFit>, gate: Record<string, unknown>): LiveSet {
  return {
    version: setVersion(tag),
    createdMs,
    changedMs: createdMs,
    status,
    reason,
    digest: `sha256:${hex64(`dataset:${tag}`)}`,
    members: membersOf(tag, fits),
    gate,
    rows: fits.compute.rows,
  };
}

const SET_ACTIVE = setVersion("active");
const SET_OLD = setVersion("retired");

const SETS: LiveSet[] = [
  liveSet("rejected", T0 - 9 * H, "rejected", "regret_worse: mean regret 3.4 % > current 2.6 % x 1.05", FITS_REJECTED, gateOf(FITS_REJECTED, { accepted: false, rule: "regret_worse", regret: { status: "rejected", groups: 41, candidate_mean_pct: 3.4, current_mean_pct: 2.6, reason: "candidate 3.4 % > current 2.6 % x 1.05" } })),
  liveSet("active", T0 - 26 * H, "active", null, FITS_ACTIVE, gateOf(FITS_ACTIVE, { accepted: true, rule: null, regret: { status: "passed", groups: 44, candidate_mean_pct: 2.6, current_mean_pct: 3.1 } })),
  liveSet("retired", T0 - 98 * H, "superseded", null, FITS_OLD, gateOf(FITS_OLD, { accepted: true, rule: null, regret: "pending_g4" })),
];

const RETRAIN_INTERVAL_S = 21_600;
const RETRAIN_MS = 15_000;

interface Retrain {
  running: { sinceMs: number; endsMs: number; trigger: string; outcome: "candidate" | "accepted" } | null;
  last: MlRetrainOut | null;
  retrains: number;
  failures: number;
}

/** A retrain is running when the demo opens: it ends in a rejected candidate (coverage). */
const RETRAIN: Retrain = {
  running: { sinceMs: T0 - 50_000, endsMs: T0 + 40_000, trigger: "interval", outcome: "candidate" },
  last: {
    trigger: "interval",
    started_at: iso(T0 - 9 * H - 4 * MIN),
    finished_at: iso(T0 - 9 * H),
    created_set: setVersion("rejected"),
    superseded_set: null,
    status: "rejected",
    reason: "regret_worse: mean regret 3.4 % > current 2.6 % x 1.05",
    rows: FITS_REJECTED.compute.rows,
    seconds: { dataset: 11.4, fit: 162.8, gate: 48.1 },
    error: null,
    detail: null,
    skipped: null,
  },
  retrains: 14,
  failures: 1,
};

let pinned: string | null = null;

/** Finish a retrain whose time has come: add its set (as the server's gate decided). */
function settle(now: number): void {
  const run = RETRAIN.running;
  if (run === null || now < run.endsMs) return;
  RETRAIN.running = null;
  RETRAIN.retrains += 1;
  const tag = `train-${String(RETRAIN.retrains)}`;
  let created: LiveSet;
  let superseded: string | null = null;
  if (run.outcome === "candidate") {
    const reason = "coverage: compute: held-out coverage 0.71 < 0.80";
    created = liveSet(tag, run.endsMs, "rejected", reason, FITS_CANDIDATE, gateOf(FITS_CANDIDATE, { accepted: false, rule: "coverage", regret: { status: "skipped", reason: "the term gate failed first" } }));
  } else {
    const current = SETS.find((s) => s.status === "active");
    if (current !== undefined) {
      current.status = "superseded";
      current.changedMs = run.endsMs;
      superseded = current.version;
    }
    created = liveSet(tag, run.endsMs, "active", null, FITS_NEXT, gateOf(FITS_NEXT, { accepted: true, rule: null, regret: { status: "passed", groups: 46, candidate_mean_pct: 2.3, current_mean_pct: 2.6 } }));
  }
  SETS.unshift(created);
  RETRAIN.last = {
    trigger: run.trigger,
    started_at: iso(run.sinceMs),
    finished_at: iso(run.endsMs),
    created_set: created.version,
    superseded_set: superseded,
    status: created.status,
    reason: created.reason,
    rows: created.rows,
    seconds: { dataset: 9.8, fit: 4.1, gate: 1.2 },
    error: null,
    detail: null,
    skipped: null,
  };
}

function setOut(s: LiveSet): MlSetOut {
  return {
    set_version: s.version,
    created_at: iso(s.createdMs),
    status_changed_at: iso(s.changedMs),
    status: s.status,
    reason: s.reason,
    dataset_digest: s.digest,
    feature_version: 3,
    members: s.members,
  };
}

function detailOut(s: LiveSet): MlSetDetailOut {
  return {
    ...setOut(s),
    gate: s.gate,
    inputs: { rows: s.rows, families: 5, feature_version: 3, folds: "leave-one-family-out", seed: 20261119 },
  };
}

function retrainState(): MlRetrainStateOut {
  const run = RETRAIN.running;
  return {
    available: true,
    missing: [],
    running: run !== null,
    running_since: run === null ? null : iso(run.sinceMs),
    retrains: RETRAIN.retrains,
    failures: RETRAIN.failures,
    last: RETRAIN.last,
    interval_s: RETRAIN_INTERVAL_S,
  };
}

function serving(): string | null {
  return pinned ?? SETS.find((s) => s.status === "active")?.version ?? null;
}

/** `GET /v1/planner/ml/models?limit=`. */
export function demoMlModels(limit = 20, now = Date.now()): DemoAnswer {
  settle(now);
  const set = serving();
  const out: MlModelsOut = {
    in_use: {
      mode: "auto",
      pinned_config: null,
      kind: set === null ? null : pinned !== null ? "pinned" : "active",
      in_use_set: set,
      unavailable: null,
      ml_predict_last_ms: 0.84,
      ml_predictor_errors_total: 0,
    },
    serving_set: set,
    sets: SETS.slice(0, limit).map(setOut),
    retrain: retrainState(),
  };
  return ok(out);
}

/** `GET /v1/planner/ml/models/{set}`. */
export function demoMlSet(version: string, now = Date.now()): DemoAnswer {
  settle(now);
  const s = SETS.find((x) => x.version === version);
  if (s === undefined) return refused(404, "not_found", `no model set ${version}`);
  return ok(detailOut(s));
}

/** `GET /v1/planner/ml/train`. */
export function demoMlTrainState(now = Date.now()): DemoAnswer {
  settle(now);
  return ok(retrainState());
}

/** `POST /v1/planner/ml/train` (admin): start a retrain; a running one is not started twice. */
export function demoMlTrain(now = Date.now()): DemoAnswer {
  settle(now);
  if (RETRAIN.running !== null) {
    const out: MlTrainOut = { started: false, retrain: retrainState() };
    return ok(out);
  }
  RETRAIN.running = { sinceMs: now, endsMs: now + RETRAIN_MS, trigger: "manual", outcome: "accepted" };
  const out: MlTrainOut = { started: true, retrain: retrainState() };
  return ok(out);
}

/** `POST /v1/planner/ml/models/{set}/pin|unpin` (admin). */
export function demoMlPin(version: string, pin: boolean, now = Date.now()): DemoAnswer {
  settle(now);
  const s = SETS.find((x) => x.version === version);
  if (s === undefined) return refused(404, "not_found", `no model set ${version}`);
  if (pin) {
    if (s.status === "rejected") {
      return refused(409, "conflict", `model set ${version} was rejected by the activation gate (${s.reason ?? "?"}); pin an active or superseded set`);
    }
    if (s.status !== "pinned") {
      for (const other of SETS) {
        if (other.status === "pinned") {
          other.status = "superseded";
          other.changedMs = now;
        }
      }
      s.status = "pinned";
      s.changedMs = now;
    }
    pinned = s.version;
    return ok(detailOut(s));
  }
  if (s.status !== "pinned") return refused(409, "conflict", `model set ${version} is not pinned`);
  const active = SETS.some((x) => x.status === "active");
  s.status = active ? "superseded" : "active";
  s.changedMs = now;
  pinned = null;
  return ok(detailOut(s));
}

/* ================================================================ predictions */

const DECISIONS = 612;
const PREDICTION_LIMITS = [500, 2000];

interface FamilyShape {
  family: string;
  /** Typical actual compute seconds (log-uniform range). */
  compute: [number, number];
  split: boolean;
}

const FAMILY_SHAPES: readonly FamilyShape[] = [
  { family: "cpu.primes", compute: [14, 62], split: true },
  { family: "io.hash", compute: [6, 41], split: true },
  { family: "ml.textgen", compute: [28, 140], split: false },
  { family: "ml.train_real", compute: [85, 310], split: false },
  { family: "build.compile", compute: [48, 164], split: true },
];

const NODE_WEIGHTS: readonly [string, number][] = [
  [LAB, 0.32],
  [ASHA, 0.3],
  [RAVI, 0.24],
  [OLD, 0.1],
  [MINI, 0.04],
];

function pick<T>(r: () => number, items: readonly [T, number][]): T {
  let x = r();
  for (const [item, w] of items) {
    x -= w;
    if (x <= 0) return item;
  }
  const last = items.at(-1);
  if (last === undefined) throw new Error("empty");
  return last[0];
}

function logUniform(r: () => number, [lo, hi]: [number, number]): number {
  return Math.exp(Math.log(lo) + r() * (Math.log(hi) - Math.log(lo)));
}

/** Why ML was not used for a learned term (the dispatcher's fallback reasons). */
const FALLBACK: readonly [string, number][] = [
  ["interval_too_wide", 0.45],
  ["ood", 0.35],
  ["below_min_predicted", 0.2],
];

function memberAlgorithm(set: string | null, term: MlTermName): string {
  if (set === null) return "none";
  const s = SETS.find((x) => x.version === set);
  return s?.members.find((m) => m.term === term)?.algorithm ?? "none";
}

/** Every matched decision's points, newest first (built once: the decisions are history). */
const ALL_POINTS: { decision: number; points: MlPredictionPointOut[] }[] = (() => {
  const r = rng(4320);
  const out: { decision: number; points: MlPredictionPointOut[] }[] = [];
  for (let k = 0; k < DECISIONS; k++) {
    const decision = 9_840 - k * 3 - Math.floor(r() * 3);
    // decisions after the active set went live used it; older ones the retired set; the oldest none
    const set = k < 380 ? SET_ACTIVE : k < 560 ? SET_OLD : null;
    const node = k < 380 ? pick(r, NODE_WEIGHTS.slice(0, 4)) : pick(r, NODE_WEIGHTS);
    const shape = FAMILY_SHAPES[Math.floor(r() * FAMILY_SHAPES.length)] ?? FAMILY_SHAPES[0];
    if (shape === undefined) continue;
    const slow = node === OLD ? 1.45 : node === RAVI ? 1.2 : node === LAB ? 0.8 : 1;
    const terms: { term: MlTermName; actual: number }[] = [{ term: "compute", actual: logUniform(r, shape.compute) * slow }];
    if (r() < 0.62) terms.push({ term: "transfer", actual: logUniform(r, [0.35, 6.5]) * (node === RAVI ? 1.3 : 1) });
    if (r() < 0.4) terms.push({ term: "env", actual: logUniform(r, [0.8, 12]) });
    if (r() < 0.34) terms.push({ term: "output", actual: logUniform(r, [0.2, 3.2]) });
    if (shape.split && r() < 0.45) {
      terms.push({ term: "chunk_rate", actual: logUniform(r, [0.012, 0.21]) * slow });
      terms.push({ term: "lane_stage", actual: logUniform(r, [0.3, 4.2]) });
    }
    const points = terms.map(({ term, actual }): MlPredictionPointOut => {
      const algo = memberAlgorithm(set, term);
      const learned = algo !== "none";
      const fallback = learned && r() < 0.14 ? pick(r, FALLBACK) : null;
      const used = learned && fallback === null;
      // the calibrated estimate: mean |ln error| ~ 0.14 (within 15 % of measured); ML tighter
      const calibrated = actual * Math.exp(0.175 * normal(r));
      const ml = learned ? actual * Math.exp(0.088 * normal(r)) : null;
      return {
        decision_id: decision,
        term,
        node_id: node,
        family: shape.family,
        analytical_s: sig(actual * Math.exp(0.16 + 0.31 * normal(r))),
        calibrated_s: sig(calibrated),
        ml_s: ml === null ? null : sig(ml),
        used,
        reason: used ? null : learned ? fallback : set === null ? null : "no_model",
        actual_s: sig(actual),
        ml_model_version: set,
      };
    });
    out.push({ decision, points });
  }
  return out;
})();

/** `GET /v1/planner/ml/predictions?limit=` (admin): the newest matched decisions' points. */
export function demoMlPredictions(rawLimit: number): DemoAnswer {
  const limit = PREDICTION_LIMITS.includes(rawLimit) ? rawLimit : 500;
  const read = ALL_POINTS.slice(0, limit);
  const out: MlPredictionsOut = {
    limit,
    decisions_read: read.length,
    term: null,
    set: null,
    calibration_versions_cap: 16,
    calibration_versions_skipped: 0,
    calibration_skipped_points: 0,
    points: read.flatMap((d) => d.points),
  };
  return ok(out);
}

/* ================================================================ routing */

const SET_PATH = /^\/v1\/planner\/ml\/models\/(sha256:[0-9a-f]{64})(?:\/(pin|unpin))?$/;
const REPORT_PATH = /^\/v1\/planner\/eval-reports\/(\d+)$/;

/** True while something on the Planner page moves on its own (a running evaluation or retrain). */
export function demoPlannerMoving(now = Date.now()): boolean {
  return RETRAIN.running !== null || REPORTS.some((r) => statusOf(r, now) === "running");
}

/** A Planner route's demo answer, or undefined (not a planner route: the real controller answers). */
export function demoPlannerRoute(method: string, url: URL, body?: unknown): DemoAnswer | undefined {
  const p = url.pathname;
  if (!p.startsWith("/v1/planner/")) return undefined;
  const num = (key: string, fallback: number) => {
    const v = Number(url.searchParams.get(key) ?? String(fallback));
    return Number.isFinite(v) && v > 0 ? v : fallback;
  };
  if (method === "GET") {
    if (p === "/v1/planner/calibration") {
      const v = url.searchParams.get("version");
      return demoCalibration(v === null ? null : Number(v));
    }
    if (p === "/v1/planner/eval-reports") return demoEvalReports(num("limit", 20));
    const r = REPORT_PATH.exec(p);
    if (r !== null) return demoEvalReport(Number(r[1]));
    if (p === "/v1/planner/ml/models") return demoMlModels(num("limit", 20));
    if (p === "/v1/planner/ml/predictions") return demoMlPredictions(num("limit", 500));
    if (p === "/v1/planner/ml/train") return demoMlTrainState();
    const s = SET_PATH.exec(p);
    if (s !== null && s[2] === undefined) return demoMlSet(s[1] ?? "");
    return undefined;
  }
  if (method === "POST") {
    if (p === "/v1/planner/eval-reports") return demoStartEval(body);
    if (p === "/v1/planner/ml/train") return demoMlTrain();
    const s = SET_PATH.exec(p);
    if (s !== null && s[2] !== undefined) return demoMlPin(s[1] ?? "", s[2] === "pin");
  }
  return undefined;
}
