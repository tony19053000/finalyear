/**
 * Pure views of a job for the Jobs pages (list rows, detail header and the
 * Progress / Plan tabs). Every figure comes from controller fields: the
 * planner's own estimates (`GET /v1/jobs/{id}/decisions`), a split job's
 * chunk counts (`GET /v1/jobs/{id}/split`) and the job's timestamps. A figure
 * without its inputs is `null` and the page prints "—".
 */
import type { StatusTone } from "../../lib/status";
import { jobTone } from "../../lib/status";
import {
  durationParts,
  estimatedPercent,
  placingDecision,
  speedUp,
  splitPercent,
  type PlacementMode,
  type SpeedUp,
} from "../overview/overviewModel";
import type { DecisionOut, JobDetail, JobState, JobSummary } from "./jobsApi";
import { TERMINAL_JOB_STATES } from "./jobsApi";
import type { SplitProgress } from "./splitApi";

/* ------------------------------------------------------------- state tabs */

export interface StateTab {
  key: string;
  label: string;
  /** The API states the tab lists (empty = every state). */
  states: readonly JobState[];
}

/**
 * The list's text tabs. Every one of the API's ten job states belongs to
 * exactly one tab: Running = preparing / running / collecting / retrying,
 * Queued = submitted / planning / queued.
 */
export const STATE_TABS: readonly StateTab[] = [
  { key: "all", label: "All", states: [] },
  { key: "running", label: "Running", states: ["preparing", "running", "collecting", "retrying"] },
  { key: "queued", label: "Queued", states: ["submitted", "planning", "queued"] },
  { key: "succeeded", label: "Succeeded", states: ["succeeded"] },
  { key: "failed", label: "Failed", states: ["failed"] },
  { key: "cancelled", label: "Cancelled", states: ["cancelled"] },
];

/** The tab a state filter is exactly (null: a hand-made filter no tab matches). */
export function tabOf(states: readonly JobState[]): StateTab | null {
  return (
    STATE_TABS.find((t) => t.states.length === states.length && t.states.every((s) => states.includes(s))) ?? null
  );
}

/** Jobs per tab among `jobs` (the "All" tab counts every job). */
export function countByTab(jobs: readonly Pick<JobSummary, "state">[]): Map<string, number> {
  const out = new Map<string, number>(STATE_TABS.map((t) => [t.key, 0]));
  for (const job of jobs) {
    for (const tab of STATE_TABS) {
      if (tab.states.length === 0 || tab.states.includes(job.state)) out.set(tab.key, (out.get(tab.key) ?? 0) + 1);
    }
  }
  return out;
}

/* ------------------------------------------------------------ state light */

const TONE_LIGHT: Record<StatusTone, string> = {
  ok: "bg-status-ok",
  busy: "bg-status-busy",
  degraded: "bg-status-degraded",
  failed: "bg-status-failed",
  unknown: "bg-status-unknown",
};

/** The flat state light's colour class for a job state. */
export function stateLight(state: string): string {
  return TONE_LIGHT[jobTone(state)];
}

export function isTerminal(state: string): boolean {
  return TERMINAL_JOB_STATES.has(state);
}

/** Work is happening (not waiting, not ended): the light breathes, estimates run. */
export function isWorking(state: string): boolean {
  return state === "preparing" || state === "running" || state === "collecting";
}

/* ------------------------------------------------------------------ plan */

export const MODE_TEXT: Record<PlacementMode, string> = { local: "Local", offload: "Sent away", split: "Split" };

export interface PlanView {
  /** How the job runs: the planner's chosen option, else a pinned placement, else null (not placed yet). */
  mode: PlacementMode | null;
  /** The node(s) it runs on (a split's node set). */
  nodeIds: string[];
  /** The planner's estimates behind the speed-up (null without a decision). */
  speed: SpeedUp | null;
}

