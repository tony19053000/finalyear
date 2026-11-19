/**
 * Total hardware of the live hive: CPU cores, RAM and GPU memory. Equal slices
 * (the units differ). The ring cycles on its own: each slice in turn grows while the other two fade and the centre shows its total. Hover
 * takes over and pauses the cycle.
 * No usage is shown here - the Load card has that. No GPU -> "No GPU", no slice.
 * ↗ widens the card to the left (Job progress and Speed-up fold away) with
 * what each laptop brings; the column of the highlighted resource lights up.
 */
import { useEffect, useState } from "react";

import type { NodeSummary } from "../../api/endpoints";

import { ArrowUpRight, OverviewCard } from "./OverviewCard";
import { members, RESOURCE_LABEL, type Resource, type ResourceTotals } from "./overviewModel";
import { SERIES_CLASS, totalText } from "./seriesStyle";

function percentOfResource(r: Resource, t: ResourceTotals): number | null {
  return t[r]?.percent ?? null;
}

const R = 54;
const W = 16;
const C = 2 * Math.PI * R;
const CYCLE_MS = 3000;
const GiB = 1024 ** 3;

function gbn(v: number): string {
  return v < 10 ? v.toFixed(1).replace(/\.0$/, "") : String(Math.round(v));
}

export interface ResourceDonutCardProps {
  totals: ResourceTotals;
  nodes?: readonly NodeSummary[];
  /** node_id -> logical cores. */
  cores?: ReadonlyMap<string, number>;
  expanded?: boolean;
  onToggle?: () => void;
}

