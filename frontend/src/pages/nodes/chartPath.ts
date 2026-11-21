/**
 * Smooth line paths for the node detail charts (pure, unit-tested): a
 * monotone cubic (Fritsch-Carlson) through the points, so the curve never
 * overshoots a sample - a 0-100 % line stays inside 0-100 and a flat run
 * stays flat. Nothing is smoothed away: every sample is on the curve.
 */
function pct(v: number): string {
  return `${String(Math.round(v))}%`;
}

/** "CPU: now 25%, min 20%, max 25% over 2 samples" - the chart's text for screen readers. */
export function lineSummary(label: string, points: readonly { v: number }[]): string {
  const last = points.at(-1);
  if (last === undefined) return `${label}: no samples`;
  const values = points.map((p) => p.v);
  return `${label}: now ${pct(last.v)}, min ${pct(Math.min(...values))}, max ${pct(Math.max(...values))} over ${String(points.length)} samples`;
}

export interface XY {
  x: number;
  y: number;
}

function f(v: number): string {
  return v.toFixed(1);
}

/** `M…C…` path through `pts` (sorted by x); "" for no points, a single move for one. */
export function monotonePath(pts: readonly XY[]): string {
  const n = pts.length;
  const first = pts[0];
  if (first === undefined) return "";
  if (n === 1) return `M${f(first.x)},${f(first.y)}`;
  const slopes: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    const a = pts[i] as XY;
    const b = pts[i + 1] as XY;
    const dx = b.x - a.x;
    slopes.push(dx === 0 ? 0 : (b.y - a.y) / dx);
  }
  const t: number[] = new Array<number>(n).fill(0);
  t[0] = slopes[0] ?? 0;
  t[n - 1] = slopes[n - 2] ?? 0;
  for (let i = 1; i < n - 1; i++) {
    const m0 = slopes[i - 1] ?? 0;
    const m1 = slopes[i] ?? 0;
    t[i] = m0 * m1 <= 0 ? 0 : (m0 + m1) / 2;
  }
  for (let i = 0; i < n - 1; i++) {
    const m = slopes[i] ?? 0;
    if (m === 0) {
      t[i] = 0;
      t[i + 1] = 0;
      continue;
    }
    const a = (t[i] ?? 0) / m;
    const b = (t[i + 1] ?? 0) / m;
    const s = a * a + b * b;
    if (s > 9) {
      const k = 3 / Math.sqrt(s);
      t[i] = k * a * m;
      t[i + 1] = k * b * m;
    }
  }
  let d = `M${f(first.x)},${f(first.y)}`;
  for (let i = 0; i < n - 1; i++) {
    const a = pts[i] as XY;
    const b = pts[i + 1] as XY;
    const h = (b.x - a.x) / 3;
    d += `C${f(a.x + h)},${f(a.y + (t[i] ?? 0) * h)} ${f(b.x - h)},${f(b.y - (t[i + 1] ?? 0) * h)} ${f(b.x)},${f(b.y)}`;
  }
  return d;
}
