/**
 * The controller's clock, as seen from this browser.
 *
 * "Last heartbeat 3 s ago" compares a controller timestamp with "now"; the
 * browser may run on another laptop whose clock is off by seconds or minutes.
 * Every live event carries the controller's `at`: the offset is the median of
 * the last few `at - received` differences (network latency makes each one a
 * little low; the median ignores a stray slow one). Before any event arrives
 * the offset is 0 - the browser's own clock.
 */
import { useSyncExternalStore } from "react";

const WINDOW = 15;
const samples: number[] = [];
let offsetMs = 0;
const listeners = new Set<() => void>();

function median(values: readonly number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  const hi = sorted[mid] ?? 0;
  return sorted.length % 2 === 1 ? hi : ((sorted[mid - 1] ?? hi) + hi) / 2;
}

/** Record one controller timestamp (ms since the epoch) received now. */
export function noteServerTime(atMs: number, receivedMs: number = Date.now()): void {
  // 0 / negative is a missing timestamp, not 1970: it would drag the offset by decades.
  if (!Number.isFinite(atMs) || atMs <= 0 || !Number.isFinite(receivedMs)) return;
  samples.push(atMs - receivedMs);
  if (samples.length > WINDOW) samples.shift();
  offsetMs = median(samples);
}

/** The controller's "now" in ms since the epoch. */
export function serverNow(localMs: number = Date.now()): number {
  return localMs + offsetMs;
}

export function serverClockOffsetMs(): number {
  return offsetMs;
}

/** Tests: forget every sample. */
export function resetServerClock(): void {
  samples.length = 0;
  offsetMs = 0;
}

/* A shared 1 s ticker so "x s ago" texts move without a refetch. */
let tick = 0;
let timer: ReturnType<typeof setInterval> | null = null;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  timer ??= setInterval(() => {
    tick += 1;
    for (const l of listeners) l();
  }, 1000);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

/** Re-render every second; returns the controller's "now" (ms). */
export function useServerNow(): number {
  useSyncExternalStore(subscribe, () => tick);
  return serverNow();
}
