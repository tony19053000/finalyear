/**
 * A node's live load (D173): `GET /v1/nodes/{id}/metrics?window=300` once,
 * then one point per `node.metrics` event appended to the same cache entry
 * (events/applyEvent.ts; capped there). Nothing is polled; nothing is drawn
 * for a node that sends no samples.
 *
 * One panel per resource - CPU, Memory, GPU - each with its current value as
 * a big light figure and its own line in the resource's colour.
 */
import type { ReactNode } from "react";

import { useNodeMetrics, type MetricPoint } from "../../api/fabric";
import type { SparkPoint } from "../../components/Sparkline";
import { ErrorState } from "../../components/States";
import { formatBytes, formatTime, percentOf } from "../../lib/format";
import { SERIES_CLASS } from "../overview/seriesStyle";
import { MetricChart, type ChartLine, type SeriesTone } from "./MetricChart";

function series(samples: readonly MetricPoint[], pick: (p: MetricPoint) => number | null | undefined): SparkPoint[] {
  const out: SparkPoint[] = [];
  for (const sample of samples) {
    const v = pick(sample);
    const t = Date.parse(sample.observed_at);
    if (v != null && Number.isFinite(v) && !Number.isNaN(t)) out.push({ t, v });
  }
  return out;
}

/** The big figure: "25" with a small "%" (text content stays "25%"). */
function Figure({ value, testId }: { value: number | null | undefined; testId: string }) {
  return (
    <p className="ov-figure text-[2.75rem] text-fg" data-testid={testId}>
      {value == null || !Number.isFinite(value) ? (
        <span className="text-fg-subtle">—</span>
      ) : (
        <>
          {Math.round(value)}
          <span className="ml-1 text-[1.5rem] font-light tracking-normal text-fg-muted">%</span>
        </>
      )}
    </p>
  );
}

function PeakAvg({ points }: { points: readonly SparkPoint[] }) {
  if (points.length === 0) return null;
  const values = points.map((p) => p.v);
  const peak = Math.max(...values);
  const avg = values.reduce((a, b) => a + b, 0) / values.length;
  return (
    <span className="flex gap-4 font-mono text-[0.625rem] tracking-[0.12em] text-fg-subtle uppercase tabular-nums">
      <span>
        peak <span className="text-fg-muted">{Math.round(peak)}</span>
      </span>
      <span>
        avg <span className="text-fg-muted">{Math.round(avg)}</span>
      </span>
    </span>
  );
}

interface PanelProps {
  tone: SeriesTone;
  label: string;
  value: number | null | undefined;
  valueTestId: string;
  sub: ReactNode;
  lines: ChartLine[];
  windowMs: number;
  empty: string;
}

function Panel({ tone, label, value, valueTestId, sub, lines, windowMs, empty }: PanelProps) {
  const s = SERIES_CLASS[tone];
  return (
    <section aria-label={label} className="ov-card flex min-w-0 flex-col px-6 pt-6 pb-4" data-testid={`live-panel-${tone}`}>
      <header className="flex items-center justify-between gap-4">
        <span className="flex items-center gap-2.5">
          <span aria-hidden="true" className={`nd-light nd-light-sm ${s.bg}`} />
          <span className={`nd-label ${s.text}`}>{label}</span>
        </span>
        <PeakAvg points={lines[0]?.points ?? []} />
      </header>
      <div className="mt-3">
        <Figure value={value} testId={valueTestId} />
        <div className="mt-2.5 min-h-5 text-[0.8125rem] text-fg-muted">{sub}</div>
      </div>
      <div className="mt-3">
        <MetricChart tone={tone} lines={lines} windowMs={windowMs} empty={empty} />
      </div>
    </section>
  );
}

