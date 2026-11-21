/**
 * The Overview line graph's history: recorded in the browser, because the
 * controller keeps only ~5 min of live samples per node and no fabric-wide
 * series. While the Overview is open it samples the live node list every
 * `SAMPLE_MS` and keeps the last `WINDOW_MS`; the store is module-level, so
 * the graph keeps what it has when the user visits another page and comes
 * back (it is lost on a reload - it says "recording" until it fills).
 */
import { useEffect, useRef, useSyncExternalStore } from "react";

import type { NodeLoad } from "./overviewModel";

export const SAMPLE_MS = 4_000;
export const WINDOW_MS = 10 * 60_000;
const MAX_POINTS = Math.ceil(WINDOW_MS / SAMPLE_MS) + 4;

export interface HistoryPoint {
  /** Browser time (ms). */
  t: number;
  fabric: NodeLoad;
  /** Per live node. */
  nodes: Record<string, NodeLoad>;
}

let points: readonly HistoryPoint[] = [];
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

export function recordPoint(point: HistoryPoint): void {
  const cutoff = point.t - WINDOW_MS;
  points = [...points.filter((p) => p.t >= cutoff), point].slice(-MAX_POINTS);
  emit();
}

/** Tests only. */
export function resetHistory(): void {
  points = [];
  emit();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function snapshot(): readonly HistoryPoint[] {
  return points;
}

export function useFabricHistory(): readonly HistoryPoint[] {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

/**
 * Records `read()` now and then every `SAMPLE_MS` while mounted. `read`
 * returns null while there is nothing to record (no node reporting).
 */
export function useHistorySampler(read: () => Omit<HistoryPoint, "t"> | null): void {
  const latest = useRef(read);
  useEffect(() => {
    latest.current = read;
  });
  useEffect(() => {
    const tick = () => {
      const p = latest.current();
      if (p !== null) recordPoint({ t: Date.now(), ...p });
    };
    // A first point as soon as data is there, then a steady clock.
    const first = setTimeout(tick, 600);
    const timer = setInterval(tick, SAMPLE_MS);
    return () => {
      clearTimeout(first);
      clearInterval(timer);
    };
  }, []);
}
