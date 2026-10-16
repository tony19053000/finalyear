/** Formatting for the Jobs pages: times, durations, sizes (pure, unit-tested). */

/** "2026-10-16 14:03:07" in the browser's zone; the raw value on bad input. */
export function formatTime(iso: string | null | undefined): string {
  if (iso == null || iso === "") return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}

/** "850 ms", "12.4 s", "3 m 05 s", "2 h 01 m". */
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const whole = Math.round(seconds);
  const pad = (n: number) => String(n).padStart(2, "0");
  if (whole < 3600) return `${Math.floor(whole / 60)} m ${pad(whole % 60)} s`;
  return `${Math.floor(whole / 3600)} h ${pad(Math.floor((whole % 3600) / 60))} m`;
}

/** Milliseconds between two ISO instants, or null. */
export function between(from: string | null | undefined, to: string | null | undefined): number | null {
  if (from == null || to == null) return null;
  const a = Date.parse(from);
  const b = Date.parse(to);
  return Number.isNaN(a) || Number.isNaN(b) ? null : b - a;
}

const UNITS = ["B", "KiB", "MiB", "GiB", "TiB"];

/** "0 B", "512 B", "1.5 KiB", "8.0 MiB" (binary units, as the CLI). */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return "—";
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return unit === 0 ? `${value} B` : `${value.toFixed(1)} ${UNITS[unit] ?? ""}`;
}

/** A digest shortened for a table cell: "sha256:1a2b3c4d5e6f…". */
export function shortDigest(digest: string | null | undefined, keep = 12): string {
  if (digest == null || digest === "") return "—";
  const hex = digest.replace(/^sha256:/, "");
  return hex.length > keep ? `${hex.slice(0, keep)}…` : hex;
}

/** `<input type="datetime-local">` value -> ISO 8601 with the zone, or "". */
export function localInputToIso(value: string): string {
  if (value === "") return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toISOString();
}

/** ISO 8601 -> an `<input type="datetime-local">` value in the local zone. */
export function isoToLocalInput(iso: string): string {
  if (iso === "") return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}
