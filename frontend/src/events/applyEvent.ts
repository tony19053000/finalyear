/**
 * What a fabric event does to the TanStack Query cache (D172: the client
 * keeps no second store - it patches/invalidates the cache the pages read).
 *
 * - `node.status` {node_id, state, connected}: patch the node in the list and
 *   its detail at once (the page changes within one frame), then invalidate
 *   both so the authoritative row follows.
 * - `node.metrics` (<= 1 Hz per node): patched in place, never refetched -
 *   the node's newest numbers in the list/detail (not on a row already
 *   marked disconnected - D16), one point merged in time order into its
 *   sparkline series (capped to the metrics window, measured back from the
 *   controller's clock), and "last heartbeat" moved to the event's time.
 * - `job.state` / `attempt.state`: invalidate that job, the plain jobs list
 *   and the Overview's active-job counts. (The Jobs pages add their own
 *   applier - see `EVENT_APPLIERS` in app/AppShell.tsx.)
 * - `alert` (admins only): merged into the first alerts page by `id`, so the
 *   same alert from the list and the stream is shown once.
 * - unknown types are ignored, never an error.
 */
import type { InfiniteData, QueryClient } from "@tanstack/react-query";

import type { NodeList, NodeSummary } from "../api/endpoints";
import { METRICS_WINDOW_S, type AlertList, type AlertOut, type MetricPoint, type NodeDetail, type NodeMetrics } from "../api/fabric";
import { queryKeys } from "../api/queryKeys";
import { noteServerTime, serverNow } from "../lib/serverClock";
import type { FabricEvent } from "./eventStream";

/** One step of the event -> cache pipeline (the shell runs each in order). */
export type EventApplier = (client: QueryClient, event: FabricEvent) => void;

/** Points kept per node series: the window at the agents' 2 s report rate, plus slack. */
export const MAX_SERIES_POINTS = 200;

