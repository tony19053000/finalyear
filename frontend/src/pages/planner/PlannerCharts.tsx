/**
 * The Planner's own charts in the Overview / Benchmarks language (hand-
 * written SVG like BenchCharts): thin marks, a faint grid, legend labels
 * only, a frosted tooltip; honey for what the planner predicts with (the ML
 * model where it acted, the zero-error bin), grey for the calibration and the
 * rest. Sizes are SVG attributes (no inline style: CSP `style-src 'self'`).
 * Every point drawn is one the API answered; nothing is fitted here.
 */
import { useState, type MouseEvent } from "react";

import { cn } from "../../lib/cn";
import type { ErrorBin, ScatterPoint } from "./mlView";
import { useElementWidth } from "./plannerQueries";

const GRID = { stroke: "#ffffff", strokeOpacity: 0.05 };
const TIP_W = 236;

function logTicks(lo: number, hi: number): number[] {
  const out: number[] = [];
  for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) {
    for (const m of [1, 3]) {
      const v = m * 10 ** e;
      if (v >= lo * 0.98 && v <= hi * 1.02) out.push(v);
    }
  }
  return out.length >= 2 ? out : [lo, hi];
}

function tick(v: number): string {
  return Number.isFinite(v) ? String(Number(v.toPrecision(2))) : "";
}

const PAD = { l: 58, r: 18, t: 16, b: 34 };

interface Hover {
  series: "calibration" | "ml";
  point: ScatterPoint;
}

/**
 * Predicted vs actual on shared log axes (square domain, so y = x is the
 * diagonal): grey dots for the calibrated estimate, honey dots for the ML
 * prediction where it was used. Hover names the nearest point.
 */
