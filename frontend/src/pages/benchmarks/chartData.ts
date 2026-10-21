/**
 * Chart data of the Benchmarks page's Charts tab (V0.9 G6, D221) - pure
 * mapping from the labels answer (`GET .../labels`, labels_version 1) to
 * plot points. **No statistics here**: every median and CI is the server's
 * (`meshforge.bench.stats` via `build_labels`); this module only picks,
 * groups and orders them. Error bars are the CI as offsets from the median,
 * the form Recharts' ErrorBar takes.
 *
 * 1. Per family ("runtime vs parameter"): one chart per (family, condition,
 *    the values of every OTHER swept parameter); x = the swept parameter (the
 *    first numeric parameter whose value differs between the family's
 *    groups, by name), one series per node (per node + strategy when the
 *    family ran under more than one strategy), y = median `total_ms`.
 * 2. Per group ("strategy comparison"): one bar per measured configuration
 *    (strategy on node), median `total_ms` with its CI.
 */
import type { ConfigLabel, GroupLabel, LabelsOut } from "./benchApi";

export interface PlotPoint {
  x: number;
  /** Median total_ms (server). */
  y: number;
  ciLow: number;
  ciHigh: number;
  /** [median - ci_low, ci_high - median] (both >= 0). */
  err: [number, number];
  n: number;
  configId: string;
  groupId: string;
}

export interface Series {
  key: string;
  label: string;
  nodeId: string;
  strategy: string;
  points: PlotPoint[];
}

export interface FamilyChart {
  /** Stable id: family | condition | fixed params. */
  key: string;
  family: string;
  condition: string;
  /** The swept parameter on x (null: nothing varies - no runtime-vs-param chart). */
  param: string | null;
  /** The other parameters that vary, fixed to these values in this chart. */
  fixed: Record<string, unknown>;
  series: Series[];
  /** Log x scale: positive values spanning >= 20x. */
  logX: boolean;
}

export interface StrategyBar {
  configId: string;
  label: string;
  strategy: string;
  nodeId: string;
  originNodeId: string;
  y: number;
  ciLow: number;
  ciHigh: number;
  err: [number, number];
  n: number;
  best: boolean;
}

