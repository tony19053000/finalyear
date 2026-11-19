/**
 * DEMO DATA (localhost `?demo=1` only - see demo/install.ts). Fake controller
 * answers so the Overview can be seen filled: 5 laptops, 4 jobs with planner
 * decisions, a split job whose chunks advance. Values drift with time.
 * Delete the whole src/demo/ folder (and its two lines in main.tsx) to remove.
 */
const GiB = 1024 ** 3;

interface DemoGpu {
  vramGiB: number;
}

interface DemoNode {
  id: string;
  hostname: string;
  cores: number;
  ramGiB: number;
  gpu: DemoGpu | null;
  gpuModel: string | null;
  state: "online" | "suspect" | "offline";
  /** Base CPU %, RAM fraction, GPU util %. */
  cpu: number;
  ram: number;
  gpuUtil: number;
  os: string;
}

export const DEMO_NODES: DemoNode[] = [
  { id: "node_demo_asha", hostname: "asha-laptop", cores: 8, ramGiB: 16, gpu: { vramGiB: 6 }, gpuModel: "NVIDIA GeForce RTX 3060 Laptop GPU", state: "online", cpu: 78, ram: 0.74, gpuUtil: 84, os: "Windows 11" },
  { id: "node_demo_lab07", hostname: "lab-pc-07", cores: 16, ramGiB: 32, gpu: { vramGiB: 12 }, gpuModel: "NVIDIA GeForce RTX 4070", state: "online", cpu: 52, ram: 0.56, gpuUtil: 61, os: "Ubuntu 24.04" },
  { id: "node_demo_ravi", hostname: "ravi-thinkpad", cores: 8, ramGiB: 16, gpu: null, gpuModel: null, state: "online", cpu: 34, ram: 0.45, gpuUtil: 0, os: "Ubuntu 22.04" },
  { id: "node_demo_old", hostname: "old-desktop", cores: 4, ramGiB: 8, gpu: null, gpuModel: null, state: "suspect", cpu: 22, ram: 0.62, gpuUtil: 0, os: "Windows 10" },
  { id: "node_demo_mini", hostname: "mini-pc", cores: 4, ramGiB: 8, gpu: { vramGiB: 4 }, gpuModel: "NVIDIA GeForce GTX 1650", state: "offline", cpu: 0, ram: 0, gpuUtil: 0, os: "Ubuntu 24.04" },
];

const T0 = Date.now();

function elapsedS(): number {
  return (Date.now() - T0) / 1000;
}

function iso(msAgo: number): string {
  return new Date(Date.now() - msAgo).toISOString();
}

/** Slow, smooth drift (a few sines, period ~1-4 min), at `atMs` (default now). */
function drift(seed: number, amp: number, atMs: number = Date.now()): number {
  const t = (atMs - T0) / 1000;
  return amp * (0.6 * Math.sin(t / 23 + seed) + 0.3 * Math.sin(t / 61 + seed * 2.1) + 0.1 * Math.sin(t / 9 + seed * 3.7));
}

function clamp(v: number, a: number, b: number): number {
  return Math.max(a, Math.min(b, v));
}

function live(n: DemoNode, i: number, atMs: number = Date.now()) {
  if (n.state === "offline") return { cpu_percent: null, ram_used_bytes: null, ram_total_bytes: null, gpus: [], sample_age_s: null };
  const cpu = clamp(n.cpu + drift(i, 12, atMs), 2, 99);
  const ram = clamp(n.ram + drift(i + 5, 0.06, atMs), 0.1, 0.97);
  const gpus =
    n.gpu === null
      ? []
      : [
          {
            index: 0,
            util_percent: clamp(n.gpuUtil + drift(i + 9, 14, atMs), 0, 100),
            vram_used_bytes: Math.round(n.gpu.vramGiB * GiB * clamp(0.55 + drift(i + 3, 0.15, atMs), 0.1, 0.95)),
            vram_total_bytes: n.gpu.vramGiB * GiB,
          },
        ];
  return {
    cpu_percent: cpu,
    ram_used_bytes: Math.round(ram * n.ramGiB * GiB),
    ram_total_bytes: n.ramGiB * GiB,
    gpus,
    sample_age_s: n.state === "suspect" ? 14 : 0.5,
  };
}

