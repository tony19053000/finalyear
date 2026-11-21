/**
 * DEMO BENCHMARKS (localhost `?demo=1` only - see demo/install.ts). Nine
 * benchmark campaigns over the demo laptops, built around the project's
 * measured results: 1.97x on 2 laptops and 3.21x on 4 (CPU), 3.19x on 3 GPUs
 * (text AI, 97 % of ideal), 2.10x on real-data AI over 2 GPUs, 3.4x on Windows
 * (164 s -> 48 s), a 432-run planner check with 0 failed runs (planner
 * estimate within 15 %, ~6 ms per decision). One campaign runs (a run ends
 * every few seconds, then it starts over), one waits to be started, one was
 * cancelled; the controls work on all of them (in memory, until a reload).
 *
 * Every answer is typed against the real API types and derived from one
 * model per campaign (runs -> counts -> labels -> quality -> status), so the
 * numbers agree across tabs. Delete the whole src/demo/ folder to remove.
 */
import type {
  CampaignControlOut,
  CampaignDetail,
  CampaignList,
  CampaignOut,
  CampaignState,
  CampaignStatus,
  CampaignVerb,
  ConfigLabel,
  GroupLabel,
  LabelsOut,
  MetricStats,
  RunCounts,
  RunList,
  RunOut,
  RunState,
} from "../pages/benchmarks/benchApi";
import type { QualityConfig, QualityReport } from "../pages/benchmarks/qualityApi";

const ASHA = "node_demo_asha";
const LAB = "node_demo_lab07";
const RAVI = "node_demo_ravi";
const OLD = "node_demo_old";
const MINI = "node_demo_mini";

const HOST: Record<string, string> = {
  [ASHA]: "asha-laptop",
  [LAB]: "lab-pc-07",
  [RAVI]: "ravi-thinkpad",
  [OLD]: "old-desktop",
  [MINI]: "mini-pc",
};

/** A live campaign finishes one run this often. */
const STEP_MS = 5_000;
const CV_THRESHOLD = 0.1;
const T0 = Date.now();
const H = 3_600_000;

type Strategy = RunOut["strategy"];

interface Cfg {
  strategy: Strategy;
  node: string;
  set?: readonly string[];
}

interface Group {
  family: string;
  params: Record<string, number>;
  condition?: string;
  /** Median total_ms per configuration (same order as the campaign's `cfgs`). */
  ms: readonly number[];
  /** Coefficient of variation per configuration (one number: all of them). */
  cv?: number | readonly number[];
  /** The configuration the planner preferred (default: the fastest). */
  planner?: number;
}

interface Spec {
  tag: string;
  name: string;
  /** Hours before the demo opened. */
  createdH: number;
  startedH: number | null;
  seed: number;
  session: string | null;
  origin: string;
  nodes: readonly string[];
  cfgs: readonly Cfg[];
  groups: readonly Group[];
  reps: number;
  state: CampaignState;
  finished: CampaignStatus["finished_reason"];
  runnerOwned: boolean;
  /** A finished campaign's runs that ended (cancelled: the rest were skipped). */
  ran?: number;
  /** Run seqs that failed (infra retries used up). */
  failed?: readonly number[];
  /** Run seqs whose first try hit an infrastructure failure, then succeeded. */
  retried?: readonly number[];
  preempted?: number;
  conditionFailures?: Record<string, number>;
  missing?: { group: number; cfg: number; field: string; count: number };
  budget: { max_runs: number; max_hours: number };
  notes: readonly string[];
  /** A running campaign: runs ended when the demo opened; `loop` starts it over at the end. */
  live?: { base: number; loop: boolean };
  createdBy?: string;
}

/* ------------------------------------------------------------ helpers */