export interface StrategyChart {
  groupId: string;
  family: string;
  condition: string;
  params: Record<string, unknown>;
  label: GroupLabel["label"];
  bars: StrategyBar[];
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** The server's median + CI of a measured config, or null (unsupported, no total_ms, non-finite). */
function cost(config: ConfigLabel): { y: number; ciLow: number; ciHigh: number; err: [number, number]; n: number } | null {
  const t = config.total_ms;
  if (config.state !== "measured" || t == null) return null;
  if (!finite(t.median) || !finite(t.ci_low) || !finite(t.ci_high)) return null;
  return {
    y: t.median,
    ciLow: t.ci_low,
    ciHigh: t.ci_high,
    err: [Math.max(0, t.median - t.ci_low), Math.max(0, t.ci_high - t.median)],
    n: t.n,
  };
}

/** A canonical text of a param value (object keys sorted), for grouping. */
function canon(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canon).join(",")}]`;
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canon(v)}`).join(",")}}`;
  }
  return value === undefined ? "null" : JSON.stringify(value);
}

/** Parameter names whose value differs between `groups`, sorted. */
export function sweptParams(groups: readonly GroupLabel[]): string[] {
  const names = new Set<string>();
  for (const g of groups) for (const k of Object.keys(g.params)) names.add(k);
  return [...names]
    .filter((name) => new Set(groups.map((g) => canon(g.params[name]))).size > 1)
    .sort();
}

/** The x parameter: the first swept one whose every value is a finite number. */
export function xParam(groups: readonly GroupLabel[]): string | null {
  for (const name of sweptParams(groups)) {
    if (groups.every((g) => finite(g.params[name]))) return name;
  }
  return null;
}

function byKey<T>(items: Iterable<T>, key: (item: T) => string): Map<string, T[]> {
  const out = new Map<string, T[]>();
  for (const item of items) {
    const k = key(item);
    const list = out.get(k);
    if (list === undefined) out.set(k, [item]);
    else list.push(item);
  }
  return out;
}

const cmp = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);

/** The families present in the labels, sorted. */
export function familiesOf(labels: LabelsOut): string[] {
  return [...new Set(labels.groups.map((g) => g.family))].sort(cmp);
}

/** Runtime-vs-parameter charts of one family (see the module doc). */
export function familyCharts(labels: LabelsOut, family: string): FamilyChart[] {
  const groups = labels.groups.filter((g) => g.family === family);
  const charts: FamilyChart[] = [];
  for (const [condition, inCondition] of [...byKey(groups, (g) => g.condition)].sort(([a], [b]) => cmp(a, b))) {
    const param = xParam(inCondition);
    const others = sweptParams(inCondition).filter((p) => p !== param);
    const facets = byKey(inCondition, (g) => canon(others.map((p) => g.params[p])));
    const strategies = new Set(inCondition.flatMap((g) => g.configs.map((c) => c.strategy)));
    for (const [facetKey, facetGroups] of [...facets].sort(([a], [b]) => cmp(a, b))) {
      const fixed: Record<string, unknown> = {};
      const first = facetGroups[0];
      if (first !== undefined) for (const p of others) fixed[p] = first.params[p];
      const series = new Map<string, Series>();
      for (const g of facetGroups) {
        const x = param === null ? 0 : (g.params[param] as number);
        for (const c of g.configs) {
          const measured = cost(c);
          if (measured === null) continue;
          const key = strategies.size > 1 ? `${c.node_id}|${c.strategy}` : c.node_id;
          let s = series.get(key);
          if (s === undefined) {
            s = {
              key,
              label: strategies.size > 1 ? `${c.node_id} (${c.strategy})` : c.node_id,
              nodeId: c.node_id,
              strategy: c.strategy,
              points: [],
            };
            series.set(key, s);
          }
          s.points.push({ x, ...measured, configId: c.config_id, groupId: g.group_id });
        }
      }
      const list = [...series.values()].sort((a, b) => cmp(a.key, b.key));
      for (const s of list) s.points.sort((a, b) => a.x - b.x || cmp(a.configId, b.configId));
      const xs = list.flatMap((s) => s.points.map((p) => p.x));
      const minX = Math.min(...xs);
      const maxX = Math.max(...xs);
      charts.push({
        key: `${family}|${condition}|${facetKey}`,
        family,
        condition,
        param,
        fixed,
        series: list,
        logX: param !== null && xs.length > 0 && minX > 0 && maxX / minX >= 20,
      });
    }
  }
  return charts;
}

/** A chart is worth drawing: a swept parameter and at least two points in some series. */
export function plottable(chart: FamilyChart): boolean {
  return chart.param !== null && chart.series.some((s) => s.points.length >= 2);
}

/** The strategy comparison of one group: a bar per measured config, in config_id order. */
export function strategyChart(group: GroupLabel): StrategyChart {
  const bars: StrategyBar[] = [];
  for (const c of [...group.configs].sort((a, b) => cmp(a.config_id, b.config_id))) {
    const measured = cost(c);
    if (measured === null) continue;
    bars.push({
      configId: c.config_id,
      label:
        c.strategy === "local"
          ? `local on ${c.node_id}`
          : c.strategy === "mode3"
            ? `mode3 (set led by ${c.node_id})`
            : `${c.strategy} → ${c.node_id}`,
      strategy: c.strategy,
      nodeId: c.node_id,
      originNodeId: c.origin_node_id,
      ...measured,
      best: group.best_config_id === c.config_id,
    });
  }
  return {
    groupId: group.group_id,
    family: group.family,
    condition: group.condition,
    params: group.params,
    label: group.label,
    bars,
  };
}

/** `{limit: 10000000, repeats: 4}` -> `limit=10000000, repeats=4` (sorted keys). */
export function formatParams(params: Record<string, unknown>): string {
  const keys = Object.keys(params).sort(cmp);
  if (keys.length === 0) return "defaults";
  return keys
    .map((k) => {
      const v = params[k];
      return `${k}=${typeof v === "string" ? v : canon(v)}`;
    })
    .join(", ");
}
