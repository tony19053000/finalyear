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
import { isJobState, type JobDetail, type JobList } from "./jobsApi";

export type JobListData = InfiniteData<JobList, string | null>;

type JobRow = JobList["jobs"][number];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** The pages of a cached list, or null when the entry is not a list we wrote (T-10). */
function listPages(data: unknown): unknown[] | null {
  return isRecord(data) && Array.isArray(data.pages) ? (data.pages as unknown[]) : null;
}

function pageJobs(page: unknown): JobRow[] | null {
  return isRecord(page) && Array.isArray(page.jobs) ? (page.jobs as JobRow[]) : null;
}

function rowId(row: unknown): unknown {
  return isRecord(row) ? row.job_id : undefined;
}

/** `jobKeys.list(filters)`'s states, or null when the key is not that shape. */
function keyStates(key: readonly unknown[]): readonly unknown[] | null {
  const filters = key[2];
  if (key.length !== 3 || !isRecord(filters) || !Array.isArray(filters.states)) return null;
  return filters.states as unknown[];
}

/**
 * Defensive by design: the shell chains appliers, so a cache entry of an odd
 * shape (another writer, an older key layout) is skipped, never a throw.
 */
function patchLists(client: QueryClient, jobId: string, state: JobDetail["state"]): void {
  const stale: (readonly unknown[])[] = [];
  for (const [key, data] of client.getQueriesData({ queryKey: jobKeys.lists })) {
    const pages = listPages(data);
    const states = keyStates(key);
    if (pages === null || states === null) continue;
    const holds = (page: unknown) => pageJobs(page)?.some((job) => rowId(job) === jobId) === true;
    if (pages.some(holds)) {
      const patched = pages.map((page) => {
        const jobs = pageJobs(page);
        if (jobs === null || !holds(page)) return page;
        return { ...(page as JobList), jobs: jobs.map((job) => (rowId(job) === jobId ? { ...job, state } : job)) };
      });
      client.setQueryData(key, { ...(data as JobListData), pages: patched });
      continue;
    }
    if (states.length > 0 && !states.includes(state)) continue;
    const newest = rowId(pageJobs(pages[0])?.[0]);
    if (typeof newest !== "string" || jobId > newest) stale.push(key);
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
      client.setQueryData<JobDetail>(jobKeys.detail(jobId), (old) => (isRecord(old) ? { ...old, state } : old));
    }
  }
  // Prefix: the detail and its decisions.
  void client.invalidateQueries({ queryKey: jobKeys.detail(jobId) });
  return true;
}
