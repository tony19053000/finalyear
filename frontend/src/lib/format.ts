/**
 * Number and time formatting for the dashboard. Pure functions: every number
 * the UI prints goes through one of these, so "what the API said" and "what
 * the page shows" differ only by a tested rounding rule (checklist 26).
 */

const BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"] as const;

/** `1536` -> `"1.5 KiB"`; binary units, one decimal from KiB up. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return "—";
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${String(Math.round(value))} B` : `${value.toFixed(1)} ${BYTE_UNITS[unit] ?? ""}`;
}

/** A 64-bit count the API sends as a JSON string (proto int64) or a number. */
export function toNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && /^\d+$/.test(value)) return Number(value);
  return null;
}

/** `42.04` -> `"42%"`; `null` -> `"—"`. Whole percent: the samples are not more precise. */
export function formatPercent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${String(Math.round(value))}%`;
}

/** `used` of `total` as a percentage (null when either is unknown or total is 0). */
export function percentOf(used: number | null | undefined, total: number | null | undefined): number | null {
  if (used == null || total == null || !Number.isFinite(used) || !Number.isFinite(total) || total <= 0) return null;
  return (used / total) * 100;
}

/** Bytes per second -> `"12.3 MiB/s"`. */
export function formatRate(bytesPerS: number | null | undefined): string {
  const text = formatBytes(bytesPerS);
  return text === "—" ? text : `${text}/s`;
}

/** Milliseconds with a sensible precision: `0.42 ms`, `12 ms`, `1.2 s`. */
export function formatMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 10) return `${ms.toFixed(2)} ms`;
  if (ms < 1000) return `${String(Math.round(ms))} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

/** Seconds -> `"just now"`, `"12 s ago"`, `"3 min ago"`, `"2 h ago"`, `"4 d ago"`. */
export function formatAgo(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, seconds);
  if (s < 2) return "just now";
  if (s < 60) return `${String(Math.floor(s))} s ago`;
  if (s < 3600) return `${String(Math.floor(s / 60))} min ago`;
  if (s < 86_400) return `${String(Math.floor(s / 3600))} h ago`;
  return `${String(Math.floor(s / 86_400))} d ago`;
}

/** Seconds between an ISO instant and `nowMs` (null for an unparsable instant). */
export function secondsSince(iso: string | null | undefined, nowMs: number): number | null {
  if (iso == null) return null;
  const at = Date.parse(iso);
  return Number.isNaN(at) ? null : (nowMs - at) / 1000;
}

/** Local date + time of an ISO instant (the raw string when unparsable). */
export function formatDateTime(iso: string | null | undefined): string {
  if (iso == null) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

/** Local time of day, to the second. */
export function formatTime(iso: string | null | undefined): string {
  if (iso == null) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleTimeString();
}

/** `node_01J...XYZ` -> `node_…XYZ12` for tight columns (the full id stays in a title). */
export function shortId(id: string, keep = 6): string {
  const underscore = id.indexOf("_");
  if (underscore < 0 || id.length <= underscore + 1 + keep) return id;
  return `${id.slice(0, underscore + 1)}…${id.slice(-keep)}`;
}
