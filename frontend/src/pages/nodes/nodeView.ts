/**
 * Derived views of `GET /v1/nodes` rows used by the Overview and the Nodes
 * pages. Pure and unit-tested: every aggregate the UI prints is computed here
 * from API fields only (checklist 26) - nothing is estimated or invented.
 */
import type { NodeSummary } from "../../api/endpoints";
import type { StatusTone } from "../../lib/status";
import { nodeTone } from "../../lib/status";

/** Node states in display order (most useful first). */
export const NODE_STATE_ORDER = ["online", "suspect", "draining", "enrolling", "offline", "revoked"] as const;

export type NodeStateName = (typeof NODE_STATE_ORDER)[number];

/** `state -> count`, every state present in the list (and only those). */
export function countByState(nodes: readonly NodeSummary[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const node of nodes) counts.set(node.state, (counts.get(node.state) ?? 0) + 1);
  return new Map(
    [...counts.entries()].sort(([a], [b]) => stateRank(a) - stateRank(b) || a.localeCompare(b)),
  );
}

function stateRank(state: string): number {
  const i = (NODE_STATE_ORDER as readonly string[]).indexOf(state);
  return i < 0 ? NODE_STATE_ORDER.length : i;
}

export interface FabricHealth {
  tone: StatusTone;
  headline: string;
  online: number;
  total: number;
}

/** One line about the fabric as a whole: revoked nodes do not count as members. */
export function fabricHealth(nodes: readonly NodeSummary[]): FabricHealth {
  const members = nodes.filter((n) => n.state !== "revoked");
  const total = members.length;
  const online = members.filter((n) => n.state === "online").length;
  if (total === 0) return { tone: "unknown", headline: "No nodes yet", online, total };
  if (online === total) {
    return { tone: "ok", headline: `All ${String(total)} node${total === 1 ? "" : "s"} online`, online, total };
  }
  if (online === 0) return { tone: "failed", headline: `No node online (0 of ${String(total)})`, online, total };
  return { tone: "degraded", headline: `${String(online)} of ${String(total)} nodes online`, online, total };
}

export interface Utilization {
  /** Nodes with a live sample (connected and reporting). */
  reporting: number;
  /** Mean CPU % over the reporting nodes. */
  cpuMean: number | null;
  ramUsed: number;
  ramTotal: number;
  gpuCount: number;
  gpuUtilMean: number | null;
  vramUsed: number;
  vramTotal: number;
}

/** Aggregate live usage over the nodes that report a sample right now. */
export function utilization(nodes: readonly NodeSummary[]): Utilization {
  const live = nodes.filter((n) => n.connected && n.cpu_percent != null);
  const gpus = live.flatMap((n) => n.gpus);
  const sum = (values: number[]) => values.reduce((a, b) => a + b, 0);
  return {
    reporting: live.length,
    cpuMean: live.length === 0 ? null : sum(live.map((n) => n.cpu_percent ?? 0)) / live.length,
    ramUsed: sum(live.map((n) => n.ram_used_bytes ?? 0)),
    ramTotal: sum(live.map((n) => n.ram_total_bytes ?? 0)),
    gpuCount: gpus.length,
    gpuUtilMean: gpus.length === 0 ? null : sum(gpus.map((g) => g.util_percent)) / gpus.length,
    vramUsed: sum(gpus.map((g) => g.vram_used_bytes)),
    vramTotal: sum(gpus.map((g) => g.vram_total_bytes)),
  };
}

export function stateTone(node: Pick<NodeSummary, "state">): StatusTone {
  return nodeTone(node.state);
}

/** `hostname -> node`, for naming nodes that other rows refer to by id. */
export function hostnames(nodes: readonly NodeSummary[] | undefined): Map<string, string> {
  return new Map((nodes ?? []).map((n) => [n.node_id, n.hostname]));
}

export type NodeSortKey = "name" | "state" | "cpu" | "memory" | "gpu" | "heartbeat";

function memPercent(n: NodeSummary): number {
  return n.ram_used_bytes != null && n.ram_total_bytes ? n.ram_used_bytes / n.ram_total_bytes : -1;
}

function gpuUtil(n: NodeSummary): number {
  const gpus = n.gpus;
  return gpus.length === 0 ? -1 : gpus.reduce((a, g) => a + g.util_percent, 0) / gpus.length;
}

/** Sorted copy; an unknown value counts as the lowest; ties fall back to the name. */
export function sortNodes(
  nodes: readonly NodeSummary[],
  key: NodeSortKey,
  direction: "ascending" | "descending",
): NodeSummary[] {
  const value = (n: NodeSummary): number | string => {
    switch (key) {
      case "name":
        return n.hostname.toLowerCase();
      case "state":
        return stateRank(n.state);
      case "cpu":
        return n.cpu_percent ?? -1;
      case "memory":
        return memPercent(n);
      case "gpu":
        return gpuUtil(n);
      case "heartbeat":
        return Date.parse(n.last_seen_at) || 0;
    }
  };
  const sign = direction === "ascending" ? 1 : -1;
  return [...nodes].sort((a, b) => {
    const va = value(a);
    const vb = value(b);
    const cmp = typeof va === "string" && typeof vb === "string" ? va.localeCompare(vb) : Number(va) - Number(vb);
    return cmp !== 0 ? sign * cmp : a.hostname.localeCompare(b.hostname) || a.node_id.localeCompare(b.node_id);
  });
}