function hash(text: string): number {
  let h = 2166136261;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** A stable pseudo-random number in [0, 1). */
function rand(key: string): number {
  return hash(key) / 2 ** 32;
}

/** A ULID-shaped id: `prefix_01JDEMO…TAG`. */
function ulid(prefix: string, tag: string): string {
  return `${prefix}_01JDEMO${tag.toUpperCase().padStart(19, "0")}`;
}

function iso(ms: number): string {
  return new Date(ms).toISOString();
}

function round1(v: number): number {
  return Math.round(v * 10) / 10;
}

function median(values: readonly number[]): number {
  const s = [...values].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length === 0 ? 0 : s.length % 2 === 1 ? (s[m] ?? 0) : ((s[m - 1] ?? 0) + (s[m] ?? 0)) / 2;
}

function cvOf(group: Group, c: number): number {
  const cv = group.cv ?? 0.03;
  return typeof cv === "number" ? cv : (cv[c] ?? 0.03);
}

/** The server's per-config statistics over `n` repetitions (median + a CI around it). */
function stats(m: number, cv: number, n: number, key: string): MetricStats {
  if (n < 2) return { n, median: m, mean: m, std: null, cv: null, ci_low: m, ci_high: m, ci_coverage: 0, outliers: 0 };
  const std = m * cv;
  const half = (2.1 * std) / Math.sqrt(n);
  return {
    n,
    median: m,
    mean: m * (1 + (rand(`${key}:mean`) - 0.5) * cv * 0.4),
    std,
    cv,
    ci_low: m - half,
    ci_high: m + half,
    ci_coverage: n >= 6 ? 0.938 : n >= 4 ? 0.875 : 0.75,
    outliers: rand(`${key}:out`) < 0.12 ? 1 : 0,
  };
}

const BYTES: Record<string, number> = {
  "cpu.primes": 2.1e6,
  "build.compile": 96e6,
  "ml.textgen": 42e6,
  "ml.train_real": 610e6,
  "io.hash": 1.05e6,
};

/* ------------------------------------------------------------ the campaigns */

const SPLIT4 = [ASHA, LAB, RAVI, OLD] as const;

const SPECS: Spec[] = [
  {
    tag: "QUEUE",
    name: "gpu-memory-sweep",
    createdH: 0.35,
    startedH: null,
    seed: 5150,
    session: "lab evening",
    origin: ASHA,
    nodes: [ASHA, LAB, MINI],
    cfgs: [
      { strategy: "local", node: ASHA },
      { strategy: "mode1", node: LAB },
      { strategy: "mode3", node: ASHA, set: [ASHA, LAB, MINI] },
    ],
    groups: [8, 16, 32, 64].map((batch) => ({
      family: "ml.textgen",
      params: { batch },
      ms: [batch * 3_400, batch * 2_150, batch * 1_120],
    })),
    reps: 8,
    state: "active",
    finished: null,
    runnerOwned: false,
    budget: { max_runs: 96, max_hours: 4 },
    notes: ["How batch size changes the GPU split (VRAM 4-12 GB). Waiting for the evening slot."],
  },
  {
    tag: "NIGHT",
    name: "overnight-mixed-sweep",
    createdH: 0.9,
    startedH: 0.75,
    seed: 8086,
    session: "office wifi",
    origin: ASHA,
    nodes: [ASHA, LAB, RAVI],
    cfgs: [
      { strategy: "local", node: ASHA },
      { strategy: "mode1", node: LAB },
      { strategy: "mode1", node: RAVI },
      { strategy: "mode3", node: ASHA, set: [ASHA, LAB, RAVI] },
    ],
    groups: [
      { family: "cpu.primes", params: { limit: 10_000_000 }, ms: [690, 560, 720, 470] },
      { family: "cpu.primes", params: { limit: 100_000_000 }, ms: [6_420, 4_300, 6_880, 2_890] },
      { family: "cpu.primes", params: { limit: 1_000_000_000 }, ms: [61_400, 40_900, 65_200, 25_600] },
      { family: "io.hash", params: { size_mb: 256 }, ms: [3_100, 5_400, 6_100, 4_700], cv: 0.05 },
      { family: "io.hash", params: { size_mb: 1024 }, ms: [12_300, 19_800, 22_600, 14_100], cv: 0.05 },
      { family: "io.hash", params: { size_mb: 4096 }, ms: [49_100, 74_300, 86_900, 41_800], cv: 0.06 },
    ],
    reps: 5,
    state: "active",
    finished: null,
    runnerOwned: true,
    retried: [9, 37],
    budget: { max_runs: 120, max_hours: 8 },
    notes: ["CPU and disk-bound work side by side: where does splitting stop paying off?"],
    live: { base: 46, loop: true },
  },
  {
    tag: "PLAN",
    name: "planner-check-432",
    createdH: 30,
    startedH: 29.5,
    seed: 4320,
    session: "lab ethernet",
    origin: ASHA,
    nodes: [...SPLIT4],
    cfgs: [
      { strategy: "local", node: ASHA },
      { strategy: "mode1", node: LAB },
      { strategy: "mode1", node: RAVI },
      { strategy: "mode3", node: ASHA, set: SPLIT4 },
    ],
    groups: [
      { family: "build.compile", params: { targets: 4 }, ms: [11_200, 14_900, 16_300, 13_800] },
      { family: "build.compile", params: { targets: 8 }, ms: [21_900, 19_600, 27_400, 15_100] },
      { family: "build.compile", params: { targets: 16 }, ms: [43_800, 34_100, 52_200, 19_700] },
      { family: "cpu.primes", params: { limit: 1_000_000 }, ms: [74, 182, 196, 310] },
      { family: "cpu.primes", params: { limit: 10_000_000 }, ms: [690, 610, 742, 402] },
      { family: "cpu.primes", params: { limit: 100_000_000 }, ms: [6_420, 4_310, 6_890, 2_290] },
      { family: "io.hash", params: { size_mb: 64 }, ms: [790, 2_310, 2_520, 3_100], cv: 0.04 },
      { family: "io.hash", params: { size_mb: 256 }, ms: [3_100, 5_380, 6_050, 4_690], cv: 0.04, planner: 1 },
      { family: "io.hash", params: { size_mb: 1024 }, ms: [12_300, 19_700, 22_400, 10_900], cv: 0.035 },
      { family: "ml.textgen", params: { prompts: 128 }, ms: [6_900, 5_200, 18_800, 6_100] },
      { family: "ml.textgen", params: { prompts: 512 }, ms: [27_400, 17_900, 74_100, 13_600] },
      { family: "ml.textgen", params: { prompts: 2048 }, ms: [104_800, 66_100, 291_000, 41_900] },
    ],
    reps: 9,
    state: "done",
    finished: "complete",
    runnerOwned: true,
    retried: [41, 188, 377],
    budget: { max_runs: 432, max_hours: 12 },
    notes: [
      "Does the planner pick the fastest option? 12 workloads x 4 placements x 9 repetitions.",
      "Result: 432 runs, 0 failed; planner estimate within 15 % of the measured time;",
      "~6 ms per planning decision.",
    ],
  },
  {
    tag: "WIFI",
    name: "wifi-vs-ethernet",
    createdH: 26,
    startedH: 25.8,
    seed: 2412,
    session: "home wifi",
    origin: ASHA,
    nodes: [ASHA, OLD, LAB],
    cfgs: [
      { strategy: "local", node: ASHA },
      { strategy: "mode1", node: OLD },
      { strategy: "mode1", node: LAB },
    ],
    groups: [
      { family: "io.hash", params: { size_mb: 256 }, condition: "ethernet", ms: [3_100, 4_900, 4_200], cv: 0.04 },
      { family: "io.hash", params: { size_mb: 1024 }, condition: "ethernet", ms: [12_300, 18_100, 15_400], cv: 0.04 },
      { family: "io.hash", params: { size_mb: 256 }, condition: "wifi", ms: [3_100, 9_800, 8_600], cv: 0.12 },
      { family: "io.hash", params: { size_mb: 1024 }, condition: "wifi", ms: [12_300, 37_600, 31_900], cv: 0.12 },
    ],
    reps: 5,
    state: "cancelled",
    finished: "cancelled",
    runnerOwned: true,
    ran: 33,
    failed: [17, 26],
    retried: [20],
    conditionFailures: { check_failed: 1 },
    missing: { group: 1, cfg: 1, field: "planner_node_id", count: 2 },
    budget: { max_runs: 60, max_hours: 2 },
    notes: ["Same transfers over wifi and ethernet. Cancelled: old-desktop kept dropping off the wifi."],
  },
  {
    tag: "REAL",
    name: "real-data-ai-2-gpus",
    createdH: 50,
    startedH: 49.6,
    seed: 2100,
    session: "lab ethernet",
    origin: ASHA,
    nodes: [ASHA, LAB],
    cfgs: [
      { strategy: "local", node: ASHA },
      { strategy: "mode1", node: LAB },
      { strategy: "mode3", node: ASHA, set: [ASHA, LAB] },
    ],
    groups: [
      { family: "ml.train_real", params: { epochs: 1 }, ms: [66_400, 46_300, 44_900], cv: [0.03, 0.14, 0.05] },
      { family: "ml.train_real", params: { epochs: 3 }, ms: [192_500, 131_900, 95_300], cv: 0.04 },
      { family: "ml.train_real", params: { epochs: 5 }, ms: [318_000, 214_600, 151_400], cv: 0.035 },
    ],
    reps: 5,
    state: "done",
    finished: "complete",
    runnerOwned: true,
    retried: [12],
    budget: { max_runs: 45, max_hours: 6 },
    notes: ["Real images, real labels: a training run split over two GPUs. Best: 2.10x (318 s -> 151 s)."],
  },
  {
    tag: "GPU3",
    name: "text-ai-3-gpus",
    createdH: 74,
    startedH: 73.5,
    seed: 3190,
    session: "lab ethernet",
    origin: ASHA,
    nodes: [ASHA, LAB, MINI],
    cfgs: [
      { strategy: "local", node: ASHA },
      { strategy: "mode1", node: LAB },
      { strategy: "mode3", node: ASHA, set: [ASHA, LAB, MINI] },
    ],
    groups: [
      { family: "ml.textgen", params: { prompts: 512 }, ms: [27_400, 17_900, 11_300] },
      { family: "ml.textgen", params: { prompts: 2048 }, ms: [104_800, 66_100, 34_200] },
      { family: "ml.textgen", params: { prompts: 8192 }, ms: [412_000, 259_000, 129_150] },
    ],
    reps: 4,
    state: "done",
    finished: "complete",
    runnerOwned: true,
    budget: { max_runs: 36, max_hours: 6 },
    notes: [
      "Text generation over three GPUs (RTX 3060 laptop, RTX 4070, GTX 1650).",
      "Best: 3.19x = 97 % of ideal (3.29x from the GPUs' measured rates).",
    ],
  },
  {
    tag: "CPU4",
    name: "cpu-split-4-laptops",
    createdH: 108,
    startedH: 107.6,
    seed: 3210,
    session: "lab ethernet",
    origin: ASHA,
    nodes: [...SPLIT4],
    cfgs: [
      { strategy: "local", node: ASHA },
      { strategy: "mode1", node: LAB },
      { strategy: "mode3", node: ASHA, set: SPLIT4 },
    ],
    groups: [
      { family: "cpu.primes", params: { limit: 10_000_000 }, ms: [690, 610, 402] },
      { family: "cpu.primes", params: { limit: 100_000_000 }, ms: [6_420, 4_310, 2_290] },
      { family: "cpu.primes", params: { limit: 1_000_000_000 }, ms: [61_400, 40_800, 19_130] },
    ],
    reps: 5,
    state: "done",
    finished: "complete",
    runnerOwned: true,
    retried: [31],
    budget: { max_runs: 45, max_hours: 2 },
    notes: ["CPU-only split over four laptops. Best: 3.21x (61.4 s -> 19.1 s)."],
  },
  {
    tag: "CPU2",
    name: "cpu-split-2-laptops",
    createdH: 122,
    startedH: 121.7,
    seed: 1970,
    session: "lab ethernet",
    origin: ASHA,
    nodes: [ASHA, RAVI],
    cfgs: [
      { strategy: "local", node: ASHA },
      { strategy: "local", node: RAVI },
      { strategy: "mode3", node: ASHA, set: [ASHA, RAVI] },
    ],
    groups: [
      { family: "cpu.primes", params: { limit: 10_000_000 }, ms: [690, 742, 512] },
      { family: "cpu.primes", params: { limit: 100_000_000 }, ms: [6_420, 6_890, 3_480] },
      { family: "cpu.primes", params: { limit: 1_000_000_000 }, ms: [61_400, 65_900, 31_200] },
    ],
    reps: 5,
    state: "done",
    finished: "complete",
    runnerOwned: true,
    budget: { max_runs: 45, max_hours: 2 },
    notes: ["CPU-only split over two laptops. Best: 1.97x (61.4 s -> 31.2 s)."],
  },
  {
    tag: "WIN",
    name: "windows-build-split",
    createdH: 146,
    startedH: 145.5,
    seed: 3400,
    session: "lab ethernet",
    origin: ASHA,
    nodes: [...SPLIT4],
    cfgs: [
      { strategy: "local", node: ASHA },
      { strategy: "mode1", node: LAB },
      { strategy: "mode3", node: ASHA, set: SPLIT4 },
    ],
    groups: [
      { family: "build.compile", params: { targets: 16 }, ms: [44_100, 27_800, 15_600] },
      { family: "build.compile", params: { targets: 32 }, ms: [84_300, 50_200, 27_100] },
      { family: "build.compile", params: { targets: 64 }, ms: [164_000, 92_600, 48_000] },
    ],
    reps: 6,
    state: "done",
    finished: "complete",
    runnerOwned: true,
    preempted: 1,
    budget: { max_runs: 54, max_hours: 3 },
    notes: ["A C++ build on Windows laptops, split by target. Best: 3.4x (164 s -> 48 s)."],
  },
];

/* ------------------------------------------------------------ static model */

interface RunSlot {
  seq: number;
  group: number;
  cfg: number;
  rep: number;
}

interface Model {
  spec: Spec;
  id: string;
  slots: RunSlot[];
  groupIds: string[];
  /** config_id per [group][cfg]. */
  cfgIds: string[][];
  createdAt: number;
  specText: string;
  digest: string;
  /** The median run (seconds) and the pace a finished campaign ran at. */
  medianRunS: number;
}

function specText(s: Spec): string {
  const host = (id: string) => HOST[id] ?? id;
  const families = [...new Set(s.groups.map((g) => g.family))];
  const lines = s.notes.map((n) => `# ${n}`);
  lines.push(`name: ${s.name}`, `seed: ${String(s.seed)}`, `origin: ${host(s.origin)}`, `nodes: [${s.nodes.map(host).join(", ")}]`, "families:");
  for (const f of families) {
    const groups = s.groups.filter((g) => g.family === f);
    const keys = [...new Set(groups.flatMap((g) => Object.keys(g.params)))];
    lines.push(`  - family: ${f}`);
    for (const k of keys) lines.push(`    sweep: {${k}: [${[...new Set(groups.map((g) => g.params[k]))].join(", ")}]}`);
  }
  lines.push("strategies:");
  for (const c of s.cfgs) {
    if (c.strategy === "local") lines.push(`  - local: ${host(c.node)}`);
    else if (c.strategy === "mode1") lines.push(`  - mode1: ${host(c.node)}`);
    else lines.push(`  - mode3: [${(c.set ?? []).map(host).join(", ")}]`);
  }
  const conditions = [...new Set(s.groups.map((g) => g.condition ?? "none"))];
  lines.push(
    `conditions: [${conditions.join(", ")}]`,
    `repetitions: ${String(s.reps)}`,
    `budget: {max_runs: ${String(s.budget.max_runs)}, max_hours: ${String(s.budget.max_hours)}}`,
    "throttle: {max_concurrent_per_node: 1, cooldown_s: 5, max_infra_retries: 2}",
  );
  return `${lines.join("\n")}\n`;
}

function buildModel(spec: Spec): Model {
  const slots: RunSlot[] = [];
  let seq = 0;
  spec.groups.forEach((_, group) => {
    for (let rep = 0; rep < spec.reps; rep++) {
      spec.cfgs.forEach((__, cfg) => {
        seq += 1;
        slots.push({ seq, group, cfg, rep });
      });
    }
  });
  const groupIds = spec.groups.map((_, g) => ulid("grp", `${spec.tag}G${String(g + 1).padStart(2, "0")}`));
  const cfgIds = spec.groups.map((_, g) => spec.cfgs.map((__, c) => ulid("cfg", `${spec.tag}G${String(g + 1)}C${String(c + 1)}`)));
  const text = specText(spec);
  const all = spec.groups.flatMap((g) => g.ms);
  return {
    spec,
    id: ulid("camp", spec.tag),
    slots,
    groupIds,
    cfgIds,
    createdAt: T0 - spec.createdH * H,
    specText: text,
    digest: `sha256:${[0, 1, 2, 3, 4, 5, 6, 7].map((k) => hash(`${text}:${String(k)}`).toString(16).padStart(8, "0")).join("")}`,
    medianRunS: median(all) / 1000,
  };
}

const MODELS = SPECS.map(buildModel);

/* ------------------------------------------------------------ live state */

interface Live {
  state: CampaignState;
  runnerOwned: boolean;
  startedAt: number | null;
  finished: CampaignStatus["finished_reason"];
  /** Runs ended at `t0`; while active + runner-owned one more ends every STEP_MS. */
  base: number;
  t0: number;
  cancelledJobs: number;
}

const LIVE = new Map<string, Live>(
  MODELS.map((m) => [
    m.id,
    {
      state: m.spec.state,
      runnerOwned: m.spec.runnerOwned,
      startedAt: m.spec.startedH === null ? null : T0 - m.spec.startedH * H,
      finished: m.spec.finished,
      base: m.spec.live?.base ?? m.spec.ran ?? (m.spec.state === "done" ? m.slots.length : 0),
      t0: T0,
      cancelledJobs: m.spec.state === "cancelled" ? 2 : 0,
    },
  ]),
);

function moving(live: Live): boolean {
  return live.state === "active" && live.runnerOwned;
}

/** Runs ended now (done + failed + skipped are counted from it). */
function ended(model: Model, live: Live): number {
  const total = model.slots.length;
  if (!moving(live)) return Math.min(total, live.base);
  let n = live.base + Math.floor((Date.now() - live.t0) / STEP_MS);
  if (n >= total) {
    if (model.spec.live?.loop === true) {
      // the overnight sweep starts over, so there is always one running campaign
      live.base = model.spec.live.base;
      live.t0 = Date.now();
      n = live.base;
    } else {
      live.state = "done";
      live.finished = "complete";
      live.base = total;
      n = total;
    }
  }
  return n;
}

function inflight(model: Model, live: Live): number {
  return moving(live) ? Math.min(2, model.spec.nodes.length) : 0;
}

/** Each run's state now. */
function runStates(model: Model, live: Live): RunState[] {
  const e = ended(model, live);
  const fly = inflight(model, live);
  const failed = new Set(model.spec.failed ?? []);
  return model.slots.map((s) => {
    if (s.seq <= e) return failed.has(s.seq) ? "failed" : "done";
    if (live.state === "cancelled" || live.state === "done") return "skipped";
    return s.seq <= e + fly ? "submitted" : "planned";
  });
}

function countsOf(states: readonly RunState[]): RunCounts {
  const counts: RunCounts = { planned: 0, submitted: 0, done: 0, failed: 0, skipped: 0 };
  for (const s of states) counts[s] += 1;
  return counts;
}

function find(id: string): { model: Model; live: Live } | undefined {
  const model = MODELS.find((m) => m.id === id);
  const live = LIVE.get(id);
  return model === undefined || live === undefined ? undefined : { model, live };
}

/* ------------------------------------------------------------ list / detail */

function summaryOf(m: Model): CampaignOut["summary"] {
  const parallel = Math.max(1, m.spec.nodes.length - 1);
  return {
    configs: m.spec.groups.length * m.spec.cfgs.length,
    dropped: 0,
    duplicates: m.spec.tag === "PLAN" ? 2 : 0,
    estimated_hours: round1((m.slots.length * (m.medianRunS + 5)) / parallel / 3600) || 0.1,
    groups: m.spec.groups.length,
    nodes: [...m.spec.nodes],
    session_label: m.spec.session,
    unsupported: 0,
  };
}

function outOf(m: Model, live: Live): CampaignOut {
  return {
    campaign_id: m.id,
    counts: countsOf(runStates(m, live)),
    created_at: iso(m.createdAt),
    created_by: m.spec.createdBy ?? "usr_demo_admin",
    name: m.spec.name,
    run_count: m.slots.length,
    seed: m.spec.seed,
    spec_digest: m.digest,
    state: live.state,
    summary: summaryOf(m),
  };
}

export function demoCampaignList(): CampaignList {
  const campaigns = MODELS.map((m) => outOf(m, LIVE.get(m.id) as Live)).sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  return { count: campaigns.length, campaigns };
}

export function demoCampaignDetail(id: string): CampaignDetail | undefined {
  const found = find(id);
  if (found === undefined) return undefined;
  return { ...outOf(found.model, found.live), spec: found.model.specText };
}

/* ------------------------------------------------------------ runs */

function runOf(m: Model, slot: RunSlot, state: RunState, live: Live): RunOut {
  const s = m.spec;
  const cfg = s.cfgs[slot.cfg] as Cfg;
  const group = s.groups[slot.group] as Group;
  const runS = (group.ms[slot.cfg] ?? 1000) / 1000;
  const started = live.startedAt ?? m.createdAt;
  const pace = s.live !== undefined || s.startedH === null ? STEP_MS / 1000 : m.medianRunS / Math.max(1, s.nodes.length - 1) + 5;
  const base = s.live !== undefined ? live.t0 - live.base * STEP_MS : started;
  const submittedAt = base + (slot.seq - 1) * pace * 1000;
  const retried = (s.retried ?? []).includes(slot.seq);
  const failed = state === "failed";
  const ran = state === "done" || failed;
  const loser = cfg.strategy === "mode1" ? cfg.node : OLD;
  return {
    run_id: `run_${s.tag.toLowerCase()}_${String(slot.seq).padStart(4, "0")}`,
    seq: slot.seq,
    group_id: m.groupIds[slot.group] ?? "",
    config_id: m.cfgIds[slot.group]?.[slot.cfg] ?? "",
    repetition: slot.rep,
    family: group.family,
    params: { ...group.params },
    seed: s.seed * 1000 + slot.seq,
    conditions: [group.condition ?? "none"],
    strategy: cfg.strategy,
    node_id: cfg.node,
    node_set: cfg.set === undefined ? null : [...cfg.set],
    origin_node_id: s.origin,
    state,
    job_id: state === "planned" || (state === "skipped" && !ran) ? null : ulid("job", `B${s.tag}${String(slot.seq)}`),
    detail: failed ? "infra retries used up (2)" : state === "skipped" ? "campaign cancelled" : null,
    submitted_at: state === "planned" || state === "skipped" ? null : iso(submittedAt),
    ended_at: ran ? iso(submittedAt + runS * 1000) : null,
    tries: failed ? 3 : retried && ran ? 2 : state === "submitted" ? 1 : ran ? 1 : 0,
    last_failure: failed
      ? `node_lost: ${HOST[loser] ?? loser} stopped answering (heartbeat timeout 30 s)`
      : retried && ran
        ? "network_error: the stream to the node was lost"
        : null,
  };
}

/** `GET .../runs?after=&limit=&state=`: keyset pages in seq order. */
export function demoRuns(id: string, after: number, limit: number, state: string | null): RunList | undefined {
  const found = find(id);
  if (found === undefined) return undefined;
  const { model, live } = found;
  const states = runStates(model, live);
  const all = model.slots
    .map((slot, i) => ({ slot, state: states[i] ?? "planned" }))
    .filter((r) => r.slot.seq > after && (state === null || r.state === state));
  const page = all.slice(0, limit);
  const last = page.at(-1);
  return {
    campaign_id: id,
    count: page.length,
    next_after: all.length > limit && last !== undefined ? last.slot.seq : null,
    runs: page.map((r) => runOf(model, r.slot, r.state, live)),
  };
}

/* ------------------------------------------------------------ labels */

interface CfgTally {
  done: number;
  failed: number;
  open: number;
}

function tallies(m: Model, states: readonly RunState[]): CfgTally[][] {
  const t = m.spec.groups.map(() => m.spec.cfgs.map(() => ({ done: 0, failed: 0, open: 0 })));
  m.slots.forEach((slot, i) => {
    const cell = t[slot.group]?.[slot.cfg];
    if (cell === undefined) return;
    const st = states[i];
    if (st === "done") cell.done += 1;
    else if (st === "failed") cell.failed += 1;
    else if (st === "planned" || st === "submitted") cell.open += 1;
  });
  return t;
}

function computePerIter(group: Group, c: number, n: number, key: string): MetricStats | null {
  const limit = group.params.limit;
  if (group.family !== "cpu.primes" || limit === undefined || n === 0) return null;
  const local = group.ms[0] ?? 0;
  return stats(((local * 1e6) / limit) * (c === 0 ? 1 : 1.03 + c * 0.01), 0.02, n, `${key}:iter`);
}

function bytesOf(group: Group, cfg: Cfg, n: number, key: string): MetricStats | null {
  if (cfg.strategy === "local" || n === 0) return null;
  const base = (BYTES[group.family] ?? 1e6) * (group.family === "io.hash" ? (group.params.size_mb ?? 1) : 1);
  return stats(base * (cfg.strategy === "mode3" ? 1.15 : 1), 0.01, n, `${key}:bytes`);
}

function groupLabel(m: Model, g: number, tally: readonly CfgTally[]): GroupLabel {
  const s = m.spec;
  const group = s.groups[g] as Group;
  const ids = m.cfgIds[g] ?? [];
  const configs: ConfigLabel[] = s.cfgs.map((cfg, c) => {
    const n = tally[c]?.done ?? 0;
    const key = ids[c] ?? "";
    return {
      config_id: key,
      strategy: cfg.strategy,
      node_id: cfg.node,
      origin_node_id: s.origin,
      planner_node_id: null,
      state: "measured",
      repetitions: n,
      total_ms: n === 0 ? null : stats(group.ms[c] ?? 0, cvOf(group, c), n, key),
      compute_per_iter_ns: computePerIter(group, c, n, key),
      bytes: bytesOf(group, cfg, n, key),
    };
  });
  const fastest = group.ms.reduce((best, v, i) => (v < (group.ms[best] ?? Infinity) ? i : best), 0);
  const plannerNode = s.cfgs[group.planner ?? fastest]?.node ?? null;
  for (const c of configs) c.planner_node_id = plannerNode;

  const measured = configs.filter((c) => c.total_ms !== null).sort((a, b) => (a.total_ms?.median ?? 0) - (b.total_ms?.median ?? 0));
  const open = tally.some((t) => t.open > 0);
  const base = { group_id: m.groupIds[g] ?? "", family: group.family, condition: group.condition ?? "none", params: { ...group.params }, configs, planner_node_id: plannerNode };
  const none = { best_config_id: null, best_node_id: null, best_strategy: null, margin_pct: null, tied_config_ids: [], planner_agrees: null };
  if (open) return { ...base, ...none, label: "pending", reason: "runs of this group are still open" };
  if (configs.some((c) => c.repetitions < 3)) {
    return { ...base, ...none, label: "insufficient", reason: "a configuration has fewer than 3 measured repetitions" };
  }
  const best = measured[0];
  const runnerUp = measured[1];
  if (best?.total_ms == null) return { ...base, ...none, label: "insufficient", reason: "nothing measured" };
  const high = best.total_ms.ci_high;
  const separated = measured.slice(1).every((c) => (c.total_ms?.ci_low ?? Infinity) > high);
  if (!separated) {
    return {
      ...base,
      ...none,
      label: "tie",
      reason: "CIs overlap",
      tied_config_ids: measured.filter((c) => (c.total_ms?.ci_low ?? Infinity) <= high).map((c) => c.config_id),
    };
  }
  return {
    ...base,
    label: "best",
    reason: null,
    best_config_id: best.config_id,
    best_node_id: best.node_id,
    best_strategy: best.strategy,
    margin_pct: runnerUp?.total_ms == null ? null : ((runnerUp.total_ms.median - best.total_ms.median) / best.total_ms.median) * 100,
    tied_config_ids: [],
    planner_agrees: plannerNode === best.node_id,
  };
}

export function demoLabels(id: string): LabelsOut | undefined {
  const found = find(id);
  if (found === undefined) return undefined;
  const { model, live } = found;
  const states = runStates(model, live);
  const t = tallies(model, states);
  const groups = model.spec.groups
    .map((_, g) => ({ g, tally: t[g] ?? [] }))
    .filter(({ tally }) => tally.some((c) => c.done + c.failed > 0))
    .map(({ g, tally }) => groupLabel(model, g, tally));
  const counts = { best: 0, tie: 0, insufficient: 0, pending: 0 };
  for (const g of groups) counts[g.label] += 1;
  const rows = states.filter((s) => s === "done" || s === "failed").length;
  return { campaign_id: id, campaign_state: live.state, labels_version: 1, counts, groups, rows };
}

/* ------------------------------------------------------------ quality */

const NO_MISSING = {
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

interface Tallied {
  states: RunState[];
  ranSeqs: Set<number>;
  infra: number;
  retries: number;
  preempted: number;
  measured: number;
  conditions: Record<string, number>;
  records: number;
}

function tallied(m: Model, live: Live): Tallied {
  const states = runStates(m, live);
  const ranSeqs = new Set(m.slots.filter((_, i) => states[i] === "done" || states[i] === "failed").map((s) => s.seq));
  const retries = (m.spec.retried ?? []).filter((q) => ranSeqs.has(q)).length;
  const failedRuns = states.filter((s) => s === "failed").length;
  // a failed run spent all its tries on infrastructure failures (1 + 2 retries)
  const infra = retries + failedRuns * 3;
  const preempted = ranSeqs.size > 0 ? (m.spec.preempted ?? 0) : 0;
  const measured = states.filter((s) => s === "done").length;
  const conditions = ranSeqs.size > 0 ? { ...(m.spec.conditionFailures ?? {}) } : {};
  const records = measured + infra + preempted + Object.values(conditions).reduce((a, b) => a + b, 0);
  return { states, ranSeqs, infra, retries, preempted, measured, conditions, records };
}

function infraRate(t: Tallied): number | null {
  const denominator = t.records - t.preempted;
  return denominator > 0 ? t.infra / denominator : null;
}

export function demoQuality(id: string): QualityReport | undefined {
  const found = find(id);
  if (found === undefined) return undefined;
  const { model, live } = found;
  const s = model.spec;
  const t = tallied(model, live);
  const cells = tallies(model, t.states);
  const configs: QualityConfig[] = s.groups.flatMap((group, g) =>
    s.cfgs.map((cfg, c) => {
      const cell = cells[g]?.[c] ?? { done: 0, failed: 0, open: 0 };
      const key = model.cfgIds[g]?.[c] ?? "";
      const n = cell.done;
      const total = n === 0 ? null : stats(group.ms[c] ?? 0, cvOf(group, c), n, key);
      const missing = { ...NO_MISSING };
      if (s.missing !== undefined && s.missing.group === g && s.missing.cfg === c && n > 0) {
        (missing as Record<string, number>)[s.missing.field] = s.missing.count;
      }
      const cvFlag = total?.cv != null && total.cv > CV_THRESHOLD;
      const repsFlag = n < 3 && cell.open === 0;
      const outliers = total?.outliers ?? 0;
      return {
        group_id: model.groupIds[g] ?? "",
        config_id: key,
        family: group.family,
        condition: group.condition ?? "none",
        strategy: cfg.strategy,
        node_id: cfg.node,
        state: "measured",
        planned: s.reps,
        done: n,
        n,
        total_ms: total,
        compute_per_iter_ns: computePerIter(group, c, n, key),
        outlier_attempt_ids: outliers > 0 ? [`att_${s.tag.toLowerCase()}_${String(g + 1)}${String(c + 1)}_r${String(Math.min(n, 3))}`] : [],
        cv_flag: cvFlag,
        reps_flag: repsFlag,
        flagged: cvFlag || repsFlag || Object.values(missing).some((v) => v > 0),
        records_checked: n + cell.failed * 3,
        missing,
      };
    }),
  );
  const pending = live.state === "active" || live.state === "paused";
  const rate = infraRate(t);
  const measuredConfigs = configs.filter((c) => c.n > 0);
  return {
    campaign_id: id,
    quality_version: 1,
    summary: {
      campaign_state: live.state,
      pending,
      runs_by_state: countsOf(t.states),
      records: t.records,
      bench_role_records: t.records > 0 ? { warmup: s.nodes.length, filler: s.groups.length } : {},
      measured_records: t.measured,
      infra_failures: t.infra,
      owner_preempted: t.preempted,
      infra_failure_rate: rate,
      condition_failures: t.conditions,
      workload_failures: {},
      rejected_ok: {},
      other_failures: {},
      crit1_infra_ok: rate === null ? null : rate < 0.02,
      crit3_reps_ok: measuredConfigs.length === 0 ? null : configs.every((c) => c.n >= 3 || (pending && !c.reps_flag)),
      configs: configs.length,
      flagged_configs: configs.filter((c) => c.flagged).length,
      cv_threshold: CV_THRESHOLD,
    },
    configs,
  };
}

/* ------------------------------------------------------------ status */

export function demoCampaignStatus(id: string): CampaignStatus | undefined {
  const found = find(id);
  if (found === undefined) return undefined;
  const { model, live } = found;
  const t = tallied(model, live);
  const counts = countsOf(t.states);
  const endedRuns = counts.done + counts.failed + counts.skipped;
  const open = counts.planned + counts.submitted;
  return {
    campaign_id: id,
    state: live.state,
    runner_owned: live.runnerOwned,
    runner_degraded: false,
    started_at: live.startedAt === null ? null : iso(live.startedAt),
    finished_reason: live.finished,
    max_concurrent_per_node: 1,
    cooldown_s: 5,
    max_infra_retries: 2,
    counts,
    cancelled_jobs: live.cancelledJobs,
    done_percent: round1((endedRuns / Math.max(1, model.slots.length)) * 100),
    median_run_s: t.measured === 0 ? null : round1(model.medianRunS),
    eta_s: moving(live) && open > 0 ? open * (STEP_MS / 1000) : null,
    infra_failure_rate: infraRate(t),
    records: t.records,
    infra_failures: t.infra,
    preempted: t.preempted,
    retries: t.retries,
  };
}

/* ------------------------------------------------------------ controls */

export type DemoControl = { ok: true; body: CampaignControlOut } | { ok: false; status: number; body: unknown };

const REFUSED: Record<CampaignVerb, string> = {
  start: "only an active campaign can be started",
  pause: "only an active, runner-owned campaign can pause",
  resume: "only a paused campaign can resume",
  cancel: "the campaign has already finished",
};

/** `POST .../{verb}`: the demo campaign's state changes in memory (until a reload). */
export function demoControl(id: string, verb: string): DemoControl | undefined {
  const found = find(id);
  if (found === undefined || !["start", "pause", "resume", "cancel"].includes(verb)) return undefined;
  const { model, live } = found;
  const v = verb as CampaignVerb;
  const now = Date.now();
  const at = ended(model, live);
  const allowed =
    (v === "start" && live.state === "active") ||
    (v === "pause" && live.state === "active" && live.runnerOwned) ||
    (v === "resume" && live.state === "paused") ||
    (v === "cancel" && (live.state === "active" || live.state === "paused"));
  if (!allowed) return { ok: false, status: 409, body: { error: { code: "conflict", message: REFUSED[v], reason: "campaign_state" } } };
  if (v === "start" && !live.runnerOwned) {
    live.runnerOwned = true;
    live.startedAt ??= now;
    live.base = at;
    live.t0 = now;
  } else if (v === "pause") {
    live.state = "paused";
    live.base = at;
  } else if (v === "resume") {
    live.state = "active";
    live.base = at;
    live.t0 = now;
  } else if (v === "cancel") {
    live.cancelledJobs = inflight(model, live);
    live.state = "cancelled";
    live.finished = "cancelled";
    live.base = at;
  }
  return {
    ok: true,
    body: {
      campaign_id: id,
      state: live.state,
      counts: countsOf(runStates(model, live)),
      runner_owned: live.runnerOwned,
      started_at: live.startedAt === null ? null : iso(live.startedAt),
      finished_reason: live.finished,
      cancelled_jobs: v === "cancel" ? live.cancelledJobs : 0,
      cooldown_s: 5,
      max_concurrent_per_node: 1,
      max_infra_retries: 2,
    },
  };
}

/** The campaigns whose numbers move by themselves (install.ts refreshes them). */
export function demoMovingCampaigns(): string[] {
  return MODELS.filter((m) => {
    const live = LIVE.get(m.id);
    return live !== undefined && moving(live);
  }).map((m) => m.id);
}

/* ------------------------------------------------------------ downloads */

/** The text of a small but complete Parquet-shaped file (`PAR1` … `PAR1`), so the page's completeness check passes. */
export function demoParquet(id: string, what: "records" | "labels"): string | undefined {
  const found = find(id);
  if (found === undefined) return undefined;
  const { model, live } = found;
  const body =
    what === "records"
      ? demoRuns(id, 0, model.slots.length, null)?.runs.map((r) => `${String(r.seq)},${r.run_id},${r.config_id},${r.node_id},${r.state}`).join("\n") ?? ""
      : (demoLabels(id)?.groups.map((g) => `${g.group_id},${g.family},${g.label},${g.best_config_id ?? ""}`).join("\n") ?? "");
  const text = `PAR1\n# hive demo ${what} of ${model.spec.name} (${live.state}); not a real Parquet file\n${body}\nPAR1`;
  return text;
}