function str(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function num(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

type Gpu = NodeSummary["gpus"] extends readonly (infer G)[] | undefined ? G : never;

function gpusOf(value: unknown): Gpu[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const out: Gpu[] = [];
  for (const raw of value as unknown[]) {
    if (typeof raw !== "object" || raw === null) return undefined;
    const g = raw as Record<string, unknown>;
    const index = num(g.index);
    const util = num(g.util_percent);
    const used = num(g.vram_used_bytes);
    const total = num(g.vram_total_bytes);
    if (index === undefined || util === undefined || used === undefined || total === undefined) return undefined;
    out.push({ index, util_percent: util, vram_used_bytes: used, vram_total_bytes: total });
  }
  return out;
}

function patchNode<T extends NodeSummary>(node: T, patch: Partial<NodeSummary>): T {
  return { ...node, ...patch };
}

function patchNodeEverywhere(client: QueryClient, nodeId: string, patch: Partial<NodeSummary>): void {
  client.setQueryData<NodeList>(queryKeys.nodes, (list) =>
    list === undefined
      ? list
      : { ...list, nodes: list.nodes.map((n) => (n.node_id === nodeId ? patchNode(n, patch) : n)) },
  );
  client.setQueryData<NodeDetail>(queryKeys.node(nodeId), (detail) =>
    detail === undefined ? detail : patchNode(detail, patch),
  );
}

function applyNodeStatus(client: QueryClient, data: Record<string, unknown>): void {
  const nodeId = str(data.node_id);
  if (nodeId === undefined) return;
  const patch: Partial<NodeSummary> = {};
  if (typeof data.connected === "boolean") patch.connected = data.connected;
  const state = str(data.state);
  if (state !== undefined) patch.state = state as NodeSummary["state"];
  if (data.connected === false) {
    // No live stream: the live numbers are gone with it (D16: never shown as current).
    patch.cpu_percent = null;
    patch.ram_used_bytes = null;
    patch.gpus = [];
    patch.sample_age_s = null;
  }
  patchNodeEverywhere(client, nodeId, patch);
  void client.invalidateQueries({ queryKey: queryKeys.nodes, exact: true });
  void client.invalidateQueries({ queryKey: queryKeys.node(nodeId), exact: true });
}

function metricPoint(data: Record<string, unknown>): MetricPoint | undefined {
  const nodeId = str(data.node_id);
  const observedAt = str(data.observed_at);
  const cpu = num(data.cpu_percent);
  const ramUsed = num(data.ram_used_bytes);
  const ramTotal = num(data.ram_total_bytes);
  const gpus = gpusOf(data.gpus);
  if (
    nodeId === undefined ||
    observedAt === undefined ||
    cpu === undefined ||
    ramUsed === undefined ||
    ramTotal === undefined ||
    gpus === undefined
  ) {
    return undefined;
  }
  return {
    node_id: nodeId,
    observed_at: observedAt,
    cpu_percent: cpu,
    ram_used_bytes: ramUsed,
    ram_total_bytes: ramTotal,
    gpus,
    age_s: 0,
  };
}

/** A sample stamped further than this past the controller's "now" is an agent clock jump: kept out of the series. */
export const MAX_FUTURE_SKEW_S = 120;

/** A usable controller timestamp (seconds since the epoch), or undefined (missing, 0, negative, non-finite). */
function eventAtS(event: FabricEvent): number | undefined {
  return Number.isFinite(event.at) && event.at > 0 ? event.at : undefined;
}

/**
 * The node's series with `point` merged in: sorted by `observed_at` (a late
 * sample lands in place, an equal timestamp is a duplicate), cut to the
 * metrics window and the point cap. The window is measured back from the
 * newest *plausible* time - never later than the controller's clock - so one
 * sample stamped hours ahead (agent clock jump) cannot erase the history;
 * that sample itself is not kept.
 */
function mergeSample(series: NodeMetrics, point: MetricPoint, nowMs: number): NodeMetrics {
  const t = Date.parse(point.observed_at);
  if (Number.isNaN(t) || t > nowMs + MAX_FUTURE_SKEW_S * 1000) return series;
  const timed = series.samples
    .map((s) => ({ s, t: Date.parse(s.observed_at) }))
    .filter((x) => !Number.isNaN(x.t));
  if (timed.some((x) => x.t === t)) return series;
  timed.push({ s: point, t });
  timed.sort((a, b) => a.t - b.t);
  const newest = timed.at(-1)?.t ?? t;
  const cutoff = Math.min(newest, nowMs) - METRICS_WINDOW_S * 1000;
  const samples = timed
    .filter((x) => x.t >= cutoff)
    .map((x) => x.s)
    .slice(-MAX_SERIES_POINTS);
  return { ...series, samples, count: samples.length };
}

function applyNodeMetrics(client: QueryClient, event: FabricEvent): void {
  const point = metricPoint(event.data);
  if (point === undefined) return;
  const nodeId = point.node_id;
  const atS = eventAtS(event);
  const live: Partial<NodeSummary> = {
    cpu_percent: point.cpu_percent,
    ram_used_bytes: point.ram_used_bytes,
    ram_total_bytes: point.ram_total_bytes,
    gpus: point.gpus,
    sample_age_s: 0,
  };
  // The controller published this sample at `at`: its newest heartbeat.
  if (atS !== undefined) live.last_seen_at = new Date(atS * 1000).toISOString();
  // A row already marked disconnected keeps its empty live numbers (D16): a
  // sample that raced the disconnect is not "current". Reconnecting
  // (`node.status connected:true`, or the refetch) brings them back.
  const patchIfConnected = <T extends NodeSummary>(n: T): T => (n.connected ? patchNode(n, live) : n);
  client.setQueryData<NodeList>(queryKeys.nodes, (list) =>
    list === undefined
      ? list
      : { ...list, nodes: list.nodes.map((n) => (n.node_id === nodeId ? patchIfConnected(n) : n)) },
  );
  client.setQueryData<NodeDetail>(queryKeys.node(nodeId), (detail) =>
    detail === undefined ? detail : patchIfConnected(detail),
  );
  // History is still recorded: the sample was really measured then.
  client.setQueryData<NodeMetrics>(queryKeys.nodeMetrics(nodeId), (series) =>
    series === undefined ? series : mergeSample(series, point, serverNow()),
  );
}

function alertOf(event: FabricEvent): AlertOut | undefined {
  const id = str(event.data.id);
  const kind = str(event.data.kind);
  if (id === undefined || kind === undefined) return undefined;
  const nodeId = str(event.data.node_id) ?? null;
  const atS = eventAtS(event); // no usable `at`: stamp it with the controller's "now"
  const details: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(event.data)) {
    if (key !== "id" && key !== "kind" && key !== "node_id") details[key] = value;
  }
  return {
    id,
    at: new Date(atS === undefined ? serverNow() : atS * 1000).toISOString(),
    kind,
    node_id: nodeId,
    actor: nodeId === null ? "" : `node:${nodeId}`,
    details,
  };
}

function applyAlert(client: QueryClient, event: FabricEvent): void {
  const alert = alertOf(event);
  if (alert === undefined) {
    // An alert we cannot key: refetch rather than risk a duplicate.
    void client.invalidateQueries({ queryKey: queryKeys.alerts });
    return;
  }
  client.setQueryData<InfiniteData<AlertList, string | undefined>>(queryKeys.alerts, (data) => {
    if (data === undefined) return data; // not loaded (not admin, or not on the Overview)
    if (data.pages.some((page) => page.alerts.some((a) => a.id === alert.id))) return data;
    const [first, ...rest] = data.pages;
    if (first === undefined) return data;
    const alerts = [alert, ...first.alerts];
    return { ...data, pages: [{ ...first, alerts, count: alerts.length }, ...rest] };
  });
}

/** The fabric pages' applier (nodes, metrics, alerts, job counts). */
export function applyEvent(client: QueryClient, event: FabricEvent): void {
  // A frame without a usable `at` (missing -> 0, negative, NaN) says nothing about the controller's clock.
  const atS = eventAtS(event);
  if (atS !== undefined) noteServerTime(atS * 1000);
  switch (event.type) {
    case "node.status": {
      applyNodeStatus(client, event.data);
      return;
    }
    case "node.metrics": {
      applyNodeMetrics(client, event);
      return;
    }
    case "job.state":
    case "attempt.state": {
      const jobId = event.data.job_id;
      if (typeof jobId === "string") {
        void client.invalidateQueries({ queryKey: queryKeys.job(jobId) });
      }
      void client.invalidateQueries({ queryKey: queryKeys.jobs, exact: true });
      void client.invalidateQueries({ queryKey: queryKeys.activeJobs });
      return;
    }
    case "alert": {
      applyAlert(client, event);
      return;
    }
    default:
      return;
  }
}

/** A resync: everything shown may be stale - refetch all active queries. */
export function applyResync(client: QueryClient): void {
  void client.invalidateQueries();
}