function summary(n: DemoNode, i: number) {
  return {
    node_id: n.id,
    hostname: n.hostname,
    display_name: "",
    state: n.state,
    connected: n.state !== "offline",
    last_seen_at: iso(n.state === "offline" ? 47 * 60_000 : n.state === "suspect" ? 14_000 : 1_000),
    os: n.os,
    platform: n.os.startsWith("Windows") ? "windows-x86_64" : "linux-x86_64",
    agent_version: n.state === "offline" ? null : "1.0.0",
    agent_version_status: n.state === "offline" ? null : "same",
    rtt_ms: n.state === "offline" ? null : 3 + i,
    rtt_ewma_ms: n.state === "offline" ? null : 3 + i,
    scores: { status: "none", metrics: [] },
    ...live(n, i),
  };
}

export function demoNodeList() {
  const nodes = DEMO_NODES.map(summary);
  return { count: nodes.length, nodes };
}

export function demoNodeDetail(id: string) {
  const i = DEMO_NODES.findIndex((n) => n.id === id);
  const n = DEMO_NODES[i];
  if (n === undefined) return undefined;
  const offline = n.state === "offline";
  return {
    ...summary(n, i),
    first_seen_at: new Date(T0 - 9 * 86_400_000).toISOString(),
    session_epoch: 1,
    protocol_version: offline ? null : 1,
    peer_address: offline ? null : `ipv4:192.168.1.${String(21 + i)}:51820`,
    peer_endpoint: offline ? null : `192.168.1.${String(21 + i)}:7443`,
    certificate: {
      serial: `4f:a1:0${String(i)}:9c:2e:77:d3:b8`,
      fingerprint_sha256: "sha256:demo",
      public_key_fingerprint_sha256: "sha256:demo-key",
      not_after: new Date(T0 + (80 + i * 11) * 86_400_000).toISOString(),
    },
    cache: offline
      ? {}
      : {
          blob: { count: 412 + i * 37, bytes: Math.round((6.4 + i) * GiB) },
          tree: { count: 18 + i, bytes: Math.round((1.2 + i * 0.3) * GiB) },
          env: { count: 3 + (i % 2), bytes: Math.round((4.8 + i * 0.6) * GiB) },
          image: { count: n.gpu === null ? 1 : 2, bytes: Math.round((n.gpu === null ? 2.1 : 9.6) * GiB) },
        },
    owner_policy: offline
      ? null
      : {
          accepting: true,
          max_cpu_percent: 80,
          max_ram_bytes: Math.round(n.ramGiB * 0.6) * GiB,
          max_gpu_vram_bytes: n.gpu === null ? null : Math.round(n.gpu.vramGiB * 0.75 * GiB),
          only_when_plugged_in: true,
          quiet_hours: "22:00-07:00",
          allowed_users_count: 3,
        },
    not_enforced: offline ? null : n.os.startsWith("Windows") ? ["max_gpu_vram_bytes:no_cgroup_on_windows"] : [],
    scores:
      i === 0
        ? {
            status: "measured",
            measured_at: iso(2 * 86_400_000),
            metrics: [
              { name: "cpu_gflops", mean: 182.4, unit: "GFLOP/s", status: "ok", n: 5, detail: "", lower_bound: false },
              { name: "mem_bw", mean: 38.6, unit: "GB/s", status: "ok", n: 5, detail: "", lower_bound: false },
              { name: "gpu_tflops_fp16", mean: 25.3, unit: "TFLOP/s", status: "ok", n: 5, gpu_index: 0, detail: "", lower_bound: false },
              { name: "disk_read", mean: 2140, unit: "MB/s", status: "ok", n: 3, detail: "", lower_bound: false },
              { name: "net_to_controller", mean: null, unit: "MB/s", status: "unavailable", reason: "no_controller_blob", n: 0, detail: "", lower_bound: false },
            ],
          }
        : { status: offline ? "none" : "pending", metrics: [] },
    hardware: {
      cpu_model: n.cores >= 16 ? "Intel Core i7-13700" : n.cores >= 8 ? "AMD Ryzen 7 5800H" : "Intel Core i5-4590",
      cpu_cores: n.cores,
      cpu_threads: n.cores >= 8 ? n.cores * 2 : n.cores,
      ram_bytes: String(n.ramGiB * GiB),
      gpus:
        n.gpu === null
          ? []
          : [{ index: 0, model: n.gpuModel, vram_bytes: String(n.gpu.vramGiB * GiB), driver_version: "552.22", compute_capability: n.gpu.vramGiB >= 12 ? "8.9" : "8.6" }],
      capabilities: n.gpu === null ? ["exec", "exec-split"] : ["exec", "exec-split", "cuda", "docker"],
    },
  };
}

