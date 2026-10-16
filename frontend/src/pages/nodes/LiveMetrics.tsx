/**
 * A node's live sparklines (D173): `GET /v1/nodes/{id}/metrics?window=300`
 * once, then one point per `node.metrics` event appended to the same cache
 * entry (events/applyEvent.ts; capped there). Nothing is polled; nothing is
 * drawn for a node that sends no samples.
 */
import { useNodeMetrics, type MetricPoint } from "../../api/fabric";
import { Card } from "../../components/Card";
import { Sparkline, type SparkPoint } from "../../components/Sparkline";
import { ErrorState } from "../../components/States";
import { formatBytes, formatPercent, formatTime, percentOf } from "../../lib/format";

function series(samples: readonly MetricPoint[], pick: (p: MetricPoint) => number | null): SparkPoint[] {
  const out: SparkPoint[] = [];
  for (const sample of samples) {
    const v = pick(sample);
    const t = Date.parse(sample.observed_at);
    if (v != null && Number.isFinite(v) && !Number.isNaN(t)) out.push({ t, v });
  }
  return out;
}

function Row({ label, now, points, testId }: { label: string; now: string; points: SparkPoint[]; testId: string }) {
  return (
    <div className="grid grid-cols-[9rem_1fr_auto] items-center gap-3">
      <dt className="text-sm text-fg-muted">{label}</dt>
      <dd>
        <Sparkline points={points} label={label} domain={[0, 100]} width={220} height={32} format={(v) => `${v.toFixed(0)}%`} />
      </dd>
      <dd className="w-40 text-right text-sm font-medium tabular-nums" data-testid={testId}>
        {now}
      </dd>
    </div>
  );
}

export function LiveMetrics({ nodeId }: { nodeId: string }) {
  const metrics = useNodeMetrics(nodeId);

  if (metrics.isPending) {
    return (
      <Card title="Live load">
        <p role="status" className="text-sm text-fg-muted">
          Loading samples…
        </p>
      </Card>
    );
  }
  if (metrics.isError) {
    return (
      <Card title="Live load">
        <ErrorState
          title="Could not load the live samples"
          error={metrics.error}
          onRetry={() => {
            void metrics.refetch();
          }}
        />
      </Card>
    );
  }
  const samples = metrics.data.samples;
  const last = samples.at(-1);
  const gpuIndexes = [...new Set(samples.flatMap((s) => s.gpus.map((g) => g.index)))].sort((a, b) => a - b);
  return (
    <Card
      title="Live load"
      data-testid="live-metrics"
      actions={
        last !== undefined ? (
          <span className="text-xs text-fg-muted">
            Latest sample{" "}
            <time dateTime={last.observed_at} data-testid="latest-sample">
              {formatTime(last.observed_at)}
            </time>
          </span>
        ) : undefined
      }
    >
      {last === undefined ? (
        <p className="text-sm text-fg-muted" data-testid="no-samples">
          No live samples in the last {metrics.data.window_s} s - the node is not reporting (offline), or the controller
          restarted and has not heard from it yet.
        </p>
      ) : (
        <>
        <dl className="flex flex-col gap-2">
          <Row label="CPU" now={formatPercent(last.cpu_percent)} points={series(samples, (s) => s.cpu_percent)} testId="live-cpu" />
          <Row
            label="Memory"
            now={`${formatBytes(last.ram_used_bytes)} / ${formatBytes(last.ram_total_bytes)}`}
            points={series(samples, (s) => percentOf(s.ram_used_bytes, s.ram_total_bytes))}
            testId="live-ram"
          />
          {gpuIndexes.map((index) => {
            const g = last.gpus.find((x) => x.index === index);
            return (
              <Row
                key={index}
                label={`GPU ${String(index)}`}
                now={
                  g === undefined
                    ? "—"
                    : `${formatPercent(g.util_percent)} · ${formatBytes(g.vram_used_bytes)} / ${formatBytes(g.vram_total_bytes)}`
                }
                points={series(samples, (s) => s.gpus.find((x) => x.index === index)?.util_percent ?? null)}
                testId={`live-gpu-${String(index)}`}
              />
            );
          })}
        </dl>
          <p className="pt-3 text-xs text-fg-muted">
            {samples.length} sample{samples.length === 1 ? "" : "s"} over the last {metrics.data.window_s} s (the controller
            keeps about 5 minutes in memory).
          </p>
        </>
      )}
    </Card>
  );
}
