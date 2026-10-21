/**
 * Query keys of the Jobs pages. They extend the shared `queryKeys.jobs` /
 * `queryKeys.job(id)` (api/queryKeys.ts), so G2's generic event handling -
 * invalidate `["jobs", id]` on a job/attempt event - also reaches the job
 * detail and its decisions, and the list pages live under `["jobs", "list"]`.
 * Job ids are `job_<ULID>`, so the literal "list" can never be a job id.
 */
import { queryKeys } from "../../api/queryKeys";
import type { JobFilters } from "./jobsApi";

export const jobKeys = {
  all: queryKeys.jobs,
  lists: [...queryKeys.jobs, "list"] as const,
  list: (filters: JobFilters) => [...queryKeys.jobs, "list", filters] as const,
  detail: (jobId: string) => queryKeys.job(jobId),
  decisions: (jobId: string) => [...queryKeys.job(jobId), "decisions"] as const,
  /** V0.10 G7: a split job's progress summary (with its chunk map). */
  split: (jobId: string) => [...queryKeys.job(jobId), "split"] as const,
  /** V0.10 G7: one chunk's detail. */
  chunk: (jobId: string, index: number) => [...queryKeys.job(jobId), "chunk", index] as const,
  /** V0.10 G8: one chunk's recorded tries (with stderr tails). */
  chunkTries: (jobId: string, index: number) =>
    [...queryKeys.job(jobId), "chunk", index, "tries"] as const,
};