export function demoNodeMetrics(id: string) {
  const i = DEMO_NODES.findIndex((n) => n.id === id);
  const n = DEMO_NODES[i];
  // one sample every 5 s on a fixed grid, so successive fetches slide smoothly
  const step = 5_000;
  const end = Math.floor(Date.now() / step) * step;
  const samples =
    n === undefined || n.state === "offline"
      ? []
      : Array.from({ length: 60 }, (_, k) => {
          const at = end - (59 - k) * step;
          const l = live(n, i, at);
          const wobble = Math.sin(at / 9_000 + i) * 3.5 + Math.sin(at / 4_100 + i * 1.7) * 1.2;
          return {
            node_id: id,
            observed_at: new Date(at).toISOString(),
            age_s: (Date.now() - at) / 1000,
            cpu_percent: clamp((l.cpu_percent ?? 0) + wobble, 0, 100),
            ram_used_bytes: l.ram_used_bytes ?? 0,
            ram_total_bytes: l.ram_total_bytes ?? 0,
            gpus: l.gpus,
          };
        });
  return { node_id: id, window_s: 300, count: samples.length, samples };
}

/** `GET /v1/nodes/{id}/resources`: only the newest live sample's disks are read by the page. */
export function demoNodeResources(id: string) {
  const i = DEMO_NODES.findIndex((n) => n.id === id);
  const n = DEMO_NODES[i];
  if (n === undefined || n.state === "offline") return { node_id: id, history: [], history_count: 0, live: [], live_count: 0 };
  const windows = n.os.startsWith("Windows");
  const disks = windows
    ? [
        { mount: "C:\\", free_bytes: Math.round((182 + i * 9) * GiB), total_bytes: 476 * GiB },
        { mount: "D:\\", free_bytes: Math.round(611 * GiB), total_bytes: 931 * GiB },
      ]
    : [
        { mount: "/", free_bytes: Math.round((96 + i * 7) * GiB), total_bytes: 234 * GiB },
        { mount: "/home", free_bytes: Math.round((402 - i * 11) * GiB), total_bytes: 680 * GiB },
      ];
  const l = live(n, i);
  return {
    node_id: id,
    history: [],
    history_count: 0,
    live_count: 1,
    live: [{ sequence: 1, session_epoch: 1, observed_at: iso(1_000), age_s: 1, ...l, disks }],
  };
}

/** `GET /v1/network`: measured links between the live laptops, one stale pair, one failed probe. */
export function demoNetwork() {
  const ids = DEMO_NODES.filter((n) => n.state !== "offline").map((n) => n.id);
  const links: Record<string, unknown>[] = [];
  ids.forEach((src, a) => {
    ids.forEach((dst, b) => {
      if (src === dst) return;
      if (a + b === 3 && Math.min(a, b) === 1) return; // lab-pc-07 <-> ravi-thinkpad: never probed
      const stale = src === "node_demo_old" || dst === "node_demo_old";
      const bw = (38 + ((a * 7 + b * 13) % 70) + drift(a + b, 4)) * 1024 ** 2;
      links.push({
        src_node_id: src,
        dst_node_id: dst,
        rtt_ms: 1.4 + ((a + b) % 5) * 0.9,
        bw_bytes_s: bw,
        samples: 6,
        measured_at: iso(stale ? 840_000 : 40_000 + a * 9_000),
        age_s: stale ? 840 : 40 + a * 9,
        stale,
        source: "measured",
        last_failure: null,
      });
    });
  });
  return {
    now: new Date().toISOString(),
    links,
    controller_links: ids.slice(0, 3).map((id, k) => ({
      node_id: id,
      bw_bytes_s: (92 - k * 17) * 1024 ** 2,
      samples: 12 - k * 3,
      age_s: 70 + k * 40,
      last_at: iso((70 + k * 40) * 1000),
      source: "history",
    })),
    failures: [{ src_node_id: "node_demo_ravi", dst_node_id: "node_demo_old", error: "timeout after 10 s", at: iso(5 * 60_000) }],
    prober: {},
    stale_after_s: 600,
  };
}

