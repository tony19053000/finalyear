/**
 * Hand-written SVG line graph: CPU / RAM / GPU in %, 0-100, over the last
 * 10 minutes of browser-recorded history (fabricHistory.ts). A dot ends
 * each line; hovering shows a crosshair with all three values.
 */
import { useEffect, useRef, useState, type MouseEvent } from "react";

import { WINDOW_MS } from "./fabricHistory";
import { RESOURCE_LABEL, RESOURCES, type NodeLoad, type Resource } from "./overviewModel";
import { SERIES_CLASS } from "./seriesStyle";

export interface SeriesPoint {
  t: number;
  v: NodeLoad;
}

export interface LoadLineChartProps {
  points: readonly SeriesPoint[];
  hidden?: ReadonlySet<Resource>;
  height?: number;
  /** Axis labels and the hover crosshair. */
  detailed?: boolean;
  /** Small charts: stretch the recorded span across the full width (no empty left part) and draw faint grid lines. */
  compact?: boolean;
  label: string;
}

const TIP_W = 150;
const PAD = { l: 30, r: 12, t: 10, b: 20 };

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

/** Path segments of one series (a gap where the value is not reported). */
function pathOf(points: readonly SeriesPoint[], r: Resource, x: (t: number) => number, y: (v: number) => number): string {
  let d = "";
  let open = false;
  for (const p of points) {
    const v = p.v[r];
    if (v === null) {
      open = false;
      continue;
    }
    d += `${open ? "L" : "M"}${x(p.t).toFixed(1)},${y(v).toFixed(1)}`;
    open = true;
  }
  return d;
}

