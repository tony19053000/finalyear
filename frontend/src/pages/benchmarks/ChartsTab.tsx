/**
 * Charts (D221): per family, median total time vs the swept parameter (one
 * series per node, the median's CI as error bars); per group, the strategy
 * comparison (median + CI per configuration). Everything is read from the
 * labels answer through the pure `chartData.ts` - no statistics here.
 *
 * Recharts draws with SVG attributes and React-set styles (allowed by the CSP,
 * like the Sparkline). Colours come from theme classes (`currentColor`), the
 * legend is plain HTML, and every chart has its numbers as a table too
 * (screen readers, and "what the API said" is checkable).
 */
import { useId, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ErrorBar,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
  type TooltipValueType,
} from "recharts";

import { Card } from "../../components/Card";
import { EmptyState } from "../../components/States";
import { formatMs } from "../../lib/format";
import { cn } from "../../lib/cn";
import type { LabelsOut } from "./benchApi";
import {
  familiesOf,
  familyCharts,
  formatParams,
  plottable,
  strategyChart,
  type FamilyChart,
  type PlotPoint,
  type StrategyBar,
  type StrategyChart,
} from "./chartData";
import { LabelsError } from "./LabelsTab";
import { useLabels } from "./benchQueries";

/** Series colours: theme tones (each >= 4.5:1 on every surface, tokens.test.ts). */
const SERIES_CLASSES = [
  "text-accent",
  "text-status-ok",
  "text-status-busy",
  "text-status-failed",
  "text-status-degraded",
  "text-fg",
  "text-status-unknown",
] as const;

const seriesClass = (i: number) => SERIES_CLASSES[i % SERIES_CLASSES.length] ?? "text-accent";
const DOT_CLASSES = ["bg-accent", "bg-status-ok", "bg-status-busy", "bg-status-failed", "bg-status-degraded", "bg-fg", "bg-status-unknown"];
const dotClass = (i: number) => DOT_CLASSES[i % DOT_CLASSES.length] ?? "bg-accent";

const WIDTH = 640;
const HEIGHT = 300;
const MARGIN = { top: 12, right: 24, bottom: 28, left: 16 };
const TICK = { fill: "currentColor", fontSize: 11 };