export function ScatterLog({
  calibration,
  ml,
  domain,
  unit,
  label,
  nameOf,
  height = 360,
}: {
  calibration: readonly ScatterPoint[];
  ml: readonly ScatterPoint[];
  domain: [number, number];
  unit: string;
  label: string;
  nameOf: (id: string) => string;
  height?: number;
}) {
  const [ref, width] = useElementWidth<HTMLDivElement>(560);
  const [hover, setHover] = useState<Hover | null>(null);
  const [lo0, hi0] = domain;
  const lo = lo0 / 1.15;
  const hi = hi0 * 1.15;
  const iw = Math.max(10, width - PAD.l - PAD.r);
  const ih = Math.max(10, height - PAD.t - PAD.b);
  const k = (v: number) => (Math.log(Math.max(v, lo)) - Math.log(lo)) / (Math.log(hi) - Math.log(lo));
  const x = (v: number) => PAD.l + k(v) * iw;
  const y = (v: number) => PAD.t + (1 - k(v)) * ih;
  const ticks = logTicks(lo, hi);

  const onMove = (e: MouseEvent<SVGRectElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    const px = PAD.l + ((e.clientX - box.left) / box.width) * iw;
    const py = PAD.t + ((e.clientY - box.top) / box.height) * ih;
    let best: Hover | null = null;
    let dist = 24 * 24;
    const scan = (series: Hover["series"], points: readonly ScatterPoint[]) => {
      for (const p of points) {
        const d = (x(p.x) - px) ** 2 + (y(p.y) - py) ** 2;
        if (d < dist) {
          dist = d;
          best = { series, point: p };
        }
      }
    };
    scan("calibration", calibration);
    scan("ml", ml);
    setHover(best);
  };
  const h = hover;
  const tipLeft = h !== null && x(h.point.x) > width / 2;
  const tipTop = h !== null && y(h.point.y) > height / 2;
  const error = h === null ? null : (h.point.x / h.point.y - 1) * 100;

  return (
    <div ref={ref} className="relative w-full">
      <svg width={width} height={height} role="img" aria-label={label} className="block">
        {ticks.map((t) => (
          <g key={`t${String(t)}`}>
            <line x1={PAD.l} x2={width - PAD.r} y1={y(t)} y2={y(t)} {...GRID} />
            <line x1={x(t)} x2={x(t)} y1={PAD.t} y2={PAD.t + ih} {...GRID} />
            <text x={PAD.l - 10} y={y(t) + 3} textAnchor="end" className="fill-fg-subtle font-mono text-[10px]">
              {tick(t)}
            </text>
            <text x={x(t)} y={height - 14} textAnchor="middle" className="fill-fg-subtle font-mono text-[10px]">
              {tick(t)}
            </text>
          </g>
        ))}
        <line x1={x(lo)} y1={y(lo)} x2={x(hi)} y2={y(hi)} className="stroke-fg-muted" strokeOpacity={0.4} strokeDasharray="4 4" />
        <text x={width - PAD.r} y={height - 1} textAnchor="end" className="fill-fg-subtle font-mono text-[9px] tracking-[0.12em] uppercase">
          predicted ({unit}) · log
        </text>
        <text x={PAD.l} y={PAD.t - 5} className="fill-fg-subtle font-mono text-[9px] tracking-[0.12em] uppercase">
          actual ({unit})
        </text>
        <g className="bm-in text-fg-muted">
          {calibration.map((p, i) => (
            <circle key={`c${String(i)}`} cx={x(p.x)} cy={y(p.y)} r={2.4} fill="currentColor" fillOpacity={0.5} />
          ))}
        </g>
        <g className="bm-in text-[#ffc93c]">
          {ml.map((p, i) => (
            <circle key={`m${String(i)}`} cx={x(p.x)} cy={y(p.y)} r={2.6} fill="currentColor" fillOpacity={0.85} />
          ))}
        </g>
        {h !== null && (
          <circle cx={x(h.point.x)} cy={y(h.point.y)} r={5} fill="none" strokeWidth={1.25} className={h.series === "ml" ? "stroke-[#ffc93c]" : "stroke-fg"} />
        )}
        <rect
          x={PAD.l}
          y={PAD.t}
          width={iw}
          height={ih}
          fill="transparent"
          onMouseMove={onMove}
          onMouseLeave={() => {
            setHover(null);
          }}
        />
        {h !== null && (
          <foreignObject
            x={tipLeft ? x(h.point.x) - TIP_W - 14 : x(h.point.x) + 14}
            y={tipTop ? Math.max(PAD.t, y(h.point.y) - 132) : y(h.point.y) + 8}
            width={TIP_W}
            height={128}
            className="pointer-events-none overflow-visible"
          >
            <div className="ov-tooltip px-3.5 py-2.5 text-xs" data-testid="chart-tooltip">
              <p className={cn("mb-1.5 font-mono text-[0.625rem] font-medium tracking-[0.12em] uppercase", h.series === "ml" ? "text-[#ffc93c]" : "text-fg-muted")}>
                {h.series === "ml" ? "ML" : "calibration"} · #{h.point.decisionId}
              </p>
              <p className="truncate text-fg">
                {nameOf(h.point.nodeId)}
                {h.point.family !== null && <span className="text-fg-muted"> · {h.point.family}</span>}
              </p>
              <p className="mt-1 flex justify-between gap-3 font-mono tabular-nums">
                <span className="text-fg-muted">predicted</span>
                <span className="text-fg">{tick(h.point.x)}</span>
              </p>
              <p className="flex justify-between gap-3 font-mono tabular-nums">
                <span className="text-fg-muted">actual</span>
                <span className="text-fg">{tick(h.point.y)}</span>
              </p>
              {error !== null && (
                <p className="flex justify-between gap-3 font-mono tabular-nums">
                  <span className="text-fg-muted">error</span>
                  <span className="text-fg">{`${error > 0 ? "+" : ""}${error.toFixed(1)} %`}</span>
                </p>
              )}
            </div>
          </foreignObject>
        )}
      </svg>
    </div>
  );
}

const HPAD = { l: 40, r: 12, t: 12, b: 28 };

function binText(b: ErrorBin): string {
  if (b.lo === Number.NEGATIVE_INFINITY) return `below ${String(b.hi)} %`;
  if (b.hi === Number.POSITIVE_INFINITY) return `above ${String(b.lo)} %`;
  return `${String(b.lo)} … ${String(b.hi)} %`;
}

/**
 * The estimate error's distribution: thin grey bars per bin, the zero-error
 * bin honey, the ±`band` % band as two faint dashed lines; hover gives the bin.
 */