/* ------------------------------------------------------------------ jobs */

const SPLIT_TOTAL = 30;

/** 18 -> 29 done chunks, one every ~11 s, then the run starts over at 18. */
function splitDone(): number {
  return 18 + (Math.floor(elapsedS() / 11) % (SPLIT_TOTAL - 18));
}

const JOBS = [
  {
    job_id: "job_demo_split",
    name: "render-frames",
    state: "running",
    created_at: iso(6 * 60_000),
    updated_at: iso(5_000),
    placement: { mode: "auto", node_id: null, policy: "cost_model" },
    submitter_node_id: "node_demo_asha",
    priority: 0,
  },
  {
    job_id: "job_demo_send",
    name: "train-resnet18",
    state: "running",
    created_at: iso(11 * 60_000),
    updated_at: iso(20_000),
    placement: { mode: "auto", node_id: null, policy: "cost_model" },
    submitter_node_id: "node_demo_ravi",
    priority: 0,
  },
  {
    job_id: "job_demo_local",
    name: "lint-and-test",
    state: "succeeded",
    created_at: iso(19 * 60_000),
    updated_at: iso(18 * 60_000),
    placement: { mode: "auto", node_id: null, policy: "cost_model" },
    submitter_node_id: "node_demo_asha",
    priority: 0,
  },
  {
    job_id: "job_demo_fail",
    name: "feature-extract",
    state: "failed",
    created_at: iso(34 * 60_000),
    updated_at: iso(31 * 60_000),
    placement: { mode: "auto", node_id: null, policy: "cost_model" },
    submitter_node_id: "node_demo_lab07",
    priority: 0,
    failure_type: "env_error",
    failure_detail: "exit 1: ModuleNotFoundError: No module named 'cv2'",
  },
] as const;

/** 17 more failed jobs so the Alerts card has 20 entries (with the suspect and offline laptops and job_demo_fail). */
const FAIL_REASONS = [
  "exit 1: CUDA out of memory (tried to allocate 2.00 GiB)",
  "exit 137: killed - out of memory",
  "timeout: no progress for 15 min",
  "exit 2: FileNotFoundError: data/train.csv",
  "node lost: lab-pc-07 stopped answering",
  "exit 1: ImportError: libGL.so.1 not found",
  "disk full: /var/lib/meshforge/work",
];
const EXTRA_FAILED = Array.from({ length: 17 }, (_, i) => ({
  job_id: `job_demo_fail_${String(i + 2)}`,
  name: ["resize-images", "train-tiny-gpt", "encode-video", "eval-bert", "scrape-prices", "build-index", "fit-xgboost"][i % 7] ?? "job",
  state: "failed",
  created_at: iso((40 + i * 9) * 60_000),
  updated_at: iso((38 + i * 9) * 60_000),
  placement: { mode: "auto", node_id: null, policy: "cost_model" },
  submitter_node_id: "node_demo_asha",
  priority: 0,
  failure_type: "user_error",
  failure_detail: FAIL_REASONS[i % FAIL_REASONS.length] ?? "exit 1",
}));

/** Every 20 s a different job is the newest running one (split -> sent away -> local), so the Overview's speed-up and progress move. */
const ROTATION = ["job_demo_split", "job_demo_send", "job_demo_local"] as const;
const ROTATE_S = 20;

function rotatedJobs() {
  const focus = ROTATION[Math.floor(elapsedS() / ROTATE_S) % ROTATION.length];
  const sinceS = elapsedS() % ROTATE_S;
  const base: Record<string, unknown>[] = [...JOBS];
  return base
    .map((j) =>
      j.job_id === focus
        ? { ...j, state: "running", created_at: iso((sinceS + (j.job_id === "job_demo_split" ? 300 : 4)) * 1000) }
        : j.job_id === "job_demo_local"
          ? { ...j, state: "succeeded" }
          : j,
    )
    .sort((a, b) => (a.job_id === focus ? -1 : b.job_id === focus ? 1 : 0))
    .concat(EXTRA_FAILED);
}

