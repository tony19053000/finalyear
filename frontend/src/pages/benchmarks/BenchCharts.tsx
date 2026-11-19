/**
 * The Benchmarks charts in the Overview's language (LoadLineChart): hand-
 * written SVG, thin lines, a faint grid, colour-only legend labels, a frosted
 * tooltip; honey for what hive picked / the best, grey for the local
 * baseline. Sizes are SVG attributes (no inline style: CSP `style-src
 * 'self'`). Every number drawn is a server median or CI passed in.
 */
import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";

import { cn } from "../../lib/cn";
import { toneClass, type Tone } from "./benchShared";

export type { Tone };

function useWidth<T extends HTMLElement>(fallback: number) {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (el === null) return;
    const measure = () => {
      const w = el.clientWidth;
      if (w > 0) setWidth(w);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => {
      ro.disconnect();
    };
  }, []);
  return [ref, width] as const;
}

/* ------------------------------------------------------------ legend */

export function Legend({ items }: { items: readonly { key: string; label: string; className: string }[] }) {
  return (
    <ul className="flex flex-wrap gap-x-6 gap-y-1.5" aria-label="Series">
      {items.map((item) => (
        <li key={item.key} className={cn("font-mono text-[0.6875rem] font-medium tracking-[0.14em] uppercase", item.className)}>
          {item.label}
        </li>
      ))}
    </ul>
  );
}

/* ------------------------------------------------------------ comparison bars */

export interface CompareBar {
  key: string;
  label: string;
  /** A quieter second word (the strategy). */
  sub?: string;
  value: number;
  ciLow?: number;
  ciHigh?: number;
  tone: Tone;
}

/** Only the hive / best bar takes colour (honey); every other bar is grey, the baseline the quietest. */
function barClass(tone: Tone): string {
  return tone === "other" ? "text-fg-muted" : toneClass(tone);
}

/**
 * Horizontal comparison bars: one row per option, the value at the bar's
 * end; only the hive / best bar is honey, the others grey. The CI shows as a
 * faint whisker across the bar's end.
 */
export function CompareBars({ bars, format, label }: { bars: readonly CompareBar[]; format: (v: number) => string; label: string }) {
  const max = Math.max(...bars.map((b) => Math.max(b.value, b.ciHigh ?? 0)), 1);
  // the longest bar ends at 80 % of the track, leaving room for its number
  const pct = (v: number) => (Math.max(0, v) / max) * 80;
  return (
    <ul aria-label={label} className="flex flex-col gap-5" data-testid="compare-bars">
      {bars.map((b) => (
        <li key={b.key} className="grid grid-cols-[minmax(0,15rem)_minmax(0,1fr)] items-center gap-x-8">
          <span className="flex min-w-0 flex-col">
            <span className={cn("truncate text-[0.9375rem]", b.tone === "hive" ? "font-medium text-fg" : "text-fg-muted")} title={b.label}>
              {b.label}
            </span>
            {b.sub !== undefined && (
              <span className={cn("jb-tag mt-0.5 text-[0.625rem]", b.tone === "hive" ? "text-[#ffc93c]" : "text-fg-subtle")}>{b.sub}</span>
            )}
          </span>
          <svg aria-hidden="true" className={cn("block h-6 w-full overflow-visible", barClass(b.tone))}>
            <rect x="0" y="9" width="100%" height="6" rx="3" className="bm-track" />
            <rect x="0" y="9" width={`${String(pct(b.value))}%`} height="6" rx="3" fill="currentColor" fillOpacity={b.tone === "hive" ? 1 : 0.55} className="bm-bar" />
            {b.ciLow !== undefined && b.ciHigh !== undefined && b.ciHigh > b.ciLow && (
              <line x1={`${String(pct(b.ciLow))}%`} x2={`${String(pct(b.ciHigh))}%`} y1="12" y2="12" stroke="currentColor" strokeOpacity="0.45" strokeWidth="1" className="bm-in" />
            )}
            <text
              x={`${String(pct(Math.max(b.value, b.ciHigh ?? 0)))}%`}
              dx="12"
              y="12"
              dominantBaseline="central"
              className={cn("bm-in font-mono text-[12px] tabular-nums", b.tone === "hive" ? "fill-[#ffc93c]" : "fill-fg-muted")}
            >
              {format(b.value)}
            </text>
          </svg>
        </li>
      ))}
    </ul>
  );
}