export function ErrorHistogram({ bins, band, label, height = 210 }: { bins: readonly ErrorBin[]; band: number; label: string; height?: number }) {
  const [ref, width] = useElementWidth<HTMLDivElement>(420);
  const [hover, setHover] = useState<number | null>(null);
  const iw = Math.max(10, width - HPAD.l - HPAD.r);
  const ih = Math.max(10, height - HPAD.t - HPAD.b);
  const max = Math.max(1, ...bins.map((b) => b.count));
  const slot = iw / Math.max(1, bins.length);
  const barW = Math.max(2, Math.min(10, slot * 0.32));
  const first = bins[0]?.centre ?? 0;
  const step = bins.length > 1 ? (bins[1]?.centre ?? 1) - first : 1;
  const xc = (centre: number) => HPAD.l + ((centre - first) / step + 0.5) * slot;
  const y = (v: number) => HPAD.t + (1 - v / max) * ih;
  const gridSteps = [0.5, 1].map((f) => Math.round(max * f));
  const hb = hover === null ? undefined : bins[hover];
  return (
    <div ref={ref} className="relative w-full">
      <svg width={width} height={height} role="img" aria-label={label} className="block">
        {gridSteps.map((v) => (
          <g key={`g${String(v)}`}>
            <line x1={HPAD.l} x2={width - HPAD.r} y1={y(v)} y2={y(v)} {...GRID} />
            <text x={HPAD.l - 8} y={y(v) + 3} textAnchor="end" className="fill-fg-subtle font-mono text-[10px]">
              {v}
            </text>
          </g>
        ))}
        <line x1={HPAD.l} x2={width - HPAD.r} y1={y(0)} y2={y(0)} stroke="#ffffff" strokeOpacity={0.1} />
        {[-band, band].map((b) => (
          <line key={`b${String(b)}`} x1={xc(b)} x2={xc(b)} y1={HPAD.t} y2={HPAD.t + ih} className="stroke-fg-muted" strokeOpacity={0.35} strokeDasharray="3 4" />
        ))}
        {bins.map((b, i) => {
          const zero = b.lo <= 0 && b.hi > 0;
          const top = y(b.count);
          return (
            <rect
              key={`r${String(b.centre)}`}
              x={xc(b.centre) - barW / 2}
              y={top}
              width={barW}
              height={Math.max(0, y(0) - top)}
              rx={barW / 2}
              className={cn("pl-bar", zero ? "fill-[#ffc93c]" : "fill-fg-muted", hover === i && "pl-bar-on")}
              fillOpacity={zero ? 1 : 0.5}
            />
          );
        })}
        {bins.map((b) =>
          b.overflow || b.centre % 20 !== 0 ? null : (
            <text key={`x${String(b.centre)}`} x={xc(b.centre)} y={height - 10} textAnchor="middle" className="fill-fg-subtle font-mono text-[10px]">
              {b.centre > 0 ? `+${String(b.centre)}` : String(b.centre)}
            </text>
          ),
        )}
        <text x={width - HPAD.r} y={height - 1} textAnchor="end" className="fill-fg-subtle font-mono text-[9px] tracking-[0.12em] uppercase">
          estimate error %
        </text>
        <rect
          x={HPAD.l}
          y={HPAD.t}
          width={iw}
          height={ih}
          fill="transparent"
          onMouseMove={(e) => {
            const box = e.currentTarget.getBoundingClientRect();
            const i = Math.floor(((e.clientX - box.left) / box.width) * bins.length);
            setHover(Math.max(0, Math.min(bins.length - 1, i)));
          }}
          onMouseLeave={() => {
            setHover(null);
          }}
        />
        {hb !== undefined && hover !== null && (
          <foreignObject
            x={xc(hb.centre) > width / 2 ? xc(hb.centre) - 180 - 12 : xc(hb.centre) + 12}
            y={HPAD.t}
            width={180}
            height={64}
            className="pointer-events-none overflow-visible"
          >
            <div className="ov-tooltip px-3.5 py-2.5 text-xs">
              <p className="font-mono text-[0.625rem] tracking-[0.12em] text-fg-muted uppercase">{binText(hb)}</p>
              <p className="mt-1 font-mono text-fg tabular-nums">{hb.count} points</p>
            </div>
          </foreignObject>
        )}
      </svg>
    </div>
  );
}