export function demoJobList(states: string[]) {
  const all = rotatedJobs();
  const jobs = states.length === 0 ? all : all.filter((j) => states.includes(String(j.state)));
  return { count: jobs.length, next_cursor: null, jobs };
}

const DEMO_TREE = `sha256:${"7c4e1a90b2".repeat(6)}2f19`;
const DEMO_OUT = `sha256:${"e3b98a0c41".repeat(6)}d7aa`;

/** The job's state changes: submitted -> planning -> queued -> preparing -> running (-> its end). */
function demoTransitions(job: Record<string, unknown>) {
  const start = Date.parse(String(job.created_at));
  const end = Date.parse(String(job.updated_at));
  const at = (s: number) => new Date(start + s * 1000).toISOString();
  const steps: [string | null, string, number][] = [
    [null, "submitted", 0],
    ["submitted", "planning", 0.4],
    ["planning", "queued", 1.1],
    ["queued", "preparing", 3.8],
    ["preparing", "running", 9.2],
  ];
  const state = String(job.state);
  if (state === "succeeded") steps.push(["running", "collecting", (end - start) / 1000 - 2.5], ["collecting", "succeeded", (end - start) / 1000]);
  else if (state === "failed" || state === "cancelled") steps.push(["running", state, (end - start) / 1000]);
  return steps.map(([from, to, s], i) => ({ seq: i + 1, from_state: from, to_state: to, at: at(s) }));
}

function attempt(n: number, node_id: string, extra: Record<string, unknown>) {
  return {
    attempt_id: `att_demo_${String(n)}`,
    node_id,
    fencing_token: n,
    role: "run",
    state: "running",
    exit_code: null,
    output_digest: null,
    missing_outputs: [],
    logs: {},
    started_at: iso(5 * 60_000),
    ended_at: null,
    transfers: [],
    ...extra,
  };
}

/** The attempts behind each demo job (lanes for the split, one run otherwise). */
function demoAttempts(job: Record<string, unknown>) {
  const id = String(job.job_id);
  const created = Date.parse(String(job.created_at));
  const ended = String(job.updated_at);
  const started = new Date(created + 9_200).toISOString();
  if (id === "job_demo_split") {
    return ["node_demo_asha", "node_demo_lab07", "node_demo_ravi"].map((node, k) =>
      attempt(k + 1, node, { attempt_id: `att_split_${String(k)}`, role: "lane", started_at: started, logs: { stdout: { bytes: 18_400 + k * 3_100, truncated: false, digest: null } } }),
    );
  }
  if (id === "job_demo_send") {
    return [
      attempt(1, "node_demo_lab07", {
        started_at: started,
        logs: { stdout: { bytes: 42_812, truncated: false, digest: null }, stderr: { bytes: 1_204, truncated: false, digest: null } },
        transfers: [{ tree_id: DEMO_TREE, source: "controller", bytes: 184 * 1024 ** 2, duration_ms: 6_400, units: 212, failures: 0, resumed_chunks: 0, dropped: null }],
      }),
    ];
  }
  if (id === "job_demo_local") {
    return [
      attempt(1, "node_demo_asha", {
        state: "done",
        exit_code: 0,
        started_at: started,
        ended_at: ended,
        output_digest: DEMO_OUT,
        logs: { stdout: { bytes: 8_190, truncated: false, digest: "sha256:demo-log" } },
      }),
    ];
  }
  // a failed job: one attempt that ended with the job's reason
  const node = DECISIONS[id]?.chosen ?? (Number(id.split("_").pop()) % 2 === 0 ? "node_demo_asha" : "node_demo_lab07");
  const reason = typeof job.failure_detail === "string" ? job.failure_detail : "";
  const exit = /^exit (\d+)/.exec(reason);
  return [
    attempt(1, node, {
      state: "failed",
      exit_code: exit === null ? null : Number(exit[1]),
      failure_type: job.failure_type ?? "user_error",
      detail: job.failure_detail ?? null,
      started_at: started,
      ended_at: ended,
      logs: { stdout: { bytes: 3_420, truncated: false, digest: "sha256:demo-log" }, stderr: { bytes: 912, truncated: false, digest: "sha256:demo-err" } },
    }),
  ];
}

