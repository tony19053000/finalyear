/**
 * The Overview's queries. All of them share keys with the other pages (or
 * live under prefixes the event appliers already invalidate), so live events
 * move the Overview without extra wiring:
 *
 * - nodes: `queryKeys.nodes` (patched by node.status / node.metrics);
 * - node details (cores): `queryKeys.node(id)`;
 * - recent jobs: `["overview", "active-jobs", "recent"]` (job.state /
 *   attempt.state invalidate the `queryKeys.activeJobs` prefix);
 * - a job's detail, decisions and split summary: `jobKeys.*` (invalidated by
 *   job events; the split summary is also patched by split.progress).
 */
import { useQueries, useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { apiRequest } from "../../api/client";
import type { NodeSummary } from "../../api/endpoints";
import { getNode } from "../../api/fabric";
import { queryKeys } from "../../api/queryKeys";
import { jobKeys } from "../jobs/jobKeys";
import { getDecisions, getJob, type JobList, type JobSummary } from "../jobs/jobsApi";
import { getSplit, isSplitJob } from "../jobs/splitApi";
import { coresOf, isActive, members } from "./overviewModel";

/** Recent jobs the Overview looks at (speed-up, split, failed-job alerts). */
export const RECENT_JOBS = 20;
/** Of those, how many are opened to find a split job. */
const SPLIT_SCAN = 6;
/** Split summary poll while the job runs (backstop to split.progress events). */
const SPLIT_POLL_MS = 10_000;

export const recentJobsKey = [...queryKeys.activeJobs, "recent"] as const;

function listRecentJobs(signal?: AbortSignal): Promise<JobList> {
  return apiRequest<JobList>("/v1/jobs", { signal, query: { limit: RECENT_JOBS } });
}

export function useRecentJobs() {
  // Events keep it fresh; the slow poll is a backstop (and moves the demo).
  return useQuery({ queryKey: recentJobsKey, queryFn: ({ signal }) => listRecentJobs(signal), refetchInterval: 15_000 });
}

/** `node_id -> logical cores` from each member's `GET /v1/nodes/{id}` hardware. */
export function useNodeCores(nodes: readonly NodeSummary[]): Map<string, number> {
  const ids = members(nodes).map((n) => n.node_id);
  const details = useQueries({
    queries: ids.map((id) => ({
      queryKey: queryKeys.node(id),
      queryFn: ({ signal }: { signal: AbortSignal }) => getNode(id, signal),
      staleTime: 60_000,
    })),
  });
  const key = details.map((d, i) => `${ids[i] ?? ""}:${String(coresOf(d.data) ?? "")}`).join("|");
  return useMemo(() => {
    const out = new Map<string, number>();
    for (const part of key.split("|")) {
      const [id, c] = part.split(":");
      if (id && c) out.set(id, Number(c));
    }
    return out;
  }, [key]);
}

/**
 * The split job the Split card shows: the newest active split job among the
 * recent ones, else the newest split job; plus how many other splits run.
 */
export function useSplitFocus(jobs: readonly JobSummary[] | undefined) {
  const scan = (jobs ?? []).slice(0, SPLIT_SCAN);
  const details = useQueries({
    queries: scan.map((j) => ({
      queryKey: jobKeys.detail(j.job_id),
      queryFn: ({ signal }: { signal: AbortSignal }) => getJob(j.job_id, signal),
      staleTime: 30_000,
    })),
  });
  const pending = details.some((d) => d.isPending);
  const splits = scan.filter((_, i) => isSplitJob(details[i]?.data?.manifest));
  const active = splits.filter(isActive);
  const job = active[0] ?? splits[0];
  const split = useQuery({
    queryKey: jobKeys.split(job?.job_id ?? "-"),
    queryFn: ({ signal }) => getSplit(job?.job_id ?? "", signal),
    enabled: job !== undefined,
    refetchInterval: job !== undefined && isActive(job) ? SPLIT_POLL_MS : false,
  });
  return {
    job,
    split: job === undefined ? undefined : split,
    otherActive: Math.max(0, active.length - (job !== undefined && isActive(job) ? 1 : 0)),
    scanning: pending,
  };
}

export function useDecisions(jobId: string | undefined) {
  return useQuery({
    queryKey: jobKeys.decisions(jobId ?? "-"),
    queryFn: ({ signal }) => getDecisions(jobId ?? "", signal),
    enabled: jobId !== undefined,
  });
}