export function planView(
  job: Pick<JobSummary, "placement">,
  decisions: readonly DecisionOut[] | undefined,
  predictedS?: number | null,
): PlanView {
  const decision = decisions === undefined ? undefined : placingDecision(decisions);
  const speed = decision === undefined ? null : speedUp(decision, predictedS);
  if (speed !== null) {
    const ids = speed.mode === "split" ? speed.nodeSet : speed.targetNodeId !== null ? [speed.targetNodeId] : [];
    return { mode: speed.mode, nodeIds: ids, speed };
  }
  const placement = job.placement;
  const pinned = placement.mode === "auto" ? null : placement.mode;
  const ids =
    decision?.chosen_node_id != null
      ? [decision.chosen_node_id]
      : placement.node_id != null
        ? [placement.node_id]
        : [...(placement.node_set ?? [])];
  return { mode: pinned, nodeIds: ids, speed: null };
}

/** The speed-up as a figure + its small label: "3.2" "×", or "12" "s saved". */
export function speedFigure(speed: SpeedUp | null): { figure: string; unit: string; label: string } | null {
  if (speed === null) return null;
  if (speed.mode === "local") {
    if (speed.savedS === null) return null;
    const [n, u] = durationParts(speed.savedS);
    return { figure: n.replace(/\.0$/, ""), unit: u, label: "saved" };
  }
  if (speed.factor === null) return null;
  return { figure: speed.factor >= 10 ? speed.factor.toFixed(0) : speed.factor.toFixed(1), unit: "×", label: "faster" };
}

/* -------------------------------------------------------------- progress */

/** When the work started: the first move into preparing / running, else the submit time. */
export function startedAt(job: Pick<JobDetail, "created_at"> & Partial<Pick<JobDetail, "transitions">>): string {
  const first = [...(job.transitions ?? [])]
    .sort((a, b) => a.seq - b.seq)
    .find((t) => t.to_state === "preparing" || t.to_state === "running");
  return first?.at ?? job.created_at;
}

export interface ProgressView {
  /** 0..100, or null when nothing honest can be said. */
  percent: number | null;
  /** True when the percent is time ÷ the planner's estimate (capped below 100 until the job ends). */
  estimated: boolean;
}

/**
 * Split: real % from chunk counts. Succeeded: 100. Local / sent away while
 * working: time since the work started ÷ the planner's estimate ("est.").
 * Waiting, failed, cancelled: null.
 */
export function progressOf(
  job: Pick<JobSummary, "state" | "created_at"> & Partial<Pick<JobDetail, "transitions">>,
  plan: PlanView,
  split: Pick<SplitProgress, "chunks"> | undefined,
  nowMs: number,
): ProgressView {
  if (job.state === "succeeded") return { percent: 100, estimated: false };
  if (plan.mode === "split" && split !== undefined) {
    const pct = splitPercent(split);
    if (pct !== null) return { percent: pct, estimated: false };
  }
  if (isTerminal(job.state) || !isWorking(job.state) || plan.mode === "split") return { percent: null, estimated: false };
  const pct = estimatedPercent(startedAt(job), plan.speed?.chosenS ?? null, nowMs);
  return { percent: pct, estimated: pct !== null };
}

/** Milliseconds from submit to the end (a finished job) or to now. */
export function durationMs(job: Pick<JobSummary, "state" | "created_at" | "updated_at">, nowMs: number): number | null {
  const from = Date.parse(job.created_at);
  if (Number.isNaN(from)) return null;
  const to = isTerminal(job.state) ? Date.parse(job.updated_at) : nowMs;
  return Number.isNaN(to) ? null : Math.max(0, to - from);
}

/** Names for node ids (display name, else hostname, else the id). */
export function nodeNames(nodes: readonly { node_id: string; hostname: string; display_name?: string | null }[]): Map<string, string> {
  return new Map(nodes.map((n) => [n.node_id, n.display_name ? n.display_name : n.hostname]));
}