/* ------------------------------------------------------------ line chart */

export interface LinePoint {
  x: number;
  y: number;
  lo: number;
  hi: number;
  n: number;
}

export interface LineSeries {
  key: string;
  label: string;
  className: string;
  /** Drawn on top, slightly thicker (the best / hive series). */
  strong?: boolean;
  points: readonly LinePoint[];
}

const PAD = { l: 64, r: 20, t: 14, b: 34 };
const TIP_W = 230;

function niceLogTicks(min: number, max: number): number[] {
  const out: number[] = [];
  for (let e = Math.floor(Math.log10(min)); e <= Math.ceil(Math.log10(max)); e++) {
    for (const m of [1, 3]) {
      const v = m * 10 ** e;
      if (v >= min * 0.98 && v <= max * 1.02) out.push(v);
    }
  }
  return out.length >= 2 ? out : [min, max];
}

function niceLinearTicks(max: number): number[] {
  const step = 10 ** Math.floor(Math.log10(max / 4));
  const k = [1, 2, 5, 10].find((m) => max / (m * step) <= 5) ?? 10;
  const s = k * step;
  return Array.from({ length: Math.floor(max / s) + 1 }, (_, i) => i * s);
}

/**
 * Median total time vs the swept parameter: one thin line per series, the
 * CI as faint error bars, log axes when the values span 20x or more. Hover
 * shows every series at the nearest parameter value.
 */
