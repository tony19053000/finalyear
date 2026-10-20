/**
 * Live split progress for the Job page's Chunks tab (V0.10 G7, D270): what a
 * `split.progress` event does to the TanStack Query cache. Pure - no React,
 * no second store; the shell runs it through `EVENT_APPLIERS`.
 *
 * `split.progress` carries the same summary as `GET /v1/jobs/{id}/split`
 * (controller/split/progress.py, read from the rows), without the chunk map,
 * at most `progress_event_hz` (2) per job per second, to the job's owner and
 * admins:
 *
 * - a cached summary of that job gets the event's numbers in place (its chunk
 *   map is kept: the event has none);
 * - when the chunk counts moved, the summary is refetched so the grid's map
 *   follows (at most at the event rate);
 * - an event OLDER than the cache is ignored (the same rule as V0.9 D257
 *   `isStale` for campaigns): a refetch can land between two events, so a
 *   cached summary may already be newer - its `computed_at` is later, it has
 *   more chunks done (done chunks never un-finish), or its job already ended
 *   (an ended job never leaves its final state).
 *
 * Defensive by design: an event of the wrong shape is ignored, never written
 * into the cache. Returns true when the event was a split event.
 */
import type { QueryClient } from "@tanstack/react-query";

import type { FabricEvent } from "../../events/eventStream";
import { jobKeys } from "./jobKeys";
import { TERMINAL_JOB_STATES, isJobState } from "./jobsApi";
import type { ChunkCounts, SplitProgress } from "./splitApi";

export const SPLIT_PROGRESS = "split.progress";

const COUNT_KEYS = ["total", "pending", "leased", "done", "failed"] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function count(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : undefined;
}

/** All five chunk counts as non-negative integers, or undefined. */
export function parseChunkCounts(value: unknown): ChunkCounts | undefined {
  if (!isRecord(value)) return undefined;
  const out: Partial<Record<(typeof COUNT_KEYS)[number], number>> = {};
  for (const key of COUNT_KEYS) {
    const n = count(value[key]);
    if (n === undefined) return undefined;
    out[key] = n;
  }
  return out as ChunkCounts;
}

function time(value: unknown): number | undefined {
  if (typeof value !== "string") return undefined;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? undefined : ms;
}

/**
 * The event as a summary, or undefined when it is not usable: it must name
 * the job, a known job state, valid counts, items and a `computed_at`; the
 * rest of the fields keep the server's types (checked where they are shown).
 */
export function parseSplitEvent(data: Record<string, unknown>): SplitProgress | undefined {
  const jobId = data.job_id;
  if (typeof jobId !== "string" || jobId === "") return undefined;
  if (!isJobState(data.job_state)) return undefined;
  const chunks = parseChunkCounts(data.chunks);
  if (chunks === undefined) return undefined;
  if (count(data.items_done) === undefined || count(data.items_total) === undefined) return undefined;
  if (time(data.computed_at) === undefined) return undefined;
  if (!Array.isArray(data.lanes) || !Array.isArray(data.warnings)) return undefined;
  return { ...(data as unknown as SplitProgress), chunks };
}

/** The cached summary is newer than the event (tester r1 T-1 rule of D257). */
export function isStale(cached: SplitProgress, event: SplitProgress): boolean {
  if (TERMINAL_JOB_STATES.has(cached.job_state) && event.job_state !== cached.job_state) return true;
  if (event.chunks.done < cached.chunks.done || event.items_done < cached.items_done) return true;
  const cachedAt = time(cached.computed_at);
  const eventAt = time(event.computed_at);
  return cachedAt !== undefined && eventAt !== undefined && eventAt < cachedAt;
}

function isSummary(value: unknown): value is SplitProgress {
  return isRecord(value) && typeof value.job_id === "string" && parseChunkCounts(value.chunks) !== undefined;
}

function sameCounts(a: ChunkCounts, b: ChunkCounts): boolean {
  return COUNT_KEYS.every((key) => a[key] === b[key]);
}

export function applySplitEvent(client: QueryClient, event: FabricEvent): boolean {
  if (event.type !== SPLIT_PROGRESS) return false;
  const progress = parseSplitEvent(event.data);
  if (progress === undefined) return true;
  const key = jobKeys.split(progress.job_id);
  const cached = client.getQueryData(key);
  if (!isSummary(cached)) return true; // nobody shows this job's chunks
  if (isStale(cached, progress)) return true;
  client.setQueryData<SplitProgress>(key, { ...progress, chunk_map: cached.chunk_map ?? null });
  if (!sameCounts(cached.chunks, progress.chunks) || cached.job_state !== progress.job_state) {
    void client.invalidateQueries({ queryKey: key, exact: true });
  }
  return true;
}