export function demoJobDetail(id: string) {
  const job = ([...JOBS, ...EXTRA_FAILED] as Record<string, unknown>[]).find((j) => j.job_id === id);
  if (job === undefined) return undefined;
  const manifest: Record<string, unknown> = { name: job.name, entrypoint: ["python", "main.py"], run: { command: ["python", "main.py"] } };
  if (id === "job_demo_split") manifest.split = { items: 7500, chunk_items: 250 };
  return {
    ...job,
    tree_id: DEMO_TREE,
    output_digest: id === "job_demo_local" ? DEMO_OUT : null,
    manifest,
    attempts: demoAttempts(job),
    transitions: demoTransitions(job),
    inputs: [],
    warnings: [],
  };
}

function cand(node_id: string, mode: "local" | "offload" | "split", total_s: number, rank: number, extra: Record<string, unknown> = {}) {
  return {
    node_id,
    mode,
    feasible: true,
    rank,
    score: total_s,
    slot: "cpu",
    estimate: { total_s, terms: { compute: { seconds: total_s * 0.85, source: "history" }, transfer: { seconds: total_s * 0.15, source: "measured" } } },
    ...extra,
  };
}

const DECISIONS: Record<string, { chosen: string; candidates: ReturnType<typeof cand>[] }> = {
  job_demo_split: {
    chosen: "node_demo_lab07",
    candidates: [
      cand("node_demo_lab07", "split", 450, 1, { node_set: ["node_demo_asha", "node_demo_lab07", "node_demo_ravi"], chunks: 30, chunk_items: 250 }),
      cand("node_demo_lab07", "offload", 760, 2),
      cand("node_demo_asha", "local", 1440, 3),
    ],
  },
  job_demo_send: {
    chosen: "node_demo_lab07",
    candidates: [cand("node_demo_lab07", "offload", 400, 1), cand("node_demo_asha", "offload", 610, 2), cand("node_demo_ravi", "local", 840, 3)],
  },
  job_demo_local: {
    chosen: "node_demo_asha",
    candidates: [cand("node_demo_asha", "local", 12, 1), cand("node_demo_lab07", "offload", 20, 2)],
  },
  job_demo_fail: {
    chosen: "node_demo_lab07",
    candidates: [cand("node_demo_lab07", "local", 95, 1), cand("node_demo_asha", "offload", 130, 2)],
  },
};

/** The 17 extra failed jobs: alternately kept local on asha-laptop or sent to lab-pc-07. */
function extraDecision(id: string): { chosen: string; candidates: ReturnType<typeof cand>[] } | undefined {
  if (!id.startsWith("job_demo_fail_")) return undefined;
  const n = Number(id.split("_").pop());
  const t = 60 + (n % 5) * 35;
  return n % 2 === 0
    ? { chosen: "node_demo_asha", candidates: [cand("node_demo_asha", "local", t, 1), cand("node_demo_lab07", "offload", t * 1.4, 2)] }
    : { chosen: "node_demo_lab07", candidates: [cand("node_demo_lab07", "offload", t, 1), cand("node_demo_ravi", "offload", t * 1.3, 2), cand("node_demo_asha", "local", t * 2.2, 3)] };
}

export function demoDecisions(id: string) {
  const d = DECISIONS[id] ?? extraDecision(id);
  if (d === undefined) return undefined;
  return {
    job_id: id,
    count: 1,
    decisions: [
      {
        id: 1,
        attempt_id: `att_${id}`,
        attempt_no: 1,
        chosen_node_id: d.chosen,
        candidates: d.candidates,
        created_at: iso(5 * 60_000),
        outcome: "dispatched",
        policy: "cost_model",
        snapshot_digest: "sha256:demo",
      },
    ],
  };
}

