/** View helpers of the Benchmarks pages (pure, unit-tested). */
import type { StatusTone } from "../../lib/status";
import type { CampaignState, CampaignVerb, ConfigLabel, GroupLabel, LabelsOut, RunCounts, RunState } from "./benchApi";

const CAMPAIGN_TONES: Record<CampaignState, StatusTone> = {
  active: "busy",
  paused: "degraded",
  done: "ok",
  cancelled: "unknown",
};

const RUN_TONES: Record<RunState, StatusTone> = {
  planned: "unknown",
  submitted: "busy",
  done: "ok",
  failed: "failed",
  skipped: "unknown",
};

const LABEL_TONES: Record<GroupLabel["label"], StatusTone | "neutral"> = {
  best: "ok",
  tie: "busy",
  insufficient: "degraded",
  pending: "neutral",
};

export function campaignTone(state: string): StatusTone {
  return (CAMPAIGN_TONES as Record<string, StatusTone | undefined>)[state] ?? "unknown";
}

export function runTone(state: string): StatusTone {
  return (RUN_TONES as Record<string, StatusTone | undefined>)[state] ?? "unknown";
}

export function labelTone(label: string): StatusTone | "neutral" {
  return (LABEL_TONES as Record<string, StatusTone | "neutral" | undefined>)[label] ?? "neutral";
}

/**
 * The controls a campaign offers (bench/control.py state machine):
 * active, not yet started -> Start (hand it to the runner); active and
 * runner-owned -> Pause; paused -> Resume; any open state -> Cancel.
 * `runnerOwned` unknown (status not loaded): Start is offered - it is
 * idempotent on the server. A finished campaign offers nothing.
 */
export function campaignVerbs(state: CampaignState, runnerOwned: boolean | undefined): CampaignVerb[] {
  switch (state) {
    case "active":
      return runnerOwned === true ? ["pause", "cancel"] : ["start", "cancel"];
    case "paused":
      return ["resume", "cancel"];
    default:
      return [];
  }
}

export const VERB_LABELS: Record<CampaignVerb, string> = {
  start: "Start",
  pause: "Pause",
  resume: "Resume",
  cancel: "Cancel",
};

export const VERB_DONE: Record<CampaignVerb, string> = {
  start: "Campaign started: the controller's runner submits its runs",
  pause: "Campaign paused: running jobs finish, nothing new is submitted",
  resume: "Campaign resumed",
  cancel: "Campaign cancelled",
};

/** Seconds -> "45 s", "12 min", "3 h 05 min", "2 d 4 h"; null -> "—". */
export function formatEta(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${String(s)} s`;
  if (s < 3600) return `${String(Math.round(s / 60))} min`;
  if (s < 86_400) {
    const h = Math.floor(s / 3600);
    const m = Math.round((s % 3600) / 60);
    return m === 60 ? `${String(h + 1)} h 00 min` : `${String(h)} h ${String(m).padStart(2, "0")} min`;
  }
  return `${String(Math.floor(s / 86_400))} d ${String(Math.floor((s % 86_400) / 3600))} h`;
}

/** A fraction (0..1) as a percentage with 1 decimal: 0.0123 -> "1.2 %"; null -> "—". */
export function formatFraction(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)} %`;
}

/** A server percentage (0..100, one decimal) as printed: 37.5 -> "37.5 %". */
export function formatDonePercent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value.toFixed(1)} %`;
}

/** A margin in % (labels: the runner-up's median over the best's): 12.345 -> "12.3 %". */
export function formatMargin(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value.toFixed(1)} %`;
}

export interface SpecBudget {
  maxRuns: number | null;
  maxHours: number | null;
}