function clock(t: number): string {
  return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function LoadLineChart({ points, hidden, height = 220, detailed = true, compact = false, label }: LoadLineChartProps) {
  const [ref, width] = useWidth<HTMLDivElement>(560);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const now = points.at(-1)?.t ?? 0;
  const first = points[0]?.t ?? now;
  const from = compact && now - first > 0 ? Math.max(now - WINDOW_MS, first) : now - WINDOW_MS;
  const span = Math.max(1, now - from);
  const pad = detailed ? PAD : { l: 4, r: 8, t: 6, b: 4 };
  const iw = Math.max(10, width - pad.l - pad.r);
  const ih = Math.max(10, height - pad.t - pad.b);
  const x = (t: number) => pad.l + ((t - from) / span) * iw;
  const y = (v: number) => pad.t + (1 - Math.max(0, Math.min(100, v)) / 100) * ih;
  const visible = RESOURCES.filter((r) => !(hidden?.has(r) ?? false));
  const hovered = hoverIdx === null ? undefined : points[hoverIdx];

  const onMove = (e: MouseEvent<SVGRectElement>) => {
    if (points.length === 0) return;
    const box = e.currentTarget.getBoundingClientRect();
    const t = from + ((e.clientX - box.left) / box.width) * span;
    let best = 0;
    for (let i = 1; i < points.length; i++) {
      const p = points[i];
      const b = points[best];
      if (p !== undefined && b !== undefined && Math.abs(p.t - t) < Math.abs(b.t - t)) best = i;
    }
    setHoverIdx(best);
  };

  return (
    <div ref={ref} className={`relative w-full ${height >= 200 ? "h-[220px]" : height >= 150 ? "h-[150px]" : height >= 120 ? "h-[120px]" : "h-[84px]"}`}>
      <svg width={width} height={height} role="img" aria-label={label} className="block">
        {compact &&
          [0, 50, 100].map((g) => (
            <line key={`cg${String(g)}`} x1={pad.l} x2={width - pad.r} y1={y(g)} y2={y(g)} stroke="#ffffff" strokeOpacity={g === 0 ? 0.1 : 0.05} />
          ))}
        {detailed &&
          [0, 25, 50, 75, 100].map((g) => (
            <g key={g}>
              <line
                x1={pad.l}
                x2={width - pad.r}
                y1={y(g)}
                y2={y(g)}
                stroke="#ffffff"
                strokeOpacity={g === 0 ? 0.12 : 0.05}
              />
              <text x={pad.l - 8} y={y(g) + 3} textAnchor="end" className="fill-fg-subtle font-mono text-[9px]">
                {g}
              </text>
            </g>
          ))}
        {detailed &&
          [
            [from, "-10m"],
            [from + WINDOW_MS / 2, "-5m"],
            [now, "now"],
          ].map(([t, txt]) => (
            <text
              key={String(txt)}
              x={x(Number(t))}
              y={height - 4}
              textAnchor={txt === "now" ? "end" : txt === "-10m" ? "start" : "middle"}
              className="fill-fg-subtle font-mono text-[9px]"
            >
              {txt}
            </text>
          ))}
        {visible.map((r) => {
          const d = pathOf(points, r, x, y);
          if (d === "") return null;
          return (
            <g key={r} className={`ov-line ${SERIES_CLASS[r].stroke}`}>
              <path d={d} fill="none" strokeWidth={1.75} strokeLinejoin="round" strokeLinecap="round" />
            </g>
          );
        })}
        {visible.map((r) => {
          const last = [...points].reverse().find((p) => p.v[r] !== null);
          if (last === undefined) return null;
          const v = last.v[r] ?? 0;
          return (
            <g key={`dot-${r}`} className={SERIES_CLASS[r].text}>
              <circle cx={x(last.t)} cy={y(v)} r={3.5} fill="currentColor" />
            </g>
          );
        })}
        {detailed && hovered !== undefined && (
          <g>
            <line x1={x(hovered.t)} x2={x(hovered.t)} y1={pad.t} y2={pad.t + ih} className="stroke-fg-muted" strokeDasharray="3 3" />
            {visible.map((r) => {
              const v = hovered.v[r];
              return v === null ? null : (
                <circle key={r} cx={x(hovered.t)} cy={y(v)} r={4} className={`${SERIES_CLASS[r].stroke} fill-surface`} strokeWidth={2} />
              );
            })}
          </g>
        )}
        {detailed && (
          <rect
            x={pad.l}
            y={pad.t}
            width={iw}
            height={ih}
            fill="transparent"
            onMouseMove={onMove}
            onMouseLeave={() => {
              setHoverIdx(null);
            }}
          />
        )}
        {detailed && hovered !== undefined && (
          <foreignObject
            x={x(hovered.t) > width / 2 ? x(hovered.t) - TIP_W - 12 : x(hovered.t) + 12}
            y={pad.t}
            width={TIP_W}
            height={116}
            className="pointer-events-none overflow-visible"
          >
          <div className="ov-tooltip px-3.5 py-2.5 text-xs" data-testid="line-tooltip">
          <p className="mb-2 font-mono text-[0.625rem] tracking-[0.12em] text-fg-muted uppercase">{clock(hovered.t)}</p>
          {RESOURCES.map((r) => {
            const v = hovered.v[r];
            return (
              <p key={r} className="flex items-center gap-2.5 py-0.5">
                <span className={`w-8 font-mono text-[0.625rem] font-medium tracking-[0.12em] uppercase ${SERIES_CLASS[r].text}`}>
                  {RESOURCE_LABEL[r]}
                </span>
                <svg aria-hidden="true" width="64" height="4" className="shrink-0">
                  <rect width="64" height="4" rx="2" fill="#ffffff" fillOpacity="0.1" />
                  {v != null && <rect width={(64 * Math.max(0, Math.min(100, v))) / 100} height="4" rx="2" className={SERIES_CLASS[r].fill} />}
                </svg>
                <span className="ml-auto font-mono text-fg tabular-nums">{v == null ? "—" : `${String(Math.round(v))}%`}</span>
              </p>
            );
          })}
          </div>
          </foreignObject>
        )}
      </svg>
    </div>
  );
}