export function demoSplit(id: string) {
  if (id !== "job_demo_split") return undefined;
  const done = splitDone();
  const leased = Math.min(3, SPLIT_TOTAL - done);
  const share = [0.3, 0.45, 0.25];
  const lanes = ["node_demo_asha", "node_demo_lab07", "node_demo_ravi"].map((node_id, k) => ({
    attempt_id: `att_split_${String(k)}`,
    node_id,
    state: "running",
    live: true,
    chunks_done: Math.floor(done * (share[k] ?? 0)),
    items_done: Math.floor(done * 250 * (share[k] ?? 0)),
    current: k < leased ? [{ chunk_index: done + k, lease_seq: 1, leased_at: iso(4_000), speculative: false }] : [],
    ended_at: null,
    failed_tries: 0,
    first_lease_at: iso(5 * 60_000),
    last_commit_at: iso(3_000),
    rate_items_s: [5.2, 8.1, 4.4][k] ?? null,
    started_at: iso(5 * 60_000),
  }));
  const map = Array.from({ length: SPLIT_TOTAL }, (_, i) => (i < done ? "d" : i < done + leased ? "l" : "p")).join("");
  return {
    job_id: id,
    job_state: "running",
    chunk_items: 250,
    chunk_map: map,
    chunks: { total: SPLIT_TOTAL, done, leased, pending: SPLIT_TOTAL - done - leased, failed: 0 },
    combine_ms: null,
    computed_at: new Date().toISOString(),
    elapsed_s: 300 + elapsedS(),
    ended_at: null,
    eta_s: (SPLIT_TOTAL - done) * 11,
    items_done: done * 250,
    items_total: SPLIT_TOTAL * 250,
    lanes,
    lanes_total: 3,
    live_leases: leased,
    predicted_s: 450,
    queue_state: "open",
    rate_items_s: 17.7,
    speculated: 0,
    speculative_live: 0,
    started_at: iso(5 * 60_000),
    warnings: [],
  };
}

const LANE_NODES = ["node_demo_asha", "node_demo_lab07", "node_demo_ravi"];

/** `GET /v1/jobs/{id}/chunks?start=N&limit=1`: one chunk of the split, from the live chunk map. */
export function demoChunks(id: string, start: number) {
  const split = demoSplit(id);
  if (split === undefined) return undefined;
  const ch = split.chunk_map.charAt(start);
  if (ch === "") return { job_id: id, count: 0, next_cursor: null, chunks: [] };
  const node = LANE_NODES[start % 3] ?? "node_demo_asha";
  const done = ch === "d";
  const leased = ch === "l";
  return {
    job_id: id,
    count: 1,
    next_cursor: null,
    chunks: [
      {
        chunk_index: start,
        state: done ? "done" : leased ? "leased" : "pending",
        first_item: start * 250,
        item_count: 250,
        node_id: done || leased ? node : null,
        attempt_id: done || leased ? `att_split_${String(start % 3)}` : null,
        attempts: done ? (start === 4 ? 2 : 1) : leased ? 1 : 0,
        speculations: 0,
        done_at: done ? iso((SPLIT_TOTAL - start) * 9_000) : null,
        result_digest: done ? `sha256:${(start * 7919).toString(16).padStart(8, "0")}${"a1".repeat(28)}` : null,
        failed_kind: null,
        failed_nodes: done && start === 4 ? ["node_demo_old"] : [],
        leases: leased ? [{ lease_seq: 1, node_id: node, speculative: false, leased_at: iso(4_000) }] : [],
      },
    ],
  };
}