export function ResourceDonutCard({ totals, nodes = [], cores, expanded = false, onToggle }: ResourceDonutCardProps) {
  const [pointed, setHover] = useState<Resource | null>(null);
  const [tick, setTick] = useState(0);
  const shown = (["cpu", "ram", "gpu"] as const).filter((r) => percentOfResource(r, totals) !== null);

  useEffect(() => {
    if (pointed !== null) return undefined;
    const t = window.setInterval(() => {
      setTick((n) => n + 1);
    }, CYCLE_MS);
    return () => {
      window.clearInterval(t);
    };
  }, [pointed]);

  const hover: Resource | null = pointed ?? shown[tick % Math.max(1, shown.length)] ?? null;
  const gap = shown.length > 1 ? 10 : 0;

  let start = 0;
  const slices = shown.map((r) => {
    const frac = 1 / shown.length;
    const len = Math.max(0.5, frac * C - gap);
    const s = { r, len, offset: -start };
    start += frac * C;
    return s;
  });

  // per-laptop hardware for the expanded table (offline laptops left out)
  const rows = members(nodes)
    .filter((n) => n.state !== "offline")
    .map((n) => ({
      id: n.node_id,
      name: n.display_name || n.hostname,
      v: {
        cpu: cores?.get(n.node_id) ?? null,
        ram: n.ram_total_bytes ? n.ram_total_bytes / GiB : null,
        gpu: n.gpus.length === 0 ? null : n.gpus.reduce((a, g) => a + g.vram_total_bytes, 0) / GiB,
      },
    }));

  const hoverText = hover === null ? null : totalText(hover, totals);
  const [centre, centreLabel] =
    hoverText === null
      ? [String(totals.reporting), totals.reporting === 1 ? "laptop" : "laptops"]
      : [hoverText.split(" ")[0], `${RESOURCE_LABEL[hover as Resource]} ${hoverText.split(" ").slice(1).join(" ")}`];

  return (
    <OverviewCard
      title="Resources"
      hideTitle
      data-testid="resources-card"
      className="h-[12.5rem]"
      actions={
        <button
          type="button"
          className={`ov-icon-btn ${expanded ? "ov-icon-btn-honey" : ""}`}
          aria-label={expanded ? "Close the per-laptop hardware" : "Show what each laptop brings"}
          aria-expanded={expanded}
          onClick={onToggle}
        >
          {expanded ? <span aria-hidden="true" className="text-base leading-none">×</span> : <ArrowUpRight />}
        </button>
      }
    >
      {totals.reporting === 0 ? (
        <p className="mt-auto text-sm text-fg-muted" data-testid="resources-empty">
          No node is reporting resources right now.
        </p>
      ) : (
        <div className={`flex flex-1 items-center gap-8 ${expanded ? "justify-start" : "flex-col justify-center gap-4"}`}>
          <div className="relative h-[9.5rem] w-[9.5rem] shrink-0">
            <svg viewBox="0 0 140 140" className="h-full w-full -rotate-90" role="img" aria-label="Total CPU, RAM and GPU">
              <circle cx="70" cy="70" r={R} fill="none" strokeWidth={W} className="stroke-surface-raised" />
              {slices.map((s) => (
                <circle
                  key={s.r}
                  cx="70"
                  cy="70"
                  r={R}
                  fill="none"
                  strokeLinecap="round"
                  strokeWidth={hover === s.r ? W + 4 : W}
                  strokeDasharray={`${String(s.len)} ${String(C - s.len)}`}
                  strokeDashoffset={s.offset - gap / 2}
                  className={`ov-donut-slice cursor-pointer ${SERIES_CLASS[s.r].stroke}`}
                  opacity={hover !== null && hover !== s.r ? 0.35 : 1}
                  data-testid={`donut-${s.r}`}
                  onMouseEnter={() => {
                    setHover(s.r);
                  }}
                  onMouseLeave={() => {
                    setHover(null);
                  }}
                >
                  <title>{`${RESOURCE_LABEL[s.r]} · ${totalText(s.r, totals) ?? ""}`}</title>
                </circle>
              ))}
            </svg>
            <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
              <span key={hover ?? "laptops"} className="ov-donut-centre flex flex-col items-center">
                <span className="ov-figure text-5xl text-fg" data-testid="util-average">
                  {centre}
                </span>
                <span className="mt-1 font-mono text-[0.625rem] tracking-[0.12em] text-fg-muted uppercase">{centreLabel}</span>
              </span>
            </div>
          </div>
          {expanded && (
            <div className="ov-res-table flex min-w-0 flex-1 flex-col self-stretch" data-testid="resources-per-laptop">
              <div className="grid grid-cols-[minmax(0,1.2fr)_repeat(3,minmax(0,1fr))] gap-x-4 pr-12 pb-3 font-mono text-[0.5625rem] tracking-[0.14em] text-fg-subtle uppercase">
                <span>node</span>
                {(["cpu", "ram", "gpu"] as const).map((r) => (
                  <span key={r} className={`text-center transition-opacity duration-500 ${SERIES_CLASS[r].text} ${hover === r ? "" : "opacity-50"}`}>
                    {r === "cpu" ? "cores" : r === "ram" ? "RAM GB" : "VRAM GB"}
                  </span>
                ))}
              </div>
              <ul className="ov-scroll ov-scroll-none flex min-h-0 flex-1 flex-col justify-around overflow-y-auto">
                {rows.map((row) => (
                  <li key={row.id} className="grid grid-cols-[minmax(0,1.2fr)_repeat(3,minmax(0,1fr))] items-center gap-x-4 pr-12">
                    <span className="truncate font-mono text-[0.6875rem] text-fg-muted">{row.name}</span>
                    {(["cpu", "ram", "gpu"] as const).map((r) => {
                      const v = row.v[r];
                      const on = hover === r;
                      return (
                        <span
                          key={r}
                          className={`text-center font-mono text-sm tabular-nums transition-colors duration-500 ${on ? "text-fg" : "text-fg-subtle"}`}
                        >
                          {v === null ? "—" : r === "cpu" ? String(v) : gbn(v)}
                        </span>
                      );
                    })}
                  </li>
                ))}
              </ul>
            </div>
          )}
          <dl className="sr-only">
            {(["cpu", "ram", "gpu"] as const).map((r) => {
              const text = totalText(r, totals);
              return (
                <div
                  key={r}
                  className="flex items-center justify-between gap-3 rounded-full px-1"
                  onMouseEnter={() => {
                    if (text !== null) setHover(r);
                  }}
                  onMouseLeave={() => {
                    setHover(null);
                  }}
                >
                  <dt className="flex items-center gap-2 text-xs text-fg-muted">
                    <span
                      aria-hidden="true"
                      className={`h-2.5 w-2.5 rounded-full ${text === null ? "bg-border-strong" : SERIES_CLASS[r].bg}`}
                    />
                    {RESOURCE_LABEL[r]}
                  </dt>
                  <dd
                    className={`font-mono text-xs whitespace-nowrap tabular-nums ${text === null ? "text-fg-subtle" : "text-fg"}`}
                    data-testid={`util-${r}`}
                  >
                    {text ?? (r === "gpu" ? "No GPU" : "not reported")}
                  </dd>
                </div>
              );
            })}
          </dl>
        </div>
      )}
    </OverviewCard>
  );
}
