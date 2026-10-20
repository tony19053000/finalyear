/**
 * Pure helpers of the Chunks tab (V0.10 G7): grid geometry, cell colours, the
 * live elapsed time. Kept out of the component files (fast refresh wants
 * component-only modules) and unit-tested.
 */
import type { ChunkMapChar, SplitProgress } from "../../pages/jobs/splitApi";

/** Cell pitch in px (a 10 px square + 2 px gap). */
export const CELL = 12;
export const SQUARE = 10;
/** Columns when the width is not known yet (first paint, jsdom). */
export const DEFAULT_COLUMNS = 50;

export const CELL_CLASS: Record<ChunkMapChar, string> = {
  p: "fill-surface-sunken stroke-border",
  l: "fill-status-busy",
  s: "fill-status-degraded",
  d: "fill-status-ok",
  f: "fill-status-failed",
};

/** Columns that fit `width` px (at least 10; the default when unknown). */
export function columnsFor(width: number): number {
  if (!Number.isFinite(width) || width <= 0) return DEFAULT_COLUMNS;
  return Math.max(10, Math.floor(width / CELL));
}

/** The state of chunk `index` in a chunk map (an unknown character = pending). */
export function charAt(map: string, index: number): ChunkMapChar {
  const ch = map.charAt(index);
  return ch in CELL_CLASS ? (ch as ChunkMapChar) : "p";
}

/** Chunks per state in a chunk map. */
export function mapCounts(map: string): Record<ChunkMapChar, number> {
  const counts: Record<ChunkMapChar, number> = { p: 0, l: 0, s: 0, d: 0, f: 0 };
  for (const ch of map) {
    if (ch in counts) counts[ch as ChunkMapChar] += 1;
  }
  return counts;
}

/** Elapsed seconds now: the server's figure moved on by the time since it was read. */
export function liveElapsed(progress: SplitProgress, nowMs: number): number | null {
  if (progress.elapsed_s == null) return null;
  if (progress.ended_at != null) return progress.elapsed_s;
  const at = Date.parse(progress.computed_at);
  if (Number.isNaN(at)) return progress.elapsed_s;
  return progress.elapsed_s + Math.max(0, (nowMs - at) / 1000);
}
