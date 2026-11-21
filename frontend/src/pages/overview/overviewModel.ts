/**
 * Pure views of the Overview's API data (frame B layout). Every figure the
 * Overview prints is computed here from controller fields only:
 *
 * - resources: `GET /v1/nodes` live numbers + each node's `hardware` (cores);
 * - speed-up: the planner's own estimates in `GET /v1/jobs/{id}/decisions`;
 * - split progress: `GET /v1/jobs/{id}/split` chunk counts.
 *
 * Nothing is invented: a figure without its inputs is `null` and the card
 * says so.
 */
import type { NodeSummary } from "../../api/endpoints";
import type { NodeDetail } from "../../api/fabric";
import type { CandidateOut, DecisionOut, JobSummary } from "../jobs/jobsApi";
import { TERMINAL_JOB_STATES } from "../jobs/jobsApi";
import type { SplitProgress } from "../jobs/splitApi";

/** A GPU counts as busy above this utilisation (%). */
export const GPU_BUSY_PERCENT = 10;

export type Resource = "cpu" | "ram" | "gpu";
export const RESOURCES: readonly Resource[] = ["cpu", "ram", "gpu"];
export const RESOURCE_LABEL: Record<Resource, string> = { cpu: "CPU", ram: "RAM", gpu: "GPU" };

/** Revoked nodes are not fabric members: never drawn, never counted. */
export function members(nodes: readonly NodeSummary[]): NodeSummary[] {
  return nodes.filter((n) => n.state !== "revoked");
}

/** A node reporting a live sample right now. */
export function isLive(n: NodeSummary): boolean {
  return n.connected && n.cpu_percent != null;
}

/** Logical cores from the node's Hello hardware (threads first: CPU % is over logical CPUs). */
export function coresOf(detail: Pick<NodeDetail, "hardware"> | undefined): number | null {
  const hw = detail?.hardware;
  if (hw === undefined) return null;
  for (const key of ["cpu_threads", "cpu_cores"]) {
    const v = hw[key];
    const n = typeof v === "number" ? v : typeof v === "string" ? Number(v) : NaN;
    if (Number.isFinite(n) && n > 0) return n;
  }
  return null;
}

function clampPct(v: number): number {
  return Math.max(0, Math.min(100, v));
}

/** One node's live usage in percent (null = not reported). */
export interface NodeLoad {
  cpu: number | null;
  ram: number | null;
  gpu: number | null;
}

export function nodeLoad(n: NodeSummary): NodeLoad {
  if (!isLive(n)) return { cpu: null, ram: null, gpu: null };
  const ram =
    n.ram_used_bytes != null && n.ram_total_bytes ? clampPct((n.ram_used_bytes / n.ram_total_bytes) * 100) : null;
  const gpu = n.gpus.length === 0 ? null : clampPct(n.gpus.reduce((a, g) => a + g.util_percent, 0) / n.gpus.length);
  return { cpu: clampPct(n.cpu_percent ?? 0), ram, gpu };
}

/** Mean of the reported resources (0..100), or null when nothing is reported. */
export function meanLoad(load: NodeLoad): number | null {
  const values = [load.cpu, load.ram, load.gpu].filter((v): v is number => v !== null);
  return values.length === 0 ? null : values.reduce((a, b) => a + b, 0) / values.length;
}

export interface ResourceTotals {
  /** Nodes with a live sample. */
  reporting: number;
  cpu: { used: number; total: number; percent: number } | null;
  ram: { used: number; total: number; percent: number } | null;
  gpu: { busy: number; total: number; percent: number; vramTotal: number } | null;
  /** Mean of the available resources' percentages. */
  average: number | null;
}

/**
 * Fabric totals over the live nodes. CPU: cores in use = Σ cpu% × cores (a
 * node whose core count is unknown is left out of the CPU figure). RAM:
 * Σ used / Σ total. GPU: busy when util > 10 %; its percent is the mean util.
 */
