/**
 * Charts (D221): per family, median total time vs the swept parameter (one
 * series per node, the median's CI as error bars); per group, the strategy
 * comparison (median + CI per configuration). Everything is read from the
 * labels answer through the pure `chartData.ts` - no statistics here.
 *
 * Drawn in the Overview's chart language (BenchCharts.tsx): thin lines,
 * legend labels only, a faint grid, a frosted tooltip; honey for the best
 * series / the best configuration, grey for the local baseline (the run kept
 * on the laptop that submits it). Every chart has its numbers as a table too
 * (screen readers, and "what the API said" is checkable).
 */
import { useId, useMemo, useState } from "react";

import { EmptyState } from "../../components/States";
import { ListboxField } from "../../components/jobs/ListboxField";
import { formatMs } from "../../lib/format";
import type { LabelsOut } from "./benchApi";
import { CompareBars, LineChart, Legend, type CompareBar, type LineSeries } from "./BenchCharts";
import { toneClass, type Tone } from "./benchShared";
import { FrostCard, Note } from "./benchUi";
import { useNodeNames } from "./benchShared";
import { optionText, strategyText } from "./campaignView";
import { familiesOf, familyCharts, formatParams, plottable, strategyChart, type FamilyChart, type StrategyChart } from "./chartData";
import { LabelsError } from "./LabelsTab";
import { useLabels } from "./benchQueries";