export function LineChart({
  series,
  logX,
  xLabel,
  formatX,
  formatY,
  label,
  height = 280,
  head,
}: {
  series: readonly LineSeries[];
  logX: boolean;
  xLabel: string;
  formatX: (v: number) => string;
  formatY: (v: number) => string;
  label: string;
  height?: number;
  head?: ReactNode;
}) {
  const [ref, width] = useWidth<HTMLDivElement>(720);
  const [hoverX, setHoverX] = useState<number | null>(null);
  const pts = series.flatMap((s) => s.points);
  const xs = [...new Set(pts.map((p) => p.x))].sort((a, b) => a - b);
  const yMin = Math.min(...pts.map((p) => Math.max(p.lo, 1e-9)));
  const yMax = Math.max(...pts.map((p) => p.hi), 1e-9);
  const logY = yMin > 0 && yMax / yMin >= 20;
  const iw = Math.max(10, width - PAD.l - PAD.r);
  const ih = Math.max(10, height - PAD.t - PAD.b);
  const x0 = xs[0] ?? 0;
  const x1 = xs.at(-1) ?? 1;
  const x = (v: number) => {
    if (x1 === x0) return PAD.l + iw / 2;
    const k = logX ? (Math.log(v) - Math.log(x0)) / (Math.log(x1) - Math.log(x0)) : (v - x0) / (x1 - x0);
    return PAD.l + k * iw;
  };
  const yTicks = logY ? niceLogTicks(yMin, yMax) : niceLinearTicks(yMax * 1.05);
  const yLo = logY ? Math.min(yMin, yTicks[0] ?? yMin) : 0;
  const yHi = Math.max(yMax * (logY ? 1.15 : 1.05), yTicks.at(-1) ?? yMax);
  const y = (v: number) => {
    const k = logY ? (Math.log(Math.max(v, yLo)) - Math.log(yLo)) / (Math.log(yHi) - Math.log(yLo)) : v / yHi;
    return PAD.t + (1 - k) * ih;
  };
  const ordered = [...series].sort((a, b) => Number(a.strong === true) - Number(b.strong === true));

  const onMove = (e: MouseEvent<SVGRectElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    const px = PAD.l + ((e.clientX - box.left) / box.width) * iw;
    let best: number | null = null;
    for (const v of xs) if (best === null || Math.abs(x(v) - px) < Math.abs(x(best) - px)) best = v;
    setHoverX(best);
  };
  const tipLeft = hoverX !== null && x(hoverX) > width / 2;

  return (
    <div className="flex flex-col gap-4">
      {head}
      <div ref={ref} className="relative w-full">
        <svg width={width} height={height} role="img" aria-label={label} className="block">
          {yTicks.map((t) => (
            <g key={`y${String(t)}`}>
              <line x1={PAD.l} x2={width - PAD.r} y1={y(t)} y2={y(t)} stroke="#ffffff" strokeOpacity={t === yTicks[0] ? 0.1 : 0.05} />
              <text x={PAD.l - 10} y={y(t) + 3} textAnchor="end" className="fill-fg-subtle font-mono text-[10px]">
                {formatY(t)}
              </text>
            </g>
          ))}
          {xs.map((v) => (
            <text key={`x${String(v)}`} x={x(v)} y={height - 12} textAnchor="middle" className="fill-fg-subtle font-mono text-[10px]">
              {formatX(v)}
            </text>
          ))}
          <text x={width - PAD.r} y={height - 1} textAnchor="end" className="fill-fg-subtle font-mono text-[9px] tracking-[0.12em] uppercase">
            {xLabel}
            {logX ? " · log" : ""}
          </text>
          {hoverX !== null && <line x1={x(hoverX)} x2={x(hoverX)} y1={PAD.t} y2={PAD.t + ih} className="stroke-fg-muted" strokeOpacity={0.5} strokeDasharray="3 3" />}
          {ordered.map((s) => {
            const d = s.points.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.x).toFixed(1)},${y(p.y).toFixed(1)}`).join("");
            return (
              <g key={s.key} className={cn("ov-line", s.className)}>
                {s.points.map((p) => (
                  <line key={`e${String(p.x)}`} x1={x(p.x)} x2={x(p.x)} y1={y(p.lo)} y2={y(p.hi)} stroke="currentColor" strokeOpacity={0.4} strokeWidth={1} />
                ))}
                <path d={d} fill="none" stroke="currentColor" strokeWidth={s.strong === true ? 2 : 1.5} strokeLinejoin="round" strokeLinecap="round" className="bm-draw" pathLength={1} />
                {s.points.map((p) => (
                  <circle key={`p${String(p.x)}`} cx={x(p.x)} cy={y(p.y)} r={hoverX === p.x ? 4 : 2.75} fill="currentColor" className="bm-dot" />
                ))}
              </g>
            );
          })}
          <rect
            x={PAD.l}
            y={PAD.t}
            width={iw}
            height={ih}
            fill="transparent"
            onMouseMove={onMove}
            onMouseLeave={() => {
              setHoverX(null);
            }}
          />
          {hoverX !== null && (
            <foreignObject x={tipLeft ? x(hoverX) - TIP_W - 14 : x(hoverX) + 14} y={PAD.t} width={TIP_W} height={ih} className="pointer-events-none overflow-visible">
              <div className="ov-tooltip px-3.5 py-2.5 text-xs" data-testid="chart-tooltip">
                <p className="mb-2 font-mono text-[0.625rem] tracking-[0.12em] text-fg-muted uppercase">
                  {xLabel} {formatX(hoverX)}
                </p>
                {series.map((s) => {
                  const p = s.points.find((q) => q.x === hoverX);
                  return p === undefined ? null : (
                    <p key={s.key} className="flex items-baseline gap-3 py-0.5">
                      <span className={cn("min-w-0 flex-1 truncate font-mono text-[0.625rem] font-medium tracking-[0.1em] uppercase", s.className)}>{s.label}</span>
                      <span className="shrink-0 font-mono text-fg tabular-nums">{formatY(p.y)}</span>
                    </p>
                  );
                })}
              </div>
            </foreignObject>
          )}
        </svg>
      </div>
    </div>
  );
}
