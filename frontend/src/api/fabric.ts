/**
 * Calls and query hooks of the Overview and Nodes pages (V0.8 G3): node
 * detail, live metrics, network matrix, revoke, alerts, controller health and
 * the active-job counts. Types come from schema.d.ts (D171).
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiRequest } from "./client";
import type { components } from "./schema";
import { queryKeys } from "./queryKeys";

type Schemas = components["schemas"];
export type NodeDetail = Schemas["NodeDetail"];
export type NodeMetrics = Schemas["NodeMetrics"];
export type MetricPoint = Schemas["MetricPoint"];
export type NodeResources = Schemas["NodeResources"];
export type NetworkOut = Schemas["NetworkOut"];
export type NodeRevoked = Schemas["NodeRevoked"];
export type AlertList = Schemas["AlertList"];
export type AlertOut = Schemas["AlertOut"];
export type Health = Schemas["Health"];
export type JobList = Schemas["JobList"];
export type JobState = Schemas["JobState"];

/** Seconds of live samples a node page shows (the controller ring holds ~5 min, D173). */
export const METRICS_WINDOW_S = 300;
/** Alerts per page on the Overview. */
export const ALERTS_PAGE = 20;
/** The most active jobs counted in one request (the list's maximum page). */
export const ACTIVE_JOBS_LIMIT = 500;
/** Job states that are not terminal: the Overview counts these. */
export const ACTIVE_JOB_STATES: readonly JobState[] = [
  "submitted",
  "planning",
  "queued",
  "preparing",
  "running",
  "collecting",
  "retrying",
];

/* ------------------------------------------------------------------ calls */

export function getNode(nodeId: string, signal?: AbortSignal): Promise<NodeDetail> {
  return apiRequest<NodeDetail>(`/v1/nodes/${encodeURIComponent(nodeId)}`, { signal });
}

export function getNodeMetrics(nodeId: string, signal?: AbortSignal): Promise<NodeMetrics> {
  return apiRequest<NodeMetrics>(`/v1/nodes/${encodeURIComponent(nodeId)}/metrics`, {
    signal,
    query: { window: METRICS_WINDOW_S },
  });
}

/** Only the newest live sample (its disks); history is V0.9's. */
export function getNodeResources(nodeId: string, signal?: AbortSignal): Promise<NodeResources> {
  return apiRequest<NodeResources>(`/v1/nodes/${encodeURIComponent(nodeId)}/resources`, {
    signal,
    query: { limit: 1, live_limit: 1 },
  });
}

export function getNetwork(signal?: AbortSignal): Promise<NetworkOut> {
  return apiRequest<NetworkOut>("/v1/network", { signal });
}

export function revokeNode(nodeId: string): Promise<NodeRevoked> {
  return apiRequest<NodeRevoked>(`/v1/nodes/${encodeURIComponent(nodeId)}/revoke`, { method: "POST" });
}

export function listAlerts(before: string | undefined, signal?: AbortSignal): Promise<AlertList> {
  return apiRequest<AlertList>("/v1/alerts", { signal, query: { limit: ALERTS_PAGE, before } });
}

export function getHealth(signal?: AbortSignal): Promise<Health> {
  return apiRequest<Health>("/healthz", { signal });
}

export function listActiveJobs(signal?: AbortSignal): Promise<JobList> {
  return apiRequest<JobList>("/v1/jobs", {
    signal,
    query: { state: ACTIVE_JOB_STATES, limit: ACTIVE_JOBS_LIMIT },
  });
}

/* ------------------------------------------------------------------ hooks */

export function useNode(nodeId: string) {
  return useQuery({ queryKey: queryKeys.node(nodeId), queryFn: ({ signal }) => getNode(nodeId, signal) });
}

/**
 * The node's live samples: fetched once, then appended from `node.metrics`
 * events (events/applyEvent.ts) - never polled (D172/D173).
 */
export function useNodeMetrics(nodeId: string) {
  return useQuery({
    queryKey: queryKeys.nodeMetrics(nodeId),
    queryFn: ({ signal }) => getNodeMetrics(nodeId, signal),
    staleTime: Infinity,
  });
}

export function useNodeResources(nodeId: string) {
  return useQuery({
    queryKey: queryKeys.nodeResources(nodeId),
    queryFn: ({ signal }) => getNodeResources(nodeId, signal),
  });
}

export function useNetwork() {
  return useQuery({ queryKey: queryKeys.network, queryFn: ({ signal }) => getNetwork(signal) });
}

export function useHealth() {
  return useQuery({ queryKey: queryKeys.health, queryFn: ({ signal }) => getHealth(signal) });
}

export function useActiveJobs() {
  return useQuery({ queryKey: queryKeys.activeJobs, queryFn: ({ signal }) => listActiveJobs(signal) });
}

/** Admin only (`enabled` false for a user: the call would be a 403). */
export function useAlerts(enabled: boolean) {
  return useInfiniteQuery({
    queryKey: queryKeys.alerts,
    queryFn: ({ pageParam, signal }) => listAlerts(pageParam, signal),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled,
  });
}

export function useRevokeNode(nodeId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => revokeNode(nodeId),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: queryKeys.nodes, exact: true });
      void client.invalidateQueries({ queryKey: queryKeys.node(nodeId), exact: true });
    },
  });
}
