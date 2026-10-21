/**
 * A split job's progress (V0.10 G7, D270): `GET /v1/jobs/{id}/split` and
 * `GET /v1/jobs/{id}/chunks`, typed from schema.d.ts. Both are read from the
 * controller's rows on every request (checklist 37: shown numbers = rows).
 */
import { apiRequest } from "../../api/client";
import type { components } from "../../api/schema";

type Schemas = components["schemas"];
export type SplitProgress = Schemas["SplitProgressOut"];
export type SplitLane = Schemas["LaneOut"];
export type ChunkCounts = Schemas["ChunkCountsOut"];
export type ChunkList = Schemas["ChunkList"];
export type ChunkOut = Schemas["ChunkOut"];

/** Chunk map characters (`?map=true`): one per chunk in index order. */
export type ChunkMapChar = "p" | "l" | "s" | "d" | "f";

export const CHUNK_STATE_LABEL: Record<ChunkMapChar, string> = {
  p: "pending",
  l: "running",
  s: "running + speculative copy",
  d: "done",
  f: "failed",
};

/** True when the job's manifest has a `split:` section. */
export function isSplitJob(manifest: Record<string, unknown> | null | undefined): boolean {
  const split = manifest?.split;
  return typeof split === "object" && split !== null;
}

/** `GET /v1/jobs/{id}/split?map=true` - summary + one state character per chunk. */
export function getSplit(jobId: string, signal?: AbortSignal): Promise<SplitProgress> {
  return apiRequest<SplitProgress>(`/v1/jobs/${encodeURIComponent(jobId)}/split`, {
    signal,
    query: { map: true },
  });
}

export type ChunkTries = Schemas["ChunkTries"];
export type ChunkTry = Schemas["ChunkTryOut"];

/**
 * V0.10 G8 (D261/D271): one chunk's recorded tries - node, outcome, timings
 * and each try's stderr tail (redacted on the agent).
 */
export function getChunkTries(jobId: string, index: number, signal?: AbortSignal): Promise<ChunkTries> {
  return apiRequest<ChunkTries>(
    `/v1/jobs/${encodeURIComponent(jobId)}/chunks/${String(index)}/tries`,
    { signal },
  );
}

/** One chunk (`?start=N&limit=1`), or null when the job has no such chunk. */
export async function getChunk(jobId: string, index: number, signal?: AbortSignal): Promise<ChunkOut | null> {
  const page = await apiRequest<ChunkList>(`/v1/jobs/${encodeURIComponent(jobId)}/chunks`, {
    signal,
    query: { start: index, limit: 1 },
  });
  const first = page.chunks[0];
  return first !== undefined && first.chunk_index === index ? first : null;
}