function axisNumber(value: number): string {
  if (!Number.isFinite(value)) return "";
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${(value / 1e9).toPrecision(3)}G`;
  if (abs >= 1e6) return `${(value / 1e6).toPrecision(3)}M`;
  if (abs >= 1e3) return `${(value / 1e3).toPrecision(3)}k`;
  return String(Number(value.toPrecision(3)));
}

/** A short axis label: 10.0M -> 10M, 1.00G -> 1G. */
function tickNumber(value: number): string {
  return axisNumber(value).replace(/\.0+([kMG]?)$/, "$1").replace(/(\.\d*?)0+([kMG])$/, "$1$2");
}

function DataTable({ caption, head, rows }: { caption: string; head: string[]; rows: { key: string; cells: string[] }[] }) {
  return (
    <details className="bm-numbers">
      <summary className="cursor-pointer font-mono text-[0.625rem] font-medium tracking-[0.14em] text-fg-subtle uppercase transition-colors duration-300 hover:text-fg">
        Numbers ({rows.length})
      </summary>
      <table className="mt-4 w-full border-collapse" data-testid="chart-numbers">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            {head.map((h) => (
              <th key={h} scope="col" className="py-2 pr-6 text-left font-mono text-[0.625rem] font-medium tracking-[0.12em] text-fg-subtle uppercase">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} className="nd-hair-t">
              {r.cells.map((c, i) => (
                <td key={`${String(i)}-${head[i] ?? ""}`} className="py-2 pr-6 font-mono text-xs text-fg-muted tabular-nums">
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

/** The origin (the laptop the runs come from) of a family: every config of a campaign shares it. */
function originOf(labels: LabelsOut, family: string): string | null {
  return labels.groups.find((g) => g.family === family)?.configs[0]?.origin_node_id ?? null;
}

function FamilyPlot({ chart, origin, nameOf }: { chart: FamilyChart; origin: string | null; nameOf: (id: string) => string }) {
  const param = chart.param ?? "";
  const fixed = Object.keys(chart.fixed).length > 0 ? `; ${formatParams(chart.fixed)}` : "";
  const title = `${chart.family}: median total time vs ${param} (condition ${chart.condition}${fixed})`;
  const points = chart.series.reduce((n, s) => n + s.points.length, 0);
  const many = new Set(chart.series.map((s) => s.strategy)).size > 1;
  const labelOf = (s: FamilyChart["series"][number]) => (many ? `${nameOf(s.nodeId)} · ${strategyText(s.strategy)}` : nameOf(s.nodeId));
  // the best series: the lowest medians on average (honey); the local baseline grey
  const mean = (s: FamilyChart["series"][number]) => s.points.reduce((a, p) => a + p.y, 0) / Math.max(1, s.points.length);
  const isBase = (s: FamilyChart["series"][number]) => s.strategy === "local" && s.nodeId === origin;
  const bestKey = [...chart.series].filter((s) => s.points.length > 0).sort((a, b) => mean(a) - mean(b))[0]?.key;
  let other = 0;
  const series: LineSeries[] = chart.series.map((s) => {
    const tone: Tone = s.key === bestKey ? "hive" : isBase(s) ? "base" : "other";
    const className = toneClass(tone, tone === "other" ? other++ : 0);
    return {
      key: s.key,
      label: labelOf(s),
      className,
      strong: tone === "hive",
      points: s.points.map((p) => ({ x: p.x, y: p.y, lo: p.ciLow, hi: p.ciHigh, n: p.n })),
    };
  });
  return (
    <figure className="flex flex-col gap-5" data-testid="family-chart">
      <figcaption className="text-[0.9375rem] font-medium text-fg">{title}</figcaption>
      <Legend items={series.map((s) => ({ key: s.key, label: s.label, className: s.className }))} />
      <div role="img" aria-label={`${title}: ${String(chart.series.length)} series, ${String(points)} points; the numbers are in the table below.`}>
        <LineChart
          series={series}
          logX={chart.logX}
          xLabel={param}
          formatX={tickNumber}
          formatY={(v) => formatMs(v)}
          label={title}
        />
      </div>
      <DataTable
        caption={title}
        head={[param, "series", "median", "CI low", "CI high", "n"]}
        rows={chart.series.flatMap((s) =>
          s.points.map((p) => ({
            key: `${s.key}|${p.configId}`,
            cells: [axisNumber(p.x), labelOf(s), formatMs(p.y), formatMs(p.ciLow), formatMs(p.ciHigh), String(p.n)],
          })),
        )}
      />
    </figure>
  );
}

function StrategyPlot({ chart, origin, nameOf }: { chart: StrategyChart; origin: string | null; nameOf: (id: string) => string }) {
  const title = `${chart.family} (${formatParams(chart.params)}, condition ${chart.condition}): strategy comparison - label ${chart.label}`;
  if (chart.bars.length === 0) {
    return <EmptyState title="Nothing measured in this group yet">Its configurations have no successful run records.</EmptyState>;
  }
  const bars: CompareBar[] = [...chart.bars]
    .sort((a, b) => a.y - b.y)
    .map((b) => ({
      key: b.configId,
      label: nameOf(b.nodeId),
      sub: b.strategy === "local" && b.nodeId === (origin ?? b.originNodeId) ? `${strategyText(b.strategy)} · baseline` : strategyText(b.strategy),
      value: b.y,
      ciLow: b.ciLow,
      ciHigh: b.ciHigh,
      tone: b.best ? "hive" : b.strategy === "local" && b.nodeId === b.originNodeId ? "base" : "other",
    }));
  return (
    <figure className="flex flex-col gap-6" data-testid="strategy-chart">
      <figcaption className="text-[0.9375rem] font-medium text-fg">{title}</figcaption>
      <div role="img" aria-label={`${title}: ${String(chart.bars.length)} configurations; the numbers are in the table below.`}>
        <CompareBars bars={bars} format={(v) => formatMs(v)} label="Median total time per configuration" />
      </div>
      {!chart.bars.some((b) => b.best) && (
        <Note caps={false}>{chart.label === "tie" ? "No bar is honey: the configurations' CIs overlap (a tie)." : `No clear best yet (label ${chart.label}).`}</Note>
      )}
      <DataTable
        caption={title}
        head={["configuration", "median", "CI low", "CI high", "n", "best"]}
        rows={chart.bars.map((b) => ({
          key: b.configId,
          cells: [optionText(b.strategy, nameOf(b.nodeId)), formatMs(b.y), formatMs(b.ciLow), formatMs(b.ciHigh), String(b.n), b.best ? "yes" : ""],
        }))}
      />
    </figure>
  );
}

function Charts({ labels }: { labels: LabelsOut }) {
  const nameOf = useNodeNames();
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
  const origin = originOf(labels, family);
  const drawable = charts.filter(plottable);
  return (
    <div className="flex flex-col gap-8">
      <div className="flex w-72 flex-col gap-1.5 px-2">
        <label htmlFor={familyId} className="nd-label">
          Family
        </label>
        <ListboxField
          id={familyId}
          value={family}
          options={families.map((f) => ({ value: f, label: f }))}
          onChange={(f) => {
            setPicked(f);
            setPickedGroup(null);
          }}
        />
      </div>
      <FrostCard title="Runtime vs parameter">
        <div className="px-10 pt-6 pb-9">
          {drawable.length === 0 ? (
            <EmptyState title="Not enough data for this chart">
              {charts.some((c) => c.param !== null)
                ? "No node has a successful median at two or more parameter values yet."
                : `The campaign measured ${family} at one parameter point only: there is no swept parameter to plot. The strategy comparison below compares its configurations.`}
            </EmptyState>
          ) : (
            <div className="flex flex-col gap-12">
              {drawable.map((c) => (
                <FamilyPlot key={c.key} chart={c} origin={origin} nameOf={nameOf} />
              ))}
            </div>
          )}
        </div>
      </FrostCard>
      <FrostCard
        title="Strategy comparison"
        meta={
          group === undefined ? undefined : (
            <div className="flex w-80 max-w-full flex-col gap-1.5">
              <label htmlFor={groupId} className="nd-label">
                Group
              </label>
              <ListboxField
                id={groupId}
                value={group.group_id}
                options={groups.map((g) => ({ value: g.group_id, label: `${formatParams(g.params)} · ${g.condition} · ${g.label}` }))}
                onChange={(g) => {
                  setPickedGroup(g);
                }}
              />
            </div>
          )
        }
      >
        <div className="px-10 pt-6 pb-9">
          {group === undefined ? <EmptyState title="No groups in this family" /> : <StrategyPlot chart={strategyChart(group)} origin={origin} nameOf={nameOf} />}
        </div>
      </FrostCard>
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