export function LiveMetrics({ nodeId }: { nodeId: string }) {
  const metrics = useNodeMetrics(nodeId);

  if (metrics.isPending) {
    return (
      <p role="status" className="py-10 text-sm text-fg-muted">
        Loading samples…
      </p>
    );
  }
  if (metrics.isError) {
    return (
      <ErrorState
        title="Could not load the live samples"
        error={metrics.error}
        onRetry={() => {
          void metrics.refetch();
        }}
      />
    );
  }
  const samples = metrics.data.samples;
  const windowMs = metrics.data.window_s * 1000;
  const last = samples.at(-1);
  const gpuIndexes = [...new Set(samples.flatMap((s) => s.gpus.map((g) => g.index)))].sort((a, b) => a - b);

  const cpu = series(samples, (s) => s.cpu_percent);
  const ram = series(samples, (s) => percentOf(s.ram_used_bytes, s.ram_total_bytes));
  const gpuLines: ChartLine[] = gpuIndexes.map((index, k) => ({
    key: `gpu-${String(index)}`,
    label: `GPU ${String(index)}`,
    points: series(samples, (s) => s.gpus.find((x) => x.index === index)?.util_percent ?? null),
    dim: k > 0,
  }));
  const lastGpus = last?.gpus ?? [];
  const gpuNow =
    lastGpus.length === 0 ? null : lastGpus.reduce((a, g) => a + g.util_percent, 0) / lastGpus.length;

  return (
    <div data-testid="live-metrics">
      <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
        <span />
        {last !== undefined && (
          <span className="nd-label">
            Latest sample{" "}
            <time dateTime={last.observed_at} data-testid="latest-sample" className="ml-1.5 text-fg-muted">
              {formatTime(last.observed_at)}
            </time>
          </span>
        )}
      </div>
      {last === undefined && (
        <p className="mb-6 text-sm text-fg-muted" data-testid="no-samples">
          No live samples in the last {metrics.data.window_s} s - the node is not reporting (offline), or the controller
          restarted and has not heard from it yet.
        </p>
      )}
      <div className="nd-stagger grid grid-cols-1 gap-5 lg:grid-cols-3">
        <Panel
          tone="cpu"
          label="CPU"
          value={last?.cpu_percent}
          valueTestId="live-cpu"
          sub="Across all cores"
          lines={[{ key: "cpu", label: "CPU", points: cpu }]}
          windowMs={windowMs}
          empty="No samples"
        />
        <Panel
          tone="ram"
          label="Memory"
          value={last === undefined ? null : percentOf(last.ram_used_bytes, last.ram_total_bytes)}
          valueTestId="live-ram-percent"
          sub={
            last === undefined ? null : (
              <span className="font-mono text-xs tabular-nums" data-testid="live-ram">
                {`${formatBytes(last.ram_used_bytes)} / ${formatBytes(last.ram_total_bytes)}`}
              </span>
            )
          }
          lines={[{ key: "ram", label: "Memory", points: ram }]}
          windowMs={windowMs}
          empty="No samples"
        />
        <Panel
          tone="gpu"
          label={gpuIndexes.length > 1 ? `GPU · mean of ${String(gpuIndexes.length)}` : "GPU"}
          value={gpuNow}
          valueTestId="live-gpu"
          sub={
            gpuIndexes.length === 0 ? (
              <span className="text-fg-subtle">{last === undefined ? "—" : "No GPU reported"}</span>
            ) : (
              <span className="flex flex-col gap-1">
                {gpuIndexes.map((index) => {
                  const g = lastGpus.find((x) => x.index === index);
                  return (
                    <span key={index} className="flex gap-3 font-mono text-xs tabular-nums" data-testid={`live-gpu-${String(index)}`}>
                      {gpuIndexes.length > 1 && <span className="text-fg-subtle">GPU {index}</span>}
                      {gpuIndexes.length > 1 && <span>{g === undefined ? "—" : `${String(Math.round(g.util_percent))}%`}</span>}
                      <span>
                        {g === undefined ? "—" : `${formatBytes(g.vram_used_bytes)} / ${formatBytes(g.vram_total_bytes)}`}
                        <span className="ml-1.5 text-fg-subtle">VRAM</span>
                      </span>
                    </span>
                  );
                })}
              </span>
            )
          }
          lines={gpuLines.length === 0 ? [{ key: "gpu", label: "GPU", points: [] }] : gpuLines}
          windowMs={windowMs}
          empty={gpuIndexes.length === 0 && last !== undefined ? "No GPU on this node" : "No samples"}
        />
      </div>
    </div>
  );
}