export function resourceTotals(nodes: readonly NodeSummary[], cores: ReadonlyMap<string, number>): ResourceTotals {
  const live = members(nodes).filter(isLive);
  let coreUsed = 0;
  let coreTotal = 0;
  for (const n of live) {
    const c = cores.get(n.node_id);
    if (c === undefined) continue;
    coreUsed += (clampPct(n.cpu_percent ?? 0) / 100) * c;
    coreTotal += c;
  }
  let ramUsed = 0;
  let ramTotal = 0;
  for (const n of live) {
    if (n.ram_used_bytes == null || !n.ram_total_bytes) continue;
    ramUsed += n.ram_used_bytes;
    ramTotal += n.ram_total_bytes;
  }
  const gpus = live.flatMap((n) => n.gpus);
  const cpu = coreTotal > 0 ? { used: coreUsed, total: coreTotal, percent: (coreUsed / coreTotal) * 100 } : null;
  const ram = ramTotal > 0 ? { used: ramUsed, total: ramTotal, percent: (ramUsed / ramTotal) * 100 } : null;
  const gpu =
    gpus.length > 0
      ? {
          busy: gpus.filter((g) => g.util_percent > GPU_BUSY_PERCENT).length,
          total: gpus.length,
          percent: clampPct(gpus.reduce((a, g) => a + g.util_percent, 0) / gpus.length),
          vramTotal: gpus.reduce((a, g) => a + g.vram_total_bytes, 0),
        }
      : null;
  const parts = [cpu?.percent, ram?.percent, gpu?.percent].filter((v): v is number => v !== undefined);
  return {
    reporting: live.length,
    cpu,
    ram,
    gpu,
    average: parts.length === 0 ? null : parts.reduce((a, b) => a + b, 0) / parts.length,
  };
}

/** Fabric-wide percentages at one instant (the line graph's point). */
export function fabricPercents(totals: ResourceTotals): NodeLoad {
  return { cpu: totals.cpu?.percent ?? null, ram: totals.ram?.percent ?? null, gpu: totals.gpu?.percent ?? null };
}

/* ------------------------------------------------------------ jobs / split */

export function isActive(job: Pick<JobSummary, "state">): boolean {
  return !TERMINAL_JOB_STATES.has(job.state);
}

/** The job the Speed-up card describes: the newest active job, else the newest job. */
export function focusJob(jobs: readonly JobSummary[]): JobSummary | undefined {
  return jobs.find(isActive) ?? jobs[0];
}

/** Split percent done from chunk counts (null when the job has no chunks yet). */
export function splitPercent(p: Pick<SplitProgress, "chunks">): number | null {
  return p.chunks.total > 0 ? (p.chunks.done / p.chunks.total) * 100 : null;
}

/** Distinct laptops that ran (or run) a lane of the split. */
export function splitLaptops(p: Pick<SplitProgress, "lanes">): number {
  return new Set(p.lanes.map((l) => l.node_id)).size;
}

/* --------------------------------------------------------------- speed-up */

export type PlacementMode = CandidateOut["mode"];

export const MODE_LABEL: Record<PlacementMode, string> = { local: "Local", offload: "Sent away", split: "Split" };

/** The decision that placed the job: the newest dispatched one, else the newest. */
export function placingDecision(decisions: readonly DecisionOut[]): DecisionOut | undefined {
  const byNewest = [...decisions].sort((a, b) => b.attempt_no - a.attempt_no || b.id - a.id);
  return byNewest.find((d) => d.outcome === "dispatched") ?? byNewest[0];
}

function chosenOf(decision: DecisionOut): CandidateOut | undefined {
  const ranked = decision.candidates.filter((c) => c.rank === 1);
  return (
    ranked.find((c) => c.node_id === decision.chosen_node_id) ??
    ranked[0] ??
    decision.candidates.find((c) => c.node_id === decision.chosen_node_id && c.feasible)
  );
}

function total(c: CandidateOut | undefined): number | null {
  const t = c?.estimate?.total_s;
  return t != null && Number.isFinite(t) && t > 0 ? t : null;
}

export interface SpeedUp {
  mode: PlacementMode;
  /** Split / Sent away: local estimate ÷ chosen estimate. */
  factor: number | null;
  /** Local: best non-local estimate − local estimate (seconds). */
  savedS: number | null;
  /** The node the work went to (a split's: its first node). */
  targetNodeId: string | null;
  /** A split's node set (empty otherwise). */
  nodeSet: string[];
  /** The chosen option's estimated total seconds (drives the est. progress bar). */
  chosenS: number | null;
}