function axisNumber(value: number): string {
  if (!Number.isFinite(value)) return "";
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${(value / 1e9).toPrecision(3)}G`;
  if (abs >= 1e6) return `${(value / 1e6).toPrecision(3)}M`;
  if (abs >= 1e3) return `${(value / 1e3).toPrecision(3)}k`;
  return String(Number(value.toPrecision(3)));
}

function ChartTooltip({ active, payload }: TooltipContentProps<TooltipValueType, string | number>) {
  const item = payload[0]?.payload as (PlotPoint | StrategyBar) | undefined;
  if (!active || item === undefined) return null;
  return (
    <div className="rounded-md border border-border-strong bg-surface-raised px-2 py-1 text-xs text-fg shadow-overlay">
      {"x" in item && <div>x = {axisNumber(item.x)}</div>}
      {"label" in item && <div>{item.label}</div>}
      <div>
        median {formatMs(item.y)} (CI {formatMs(item.ciLow)} – {formatMs(item.ciHigh)}), n = {item.n}
      </div>
    </div>
  );
}

function Legend({ items }: { items: { key: string; label: string }[] }) {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-fg-muted" aria-label="Series">
      {items.map((item, i) => (
        <li key={item.key} className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className={cn("size-2.5 rounded-full", dotClass(i))} />
          <span className="font-mono">{item.label}</span>
        </li>
      ))}
    </ul>
  );
}

function FamilyPlot({ chart }: { chart: FamilyChart }) {
  const param = chart.param ?? "";
  const fixed = Object.keys(chart.fixed).length > 0 ? `; ${formatParams(chart.fixed)}` : "";
  const title = `${chart.family}: median total time vs ${param} (condition ${chart.condition}${fixed})`;
  const points = chart.series.reduce((n, s) => n + s.points.length, 0);
  return (
    <figure className="flex flex-col gap-2" data-testid="family-chart">
      <figcaption className="text-sm font-medium text-fg">{title}</figcaption>
      <Legend items={chart.series} />
      <div
        role="img"
        aria-label={`${title}: ${String(chart.series.length)} series, ${String(points)} points; the numbers are in the table below.`}
        className="overflow-x-auto text-fg-muted"
      >
        <ScatterChart width={WIDTH} height={HEIGHT} margin={MARGIN}>
          <CartesianGrid stroke="currentColor" strokeOpacity={0.15} />
          <XAxis
            type="number"
            dataKey="x"
            name={param}
            scale={chart.logX ? "log" : "auto"}
            domain={chart.logX ? ["dataMin", "dataMax"] : ["auto", "auto"]}
            tick={TICK}
            tickFormatter={axisNumber}
            stroke="currentColor"
            label={{ value: param + (chart.logX ? " (log)" : ""), position: "insideBottom", offset: -16, fill: "currentColor", fontSize: 11 }}
          />
          <YAxis type="number" dataKey="y" name="median total" tick={TICK} tickFormatter={(v: number) => formatMs(v)} stroke="currentColor" width={72} />
          <Tooltip content={ChartTooltip} isAnimationActive={false} />
          {chart.series.map((s, i) => (
            <Scatter
              key={s.key}
              name={s.label}
              data={s.points}
              className={seriesClass(i)}
              fill="currentColor"
              line={{ stroke: "currentColor", strokeWidth: 1.5 }}
              isAnimationActive={false}
            >
              <ErrorBar dataKey="err" direction="y" width={6} stroke="currentColor" strokeWidth={1} />
            </Scatter>
          ))}
        </ScatterChart>
      </div>
      <DataTable
        caption={title}
        head={[param, "series", "median", "CI low", "CI high", "n"]}
        rows={chart.series.flatMap((s) =>
          s.points.map((p) => ({
            key: `${s.key}|${p.configId}`,
            cells: [axisNumber(p.x), s.label, formatMs(p.y), formatMs(p.ciLow), formatMs(p.ciHigh), String(p.n)],
          })),
        )}
      />
    </figure>
  );
}

function DataTable({ caption, head, rows }: { caption: string; head: string[]; rows: { key: string; cells: string[] }[] }) {
  return (
    <details className="text-xs">
      <summary className="cursor-pointer text-fg-muted">Numbers ({rows.length})</summary>
      <table className="mt-2 border-collapse" data-testid="chart-numbers">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            {head.map((h) => (
              <th key={h} scope="col" className="px-2 py-1 text-left font-medium text-fg-subtle">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="border-t border-border">
              {r.cells.map((c, i) => (
                <td key={`${String(i)}-${head[i] ?? ""}`} className="px-2 py-1 font-mono tabular-nums">
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

function StrategyPlot({ chart }: { chart: StrategyChart }) {
  const title = `${chart.family} (${formatParams(chart.params)}, condition ${chart.condition}): strategy comparison - label ${chart.label}`;
  if (chart.bars.length === 0) {
    return <EmptyState title="Nothing measured in this group yet">Its configurations have no successful run records.</EmptyState>;
  }
  return (
    <figure className="flex flex-col gap-2" data-testid="strategy-chart">
      <figcaption className="text-sm font-medium text-fg">{title}</figcaption>
      <div
        role="img"
        aria-label={`${title}: ${String(chart.bars.length)} configurations; the numbers are in the table below.`}
        className="overflow-x-auto text-fg-muted"
      >
        <BarChart width={WIDTH} height={HEIGHT} data={chart.bars} margin={MARGIN}>
          <CartesianGrid stroke="currentColor" strokeOpacity={0.15} vertical={false} />
          <XAxis type="category" dataKey="label" tick={TICK} stroke="currentColor" interval={0} />
          <YAxis type="number" tick={TICK} tickFormatter={(v: number) => formatMs(v)} stroke="currentColor" width={72} />
          <Tooltip content={ChartTooltip} isAnimationActive={false} cursor={false} />
          <Bar dataKey="y" name="median total" className="text-accent" fill="currentColor" fillOpacity={0.7} isAnimationActive={false}>
            <ErrorBar dataKey="err" direction="y" width={8} stroke="currentColor" strokeWidth={1.5} />
          </Bar>
        </BarChart>
      </div>
      <DataTable
        caption={title}
        head={["configuration", "median", "CI low", "CI high", "n", "best"]}
        rows={chart.bars.map((b) => ({
          key: b.configId,
          cells: [b.label, formatMs(b.y), formatMs(b.ciLow), formatMs(b.ciHigh), String(b.n), b.best ? "yes" : ""],
        }))}
      />
    </figure>
  );
}

const select = "h-8 max-w-full rounded-md border border-border-strong bg-surface px-2 text-sm text-fg";

function Charts({ labels }: { labels: LabelsOut }) {
  const families = useMemo(() => familiesOf(labels), [labels]);
  const [picked, setPicked] = useState<string | null>(null);
  const family = picked !== null && families.includes(picked) ? picked : (families[0] ?? null);
  const charts = useMemo(() => (family === null ? [] : familyCharts(labels, family)), [labels, family]);
  const groups = useMemo(() => labels.groups.filter((g) => g.family === family), [labels, family]);
  const [pickedGroup, setPickedGroup] = useState<string | null>(null);
  const group = groups.find((g) => g.group_id === pickedGroup) ?? groups.find((g) => strategyChart(g).bars.length >= 2) ?? groups[0];
  const familyId = useId();
  const groupId = useId();

  if (family === null) {
    return <EmptyState title="No data to chart yet">Charts appear once the campaign has run records.</EmptyState>;
  }
  const drawable = charts.filter(plottable);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={familyId} className="text-sm text-fg-muted">
          Family
        </label>
        <select
          id={familyId}
          className={select}
          value={family}
          onChange={(e) => {
            setPicked(e.target.value);
            setPickedGroup(null);
          }}
        >
          {families.map((f) => (
            <option key={f} value={f}>
              {f}
            </option>
          ))}
        </select>
      </div>
      <Card title="Runtime vs parameter">
        {drawable.length === 0 ? (
          <EmptyState title="Not enough data for this chart">
            {charts.some((c) => c.param !== null)
              ? "No node has a successful median at two or more parameter values yet."
              : `The campaign measured ${family} at one parameter point only: there is no swept parameter to plot. The strategy comparison below compares its configurations.`}
          </EmptyState>
        ) : (
          <div className="flex flex-col gap-6">
            {drawable.map((c) => (
              <FamilyPlot key={c.key} chart={c} />
            ))}
          </div>
        )}
      </Card>
      <Card title="Strategy comparison">
        {group === undefined ? (
          <EmptyState title="No groups in this family" />
        ) : (
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <label htmlFor={groupId} className="text-sm text-fg-muted">
                Group
              </label>
              <select
                id={groupId}
                className={select}
                value={group.group_id}
                onChange={(e) => {
                  setPickedGroup(e.target.value);
                }}
              >
                {groups.map((g) => (
                  <option key={g.group_id} value={g.group_id}>
                    {`${formatParams(g.params)} · ${g.condition} · ${g.label}`}
                  </option>
                ))}
              </select>
            </div>
            <StrategyPlot chart={strategyChart(group)} />
          </div>
        )}
      </Card>
    </div>
  );
}

export function ChartsTab({ campaignId }: { campaignId: string }) {
  const labels = useLabels(campaignId);
  if (labels.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading chart data…
      </p>
    );
  }
  if (labels.isError) return <LabelsError error={labels.error} onRetry={() => void labels.refetch()} />;
  return <Charts labels={labels.data} />;
}