/** `GET /v1/jobs/{id}/chunks/{i}/tries`: chunk 4 failed once on old-desktop before it committed. */
export function demoChunkTries(id: string, index: number) {
  const page = demoChunks(id, index);
  const chunk = page?.chunks[0];
  if (chunk === undefined) return undefined;
  const one = (seq: number, node: string, outcome: string, extra: Record<string, unknown> = {}) => ({
    attempt_id: `att_split_${String(index % 3)}`,
    lease_seq: seq,
    node_id: node,
    outcome,
    won: outcome === "committed",
    speculative: false,
    leased_at: iso(60_000),
    ended_at: outcome === "committed" ? iso(40_000) : null,
    stage_ms: 410,
    env_ms: 120,
    run_ms: 10_800,
    upload_ms: 95,
    bytes_in: 2_400_000,
    bytes_out: 310_000,
    exit_code: outcome === "committed" ? 0 : null,
    failure_class: outcome === "committed" ? "none" : "node",
    failure_type: null,
    result_digest: outcome === "committed" ? chunk.result_digest : null,
    stderr_tail: null,
    ...extra,
  });
  const tries =
    chunk.state === "pending"
      ? []
      : index === 4
        ? [
            one(1, "node_demo_old", "failed", { exit_code: 137, failure_type: "resource_exhausted", stderr_tail: "frame 1003: decoding…\nKilled (out of memory)" }),
            one(2, chunk.node_id ?? "node_demo_asha", "committed"),
          ]
        : [one(1, chunk.node_id ?? "node_demo_asha", chunk.state === "done" ? "committed" : "released", chunk.state === "done" ? {} : { ended_at: null, run_ms: null, upload_ms: null, outcome: "released" })];
  return { job_id: id, chunk_index: index, count: tries.length, tries };
}

/* ------------------------------------------------------------------ logs */

const LOG_SCRIPT: Record<string, string[]> = {
  job_demo_split: [
    "[hive] split: 30 chunks x 250 items over asha-laptop, lab-pc-07, ravi-thinkpad",
    "[lane asha-laptop] env ready (python 3.11, numpy 1.26, pillow 10.3)",
    "[lane lab-pc-07] env ready (python 3.11, numpy 1.26, pillow 10.3)",
    "[lane ravi-thinkpad] env ready (python 3.11, numpy 1.26, pillow 10.3)",
  ],
  job_demo_send: [
    "[hive] sent to lab-pc-07 (est. 6 m 40 s; local would take 14 m)",
    "staging tree 7c4e1a90b2… from the controller: 184 MiB in 6.4 s",
    "Using device: cuda:0 (NVIDIA GeForce RTX 4070)",
    "Loading CIFAR-10: 50000 train / 10000 test",
  ],
  job_demo_local: [
    "[hive] stayed local on asha-laptop (sending it away would add 8 s)",
    "ruff check . -> All checks passed!",
    "pytest -q",
    "........................................................ [ 61%]",
    "....................................                     [100%]",
    "92 passed in 9.84s",
  ],
};

/** The lines a running demo job keeps printing (one every ~1.4 s). */
function liveLine(id: string, n: number): string {
  if (id === "job_demo_split") return `[lane ${["asha-laptop", "lab-pc-07", "ravi-thinkpad"][n % 3] ?? ""}] chunk #${String(18 + Math.floor(n / 3))}: frame ${String(4500 + n * 25)}/7500 rendered`;
  const epoch = 1 + Math.floor(n / 10);
  return `epoch ${String(epoch)} step ${String(((n % 10) + 1) * 39)}/390  loss ${(1.9 / (1 + n * 0.04)).toFixed(4)}  acc ${(0.31 + Math.min(0.6, n * 0.006)).toFixed(3)}`;
}

/** A demo job's log: its opening lines, whether it is still running, and how it ended. */
export function demoLog(id: string): { lines: string[]; live: boolean; end: Record<string, unknown> | null; liveLine: (n: number) => string } | undefined {
  const detail = demoJobDetail(id);
  if (detail === undefined) return undefined;
  const job = detail as Record<string, unknown>;
  const text = (v: unknown, fallback: string) => (typeof v === "string" ? v : fallback);
  const state = text(job.state, "running");
  if (state === "running") return { lines: LOG_SCRIPT[id] ?? [], live: true, end: null, liveLine: (n) => liveLine(id, n) };
  const failed = state === "failed";
  const lines = LOG_SCRIPT[id] ?? [
    `[hive] ${text(job.name, id)} started on ${failed ? "its node" : "asha-laptop"}`,
    "loading inputs…",
    ...(failed ? [text(job.failure_detail, "exit 1")] : ["done"]),
  ];
  const exit = (detail.attempts[0] as { exit_code?: number | null } | undefined)?.exit_code ?? null;
  return {
    lines,
    live: false,
    end: { end: true, state, attempt_state: failed ? "failed" : "done", exit_code: exit, failure_type: failed ? (job.failure_type ?? null) : null, final: { stdout: true, stderr: true }, gaps: {} },
    liveLine: () => "",
  };
}