/**
 * Estimated speed-up from the planner's own numbers. For a split the chosen
 * row's `estimate.total_s` is the predicted makespan (planner split_plan:
 * `predicted_makespan_s = picked.total_s`); `predictedS` (the split
 * summary's `predicted_s`) is used when that row has no estimate.
 */
export function speedUp(decision: DecisionOut, predictedS?: number | null): SpeedUp | null {
  const chosen = chosenOf(decision);
  if (chosen === undefined) return null;
  const local = decision.candidates.find((c) => c.mode === "local" && c.feasible && total(c) !== null);
  const localS = total(local);
  if (chosen.mode === "local") {
    const others = decision.candidates
      .filter((c) => c.mode !== "local" && c.feasible)
      .map(total)
      .filter((t): t is number => t !== null);
    const best = others.length === 0 ? null : Math.min(...others);
    const mine = total(chosen) ?? localS;
    return {
      mode: "local",
      factor: null,
      savedS: best !== null && mine !== null ? Math.max(0, best - mine) : null,
      targetNodeId: chosen.node_id,
      nodeSet: [],
      chosenS: mine,
    };
  }
  let chosenS = total(chosen);
  if (chosenS === null && chosen.mode === "split" && predictedS != null && predictedS > 0) chosenS = predictedS;
  return {
    mode: chosen.mode,
    factor: localS !== null && chosenS !== null ? localS / chosenS : null,
    savedS: null,
    targetNodeId: chosen.node_id,
    nodeSet: chosen.mode === "split" ? [...(chosen.node_set ?? [])] : [],
    chosenS,
  };
}

/* ----------------------------------------------------------------- alerts */

export interface FabricAlert {
  id: string;
  tone: "failed" | "degraded";
  title: string;
  detail: string;
  href: string;
  at: string;
}

/** Nodes offline or suspect, and failed jobs among the recent ones. */
export function fabricAlerts(nodes: readonly NodeSummary[], jobs: readonly JobSummary[]): FabricAlert[] {
  const out: FabricAlert[] = [];
  for (const n of members(nodes)) {
    if (n.state !== "offline" && n.state !== "suspect") continue;
    const name = n.display_name || n.hostname;
    out.push({
      id: `node:${n.node_id}`,
      tone: n.state === "offline" ? "failed" : "degraded",
      title: n.state === "offline" ? `${name} is offline` : `${name} is suspect`,
      detail: n.state === "offline" ? "No live session - its work is placed elsewhere." : "Heartbeats are late; the planner avoids it.",
      href: `/nodes/${encodeURIComponent(n.node_id)}`,
      at: n.last_seen_at,
    });
  }
  for (const j of jobs) {
    if (j.state !== "failed") continue;
    out.push({
      id: `job:${j.job_id}`,
      tone: "failed",
      title: `Job ${j.name} failed`,
      detail: j.failure_detail ?? j.failure_type ?? "See the job for its log.",
      href: `/jobs/${encodeURIComponent(j.job_id)}`,
      at: j.updated_at,
    });
  }
  return out;
}

/** A duration as [number, unit] for the big figure. */
export function durationParts(s: number): [string, string] {
  if (s < 10) return [s.toFixed(1), "s"];
  if (s < 100) return [String(Math.round(s)), "s"];
  if (s < 6000) return [(s / 60).toFixed(1), "min"];
  return [(s / 3600).toFixed(1), "h"];
}

/** Cap for an estimated bar: it never claims done before the job is. */
export const EST_CAP = 95;

/** Estimated % done from time since `startedIso` over the planner's estimate. */
export function estimatedPercent(startedIso: string, estimateS: number | null, nowMs: number): number | null {
  if (estimateS === null || estimateS <= 0) return null;
  const started = Date.parse(startedIso);
  if (!Number.isFinite(started)) return null;
  const pct = ((nowMs - started) / 1000 / estimateS) * 100;
  return Math.max(0, Math.min(EST_CAP, pct));
}
