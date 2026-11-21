/**
 * Fabric load over the last 10 minutes (browser-recorded, fabricHistory.ts).
 * The legend labels toggle their lines; ↗ unfolds the card downward (Alerts
 * folds away) to one small chart per laptop - offline laptops left out.
 */
import { useState } from "react";

import type { NodeSummary } from "../../api/endpoints";
import { useFabricHistory, type HistoryPoint } from "./fabricHistory";
import { LoadLineChart, type SeriesPoint } from "./LoadLineChart";
import { ArrowUpRight, OverviewCard } from "./OverviewCard";
import { members, RESOURCE_LABEL, RESOURCES, type NodeLoad, type Resource } from "./overviewModel";
import { SERIES_CLASS } from "./seriesStyle";

function pct(v: number | null | undefined): string {
  return v == null ? "—" : `${String(Math.round(v))}%`;
}

function nodeSeries(history: readonly HistoryPoint[], nodeId: string): SeriesPoint[] {
  const none: NodeLoad = { cpu: null, ram: null, gpu: null };
  return history.map((p) => ({ t: p.t, v: p.nodes[nodeId] ?? none }));
}

function Recording({ points }: { points: number }) {
  if (points >= 2) return null;
  return (
    <p className="pointer-events-none absolute inset-0 flex items-center justify-center text-xs text-fg-muted" role="status">
      Recording - the graph fills over the next 10 minutes.
    </p>
  );
}

export interface LoadHistoryCardProps {
  nodes: readonly NodeSummary[];
  expanded?: boolean;
  onToggle?: () => void;
}

export function LoadHistoryCard({ nodes, expanded = false, onToggle }: LoadHistoryCardProps) {
  const history = useFabricHistory();
  const [hidden, setHidden] = useState<ReadonlySet<Resource>>(new Set());
  const series: SeriesPoint[] = history.map((p) => ({ t: p.t, v: p.fabric }));
  const laptops = members(nodes).filter((n) => n.state !== "offline");

  const toggle = (r: Resource) => {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(r)) next.delete(r);
      else next.add(r);
      return next;
    });
  };

  return (
    <OverviewCard
      title={
        <span className="flex items-center gap-3 whitespace-nowrap">
          Load
          <span className="font-mono text-[0.625rem] font-medium tracking-[0.14em] text-fg-muted uppercase">10 min</span>
        </span>
      }
      data-testid="load-card"
      className={expanded ? "h-full min-h-0" : undefined}
      actions={
        <button
          type="button"
          className="ov-icon-btn ov-icon-btn-honey"
          aria-label={expanded ? "Close the per-laptop charts" : "Expand: one chart per laptop"}
          aria-expanded={expanded}
          onClick={onToggle}
        >
          {expanded ? <span aria-hidden="true" className="text-base leading-none">×</span> : <ArrowUpRight />}
        </button>
      }
    >
      <div className="mt-1 mb-3 flex flex-wrap gap-5" role="group" aria-label="Show or hide lines">
        {RESOURCES.map((r) => {
          const off = hidden.has(r);
          return (
            <button
              key={r}
              type="button"
              aria-pressed={!off}
              onClick={() => {
                toggle(r);
              }}
              className={`flex items-center gap-2 py-1 transition-opacity duration-300 hover:opacity-80 ${off ? "opacity-35" : ""}`}
              data-testid={`legend-${r}`}
            >
              <span aria-hidden="true" className={`h-2 w-2 rounded-full ${SERIES_CLASS[r].bg}`} />
              <span className={`font-mono text-[0.6875rem] font-medium tracking-[0.14em] uppercase ${SERIES_CLASS[r].text}`}>
                {RESOURCE_LABEL[r]}
              </span>
            </button>
          );
        })}
      </div>
      {/* the combined chart folds away when the per-laptop charts open */}
      <div className={`ov-fold ${expanded ? "ov-fold-closed" : ""}`} aria-hidden={expanded}>
        <div className="min-h-0">
      <div className="relative">
          <LoadLineChart points={series} hidden={hidden} height={150} label="Fabric CPU, RAM and GPU load over the last 10 minutes" />
          <Recording points={series.length} />
        </div>
        </div>
      </div>
      {/* per-laptop charts unfold below the fabric chart */}
      <div className={`ov-fold ov-fold-grow ${expanded ? "" : "ov-fold-closed"}`} aria-hidden={!expanded}>
        <div className="min-h-0">
          {laptops.length === 0 ? (
            <p className="pt-4 text-sm text-fg-muted">No laptops online.</p>
          ) : (
            <ul className={`ov-scroll ov-scroll-none mt-1 flex h-full flex-col gap-2 overflow-y-auto ${expanded ? "ov-alerts-open" : ""}`} data-testid="per-laptop-charts">
              {laptops.map((n) => {
                const s = nodeSeries(history, n.node_id);
                const last = s.at(-1)?.v;
                return (
                  <li key={n.node_id} className="rounded-2xl bg-white/[0.025] px-3.5 pt-2.5 pb-2">
                    <div className="mb-1 flex items-baseline justify-between gap-3">
                      <span className="truncate font-mono text-[0.625rem] tracking-[0.08em] text-fg-muted">{n.display_name || n.hostname}</span>
                      <span className="flex gap-3 font-mono text-[0.625rem] tabular-nums">
                        {RESOURCES.map((r) => (
                          <span key={r} className={SERIES_CLASS[r].text}>
                            {RESOURCE_LABEL[r]} {pct(last?.[r])}
                          </span>
                        ))}
                      </span>
                    </div>
                    <div className="relative">
                      <LoadLineChart points={s} hidden={hidden} height={84} detailed={false} compact label={`${n.hostname} load`} />
                      <Recording points={s.filter((p) => p.v.cpu !== null).length} />
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </OverviewCard>
  );
}
