/**
 * One resource's live line (node detail): 0-100 % over the controller's
 * sample window, in the resource's series colour (CPU honey, RAM teal, GPU
 * coral), faint grid lines, a smooth monotone curve and a frosted hover
 * read-out with a tiny value bar - the Overview's LoadLineChart language,
 * one series per chart. Screen readers get a text summary.
 */
import { useEffect, useRef, useState, type MouseEvent } from "react";

import type { SparkPoint } from "../../components/Sparkline";
import { SERIES_CLASS } from "../overview/seriesStyle";
import { lineSummary, monotonePath } from "./chartPath";

export type SeriesTone = "cpu" | "ram" | "gpu";

export interface ChartLine {
  key: string;
  label: string;
  points: readonly SparkPoint[];
  /** A secondary line (second GPU, …): same colour, fainter. */
  dim?: boolean;
}

export interface MetricChartProps {
  tone: SeriesTone;
  lines: readonly ChartLine[];
  /** The sample window the x axis spans (ms). */
  windowMs: number;
  /** Shown over the empty grid when no line has a sample. */
  empty?: string;
}

const HEIGHT = 120;
const PAD = { l: 30, r: 8, t: 10, b: 22 };
const TIP_W = 164;

function pct(v: number): string {
  return `${String(Math.round(v))}%`;
}

function useWidth(fallback: number) {
  const ref = useRef<HTMLDivElement>(null);
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

function nearest(points: readonly SparkPoint[], t: number): SparkPoint | undefined {
  let best: SparkPoint | undefined;
  for (const p of points) {
    if (best === undefined || Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
  }
  return best;
}

function clock(t: number): string {
  return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function MetricChart({ tone, lines, windowMs, empty }: MetricChartProps) {
  const [ref, width] = useWidth(360);
  const [hoverT, setHoverT] = useState<number | null>(null);
  const series = SERIES_CLASS[tone];
  const now = Math.max(0, ...lines.map((l) => l.points.at(-1)?.t ?? 0));
  const from = now - windowMs;
  const span = Math.max(1, windowMs);
  const iw = Math.max(10, width - PAD.l - PAD.r);
  const ih = HEIGHT - PAD.t - PAD.b;
  const x = (t: number) => PAD.l + ((t - from) / span) * iw;
  const y = (v: number) => PAD.t + (1 - Math.max(0, Math.min(100, v)) / 100) * ih;
  const shown = lines.map((l) => ({ ...l, points: l.points.filter((p) => p.t >= from) }));
  const hasData = shown.some((l) => l.points.length > 0);
  const primary = shown[0];
  const summary = primary === undefined ? "no samples" : lineSummary(primary.label, lines[0]?.points ?? []);
  const hoverPoint = hoverT === null || primary === undefined ? undefined : nearest(primary.points, hoverT);

  const onMove = (e: MouseEvent<SVGRectElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    setHoverT(from + ((e.clientX - box.left) / box.width) * span);
  };

  const tipH = 34 + 22 * shown.length;
  const hx = hoverPoint === undefined ? 0 : x(hoverPoint.t);

  return (
    <div ref={ref} role="img" aria-label={summary} className="relative h-[120px] w-full">
      <svg width={width} height={HEIGHT} aria-hidden="true" className="block overflow-visible">
        {[0, 25, 50, 75, 100].map((g) => (
          <line
            key={g}
            x1={PAD.l}
            x2={width - PAD.r}
            y1={y(g)}
            y2={y(g)}
            stroke="currentColor"
            className="text-fg"
            strokeOpacity={g === 0 ? 0.12 : 0.045}
          />
        ))}
        {[0, 50, 100].map((g) => (
          <text key={`l${String(g)}`} x={PAD.l - 10} y={y(g) + 3} textAnchor="end" className="fill-fg-subtle font-mono text-[9px]">
            {g}
          </text>
        ))}
        <text x={PAD.l} y={HEIGHT - 5} className="fill-fg-subtle font-mono text-[9px]">
          −{Math.round(windowMs / 60_000)} min
        </text>
        <text x={width - PAD.r} y={HEIGHT - 5} textAnchor="end" className="fill-fg-subtle font-mono text-[9px]">
          now
        </text>
        {shown.map((l) => {
          const d = monotonePath(l.points.map((p) => ({ x: x(p.t), y: y(p.v) })));
          if (d === "") return null;
          return (
            <path
              key={l.key}
              d={d}
              fill="none"
              strokeWidth={1.75}
              strokeLinejoin="round"
              strokeLinecap="round"
              strokeOpacity={l.dim === true ? 0.5 : 1}
              className={`nd-line ${series.stroke}`}
            />
          );
        })}
        {shown.map((l) => {
          const last = l.points.at(-1);
          if (last === undefined) return null;
          return (
            <circle
              key={`end-${l.key}`}
              cx={x(last.t)}
              cy={y(last.v)}
              r={3.25}
              fillOpacity={l.dim === true ? 0.5 : 1}
              className={`nd-end-dot ${series.fill}`}
            />
          );
        })}
        {hoverPoint !== undefined && (
          <g>
            <line x1={hx} x2={hx} y1={PAD.t} y2={PAD.t + ih} className="stroke-fg-muted" strokeOpacity={0.6} strokeDasharray="3 3" />
            {shown.map((l) => {
              const p = nearest(l.points, hoverPoint.t);
              return p === undefined ? null : (
                <circle key={`h-${l.key}`} cx={x(p.t)} cy={y(p.v)} r={4} strokeWidth={2} className={`${series.stroke} fill-bg`} />
              );
            })}
          </g>
        )}
        {hasData && (
          <rect
            x={PAD.l}
            y={PAD.t}
            width={iw}
            height={ih}
            fill="transparent"
            onMouseMove={onMove}
            onMouseLeave={() => {
              setHoverT(null);
            }}
          />
        )}
        {hoverPoint !== undefined && (
          <foreignObject
            x={hx > width / 2 ? hx - TIP_W - 12 : hx + 12}
            y={PAD.t}
            width={TIP_W}
            height={tipH}
            className="pointer-events-none overflow-visible"
          >
            <div className="ov-tooltip px-3.5 py-2.5 text-xs" data-testid="metric-tooltip">
              <p className="mb-1.5 font-mono text-[0.625rem] tracking-[0.12em] text-fg-muted uppercase">{clock(hoverPoint.t)}</p>
              {shown.map((l) => {
                const p = nearest(l.points, hoverPoint.t);
                return (
                  <p key={l.key} className="flex items-center gap-2.5 py-0.5">
                    <span className={`w-12 truncate font-mono text-[0.625rem] font-medium tracking-[0.12em] uppercase ${series.text}`}>
                      {l.label}
                    </span>
                    <svg aria-hidden="true" width="48" height="4" className="shrink-0">
                      <rect width="48" height="4" rx="2" fill="currentColor" className="text-fg" fillOpacity="0.1" />
                      {p !== undefined && <rect width={(48 * Math.max(0, Math.min(100, p.v))) / 100} height="4" rx="2" className={series.fill} />}
                    </svg>
                    <span className="ml-auto font-mono text-fg tabular-nums">{p === undefined ? "—" : pct(p.v)}</span>
                  </p>
                );
              })}
            </div>
          </foreignObject>
        )}
      </svg>
      {!hasData && empty !== undefined && (
        <p className="pointer-events-none absolute inset-x-0 top-1/2 -translate-y-1/2 pl-8 text-center text-xs text-fg-subtle">{empty}</p>
      )}
    </div>
  );
}
