/**
 * Live job updates for the Jobs pages (D172): what a `job.state` or
 * `attempt.state` event does to the TanStack Query cache. Pure - no React,
 * no second store; G3/the Lead calls it from the shell's event hook
 * (`applyEvent` in events/applyEvent.ts) at merge time.
 *
 * - `job.state {job_id, state}`: rows of every cached list page are patched
 *   in place (no refetch of a 10 000-row list for one change); the job's
 *   detail is patched and then refetched (transitions, attempts). A job no
 *   list holds yet and NEWER than the newest row a list holds (ids are ULIDs:
 *   lexical order = creation order) is a new job: those lists refetch. An
 *   older unknown job is on a page not loaded, or filtered out: ignored.
 * - `attempt.state {attempt_id, job_id, node_id, state}`: the job's detail
 *   and decisions refetch (attempt rows, plan outcome).
 *
 * Returns true when the event was a job event (handled here).
 */
import type { InfiniteData, QueryClient } from "@tanstack/react-query";

import type { FabricEvent } from "../../events/eventStream";
import { jobKeys } from "./jobKeys";
import { isJobState, type JobDetail, type JobFilters, type JobList } from "./jobsApi";

export type JobListData = InfiniteData<JobList, string | null>;

function patchLists(client: QueryClient, jobId: string, state: JobDetail["state"]): void {
  const stale: (readonly unknown[])[] = [];
  for (const [key, data] of client.getQueriesData<JobListData>({ queryKey: jobKeys.lists })) {
    if (data === undefined) continue;
    const holds = (page: JobList) => page.jobs.some((job) => job.job_id === jobId);
    if (data.pages.some(holds)) {
      const pages = data.pages.map((page) =>
        holds(page) ? { ...page, jobs: page.jobs.map((job) => (job.job_id === jobId ? { ...job, state } : job)) } : page,
      );
      client.setQueryData<JobListData>(key, { ...data, pages });
      continue;
    }
    const filters = key[2] as JobFilters | undefined; // jobKeys.list(filters)
    if (filters !== undefined && filters.states.length > 0 && !filters.states.includes(state)) continue;
    const newest = data.pages[0]?.jobs[0]?.job_id;
    if (newest === undefined || jobId > newest) stale.push(key);
  }
  for (const key of stale) {
    void client.invalidateQueries({ queryKey: key, exact: true });
  }
}

export function applyJobEvent(client: QueryClient, event: FabricEvent): boolean {
  if (event.type !== "job.state" && event.type !== "attempt.state") return false;
  const jobId = event.data.job_id;
  if (typeof jobId !== "string" || jobId === "") return true;
  if (event.type === "job.state") {
    const state = event.data.state;
    if (isJobState(state)) {
      patchLists(client, jobId, state);
      client.setQueryData<JobDetail>(jobKeys.detail(jobId), (old) => (old === undefined ? old : { ...old, state }));
    }
  }
  // Prefix: the detail and its decisions.
  void client.invalidateQueries({ queryKey: jobKeys.detail(jobId) });
  return true;
}