function scalar(text: string | undefined): number | null {
  if (text === undefined) return null;
  const n = Number(text.trim().replace(/^["']|["']$/g, ""));
  return Number.isFinite(n) ? n : null;
}

/**
 * The spec's `budget` (max_runs, max_hours), read from the stored YAML text -
 * the campaign API has no separate field for it. Handles the two forms the
 * spec docs use: a flow map (`budget: {max_runs: 40, max_hours: 2}`) and a
 * block map (indented `max_runs: 40` lines). Anything else -> null (the page
 * then points at the spec text, which it shows too); never a guess.
 */
export function specBudget(yaml: string): SpecBudget | null {
  const lines = yaml.split(/\r?\n/);
  const at = lines.findIndex((l) => /^budget\s*:/.test(l));
  if (at < 0) return null;
  const head = (lines[at] ?? "").replace(/#.*$/, "");
  const rest = head.slice(head.indexOf(":") + 1).trim();
  const fields = new Map<string, string>();
  if (rest.startsWith("{")) {
    if (!rest.endsWith("}")) return null;
    for (const part of rest.slice(1, -1).split(",")) {
      const colon = part.indexOf(":");
      if (colon < 0) continue;
      fields.set(part.slice(0, colon).trim(), part.slice(colon + 1).trim());
    }
  } else if (rest === "") {
    for (const line of lines.slice(at + 1)) {
      if (line.trim() === "" || /^\s*#/.test(line)) continue;
      const m = /^\s+([a-z_]+)\s*:\s*([^#]*)/.exec(line);
      if (m === null) break;
      fields.set(m[1] ?? "", m[2] ?? "");
    }
  } else {
    return null;
  }
  const maxRuns = scalar(fields.get("max_runs"));
  const maxHours = scalar(fields.get("max_hours"));
  if (maxRuns === null && maxHours === null) return null;
  return { maxRuns, maxHours };
}

/* ------------------------------------------------------------ presentation (restyle) */

const TONE_LIGHT: Record<StatusTone, string> = {
  ok: "bg-status-ok",
  busy: "bg-status-busy",
  degraded: "bg-status-degraded",
  failed: "bg-status-failed",
  unknown: "bg-status-unknown",
};

/** The flat state light's colour class of a campaign / run state. */
export function campaignLight(state: string): string {
  return TONE_LIGHT[campaignTone(state)];
}

export function runLight(state: string): string {
  return TONE_LIGHT[runTone(state)];
}

export function labelLight(label: string): string {
  const tone = labelTone(label);
  return tone === "neutral" ? "bg-fg-subtle" : TONE_LIGHT[tone];
}

/** The campaign strategies in the dashboard's words (the API's `local | mode1 | mode3`). */
export const STRATEGY_TEXT: Record<string, string> = { local: "local", mode1: "sent away", mode3: "split" };

export function strategyText(strategy: string | null | undefined): string {
  return strategy == null ? "—" : (STRATEGY_TEXT[strategy] ?? strategy);
}

/** The list's state tabs. Queued = active, but no run has left `planned` yet. */
export const LIST_TABS = [
  { key: "all", label: "All" },
  { key: "running", label: "Running" },
  { key: "queued", label: "Queued" },
  { key: "paused", label: "Paused" },
  { key: "done", label: "Done" },
  { key: "cancelled", label: "Cancelled" },
] as const;
export type ListTab = (typeof LIST_TABS)[number]["key"];

export function isListTab(value: string | null): value is ListTab {
  return value !== null && LIST_TABS.some((t) => t.key === value);
}

export function listTabOf(campaign: { state: string; counts: RunCounts }): Exclude<ListTab, "all"> {
  if (campaign.state === "active") {
    const c = campaign.counts;
    return c.submitted + c.done + c.failed + c.skipped === 0 ? "queued" : "running";
  }
  return campaign.state === "paused" ? "paused" : campaign.state === "cancelled" ? "cancelled" : "done";
}

export interface SpeedUpResult {
  factor: number;
  groupId: string;
  family: string;
  params: Record<string, unknown>;
  baseline: ConfigLabel;
  best: ConfigLabel;
}

/**
 * The campaign's best measured speed-up, from its labels (no new statistics:
 * a ratio of two server medians). Per settled group (label best or tie): the
 * baseline is the run kept local on the laptop that submits it (strategy
 * `local` on `origin_node_id`), the best is the lowest median; the factor is
 * baseline ÷ best. The largest over the groups, or null when no group has a
 * measured baseline.
 */
export function bestSpeedUp(labels: LabelsOut): SpeedUpResult | null {
  let out: SpeedUpResult | null = null;
  for (const g of labels.groups) {
    if (g.label !== "best" && g.label !== "tie") continue;
    const measured = g.configs.filter((c) => c.state === "measured" && c.total_ms != null && c.total_ms.median > 0);
    const baseline = measured.find((c) => c.strategy === "local" && c.node_id === c.origin_node_id);
    if (baseline?.total_ms == null) continue;
    const best = measured.reduce((a, b) => ((b.total_ms?.median ?? Infinity) < (a.total_ms?.median ?? Infinity) ? b : a), baseline);
    const factor = baseline.total_ms.median / (best.total_ms?.median ?? baseline.total_ms.median);
    if (out === null || factor > out.factor) out = { factor, groupId: g.group_id, family: g.family, params: g.params, baseline, best };
  }
  return out;
}

/** Groups where the planner's preferred node was the best one, of the groups with a verdict. */
export function plannerAgreement(labels: LabelsOut): { agrees: number; judged: number } {
  let agrees = 0;
  let judged = 0;
  for (const g of labels.groups) {
    if (g.planner_agrees === null) continue;
    judged += 1;
    if (g.planner_agrees) agrees += 1;
  }
  return { agrees, judged };
}

/** A speed-up factor as printed: 3.4167 -> "3.42". */
export function formatFactor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value >= 10 ? value.toFixed(1) : value.toFixed(2);
}

/** One option of a group in words: "local on asha-laptop", "sent away → lab-pc-07", "split, led by asha-laptop". */
export function optionText(strategy: string | null | undefined, node: string): string {
  if (strategy === "local") return `local on ${node}`;
  if (strategy === "mode3") return `split, led by ${node}`;
  if (strategy === "mode1") return `sent away → ${node}`;
  return `${strategy ?? "?"} → ${node}`;
}
