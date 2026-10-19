/**
 * Quality (D220, G5c's report): the campaign summary with the criterion
 * verdicts, then every configuration - flagged ones (CV over the threshold,
 * fewer than 3 repetitions) first. Read through `qualityApi.ts`; a controller
 * without the route says so (never numbers it did not send).
 */
import { useQuery } from "@tanstack/react-query";

import { Card } from "../../components/Card";
import { EmptyState, ErrorState } from "../../components/States";
import { Badge } from "../../components/Status";
import { Table, type Column } from "../../components/Table";
import { formatMs, shortId } from "../../lib/format";
import { benchKeys } from "./benchKeys";
import { formatFraction } from "./campaignView";
import { flaggedFirst, getQuality, type QualityConfig, type QualityMetric, type QualityReport } from "./qualityApi";

function cv(metric: QualityMetric | null): string {
  return metric?.cv == null ? "—" : `${(metric.cv * 100).toFixed(1)} %`;
}

function ci(metric: QualityMetric | null, format: (v: number) => string): string {
  if (metric?.ci_low == null || metric.ci_high == null) return "—";
  return `${format(metric.ci_low)} – ${format(metric.ci_high)}`;
}

function ns(value: number): string {
  return `${value.toPrecision(3)} ns`;
}

function verdict(value: boolean | null, pending: boolean) {
  if (value === null) return <Badge tone="neutral">{pending ? "pending" : "not reported"}</Badge>;
  return <Badge tone={value ? "ok" : "failed"}>{value ? (pending ? "ok so far" : "met") : "not met"}</Badge>;
}

function countList(counts: Record<string, number>): string {
  const entries = Object.entries(counts).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return entries.length === 0 ? "none" : entries.map(([k, v]) => `${k}: ${String(v)}`).join(", ");
}

const COLUMNS: Column<QualityConfig>[] = [
  {
    id: "config",
    header: "Group / config",
    mono: true,
    cell: (c) => (
      <span className="flex flex-col">
        <span title={c.group_id}>{shortId(c.group_id)}</span>
        <span title={c.config_id}>{shortId(c.config_id)}</span>
      </span>
    ),
  },
  {
    id: "what",
    header: "Runs",
    cell: (c) => (
      <span className="flex flex-col text-xs">
        <span>{[c.family, c.strategy, c.node_id].filter((x) => x !== null).join(" · ") || "—"}</span>
        <span className="text-fg-muted">{`${c.done ?? "—"} / ${c.planned ?? "—"} done`}</span>
      </span>
    ),
  },
  { id: "n", header: "n", align: "right", mono: true, cell: (c) => c.n ?? "—" },
  { id: "median", header: "Median total", align: "right", mono: true, cell: (c) => formatMs(c.total_ms?.median ?? null) },
  { id: "cv", header: "CV", align: "right", mono: true, cell: (c) => cv(c.total_ms) },
  { id: "ci", header: "CI (total)", mono: true, cell: (c) => ci(c.total_ms, formatMs) },
  {
    id: "iter",
    header: "Per iteration",
    mono: true,
    cell: (c) => (c.compute_per_iter_ns?.median != null ? `${ns(c.compute_per_iter_ns.median)} (CV ${cv(c.compute_per_iter_ns)})` : "—"),
  },
  {
    id: "outliers",
    header: "Outliers",
    align: "right",
    mono: true,
    cell: (c) => <span title={c.outlier_attempt_ids.join(", ") || undefined}>{c.outliers ?? "—"}</span>,
  },
  {
    id: "flags",
    header: "Flags",
    cell: (c) => (
      <span className="flex flex-wrap gap-1">
        {c.cv_flag && <Badge tone="degraded">high CV</Badge>}
        {c.reps_flag && <Badge tone="failed">n &lt; 3</Badge>}
        {!c.cv_flag && !c.reps_flag && <span className="text-xs text-fg-muted">—</span>}
      </span>
    ),
  },
  {
    id: "missing",
    header: "Missing fields",
    className: "text-xs",
    cell: (c) => countList(c.missing).replace(/^none$/, "—"),
  },
];

function Report({ report }: { report: QualityReport }) {
  const s = report.summary;
  const flagged = report.configs.filter((c) => c.cv_flag || c.reps_flag).length;
  return (
    <div className="flex flex-col gap-4">
      {s.pending && (
        <p role="status" className="text-sm text-fg-muted">
          The campaign is still running: these numbers and verdicts are provisional.
        </p>
      )}
      <Card title="Summary">
        <dl className="grid grid-cols-[repeat(auto-fill,minmax(14rem,1fr))] gap-x-6 gap-y-3 text-sm">
          <div>
            <dt className="text-2xs font-medium tracking-wide text-fg-subtle uppercase">Criterion 1: infra failures &lt; 2 %</dt>
            <dd data-testid="quality-crit1">{verdict(s.crit1_infra_ok, s.pending)}</dd>
          </div>
          <div>
            <dt className="text-2xs font-medium tracking-wide text-fg-subtle uppercase">Criterion 3: every config n ≥ 3 with CV</dt>
            <dd data-testid="quality-crit3">{verdict(s.crit3_reps_ok, s.pending)}</dd>
          </div>
          <div>
            <dt className="text-2xs font-medium tracking-wide text-fg-subtle uppercase">Infra failure rate (owner pre-emptions excluded)</dt>
            <dd data-testid="quality-infra-rate">{formatFraction(s.infra_failure_rate)}</dd>
          </div>
          <div>
            <dt className="text-2xs font-medium tracking-wide text-fg-subtle uppercase">Stopped by owner</dt>
            <dd>{s.owner_preempted ?? "—"}</dd>
          </div>
          <div>
            <dt className="text-2xs font-medium tracking-wide text-fg-subtle uppercase">Runs by state</dt>
            <dd>{countList(s.runs_by_state)}</dd>
          </div>
          <div>
            <dt className="text-2xs font-medium tracking-wide text-fg-subtle uppercase">Condition failures</dt>
            <dd>{countList(s.condition_failures)}</dd>
          </div>
          <div>
            <dt className="text-2xs font-medium tracking-wide text-fg-subtle uppercase">Workload failures</dt>
            <dd>{countList(s.workload_failures)}</dd>
          </div>
          <div>
            <dt className="text-2xs font-medium tracking-wide text-fg-subtle uppercase">Fillers / warm-ups (not in any statistic)</dt>
            <dd>{s.fillers ?? "—"}</dd>
          </div>
          <div>
            <dt className="text-2xs font-medium tracking-wide text-fg-subtle uppercase">CV threshold</dt>
            <dd>{report.cv_threshold === null ? "—" : `${(report.cv_threshold * 100).toFixed(0)} %`}</dd>
          </div>
        </dl>
      </Card>
      <Card title={`Configurations (${String(report.configs.length)}, ${String(flagged)} flagged)`} flush>
        <Table
          caption="Data quality per configuration, flagged first"
          columns={COLUMNS}
          rows={flaggedFirst(report.configs)}
          rowKey={(c) => `${c.group_id}|${c.config_id}`}
          empty="No configurations in the report."
        />
      </Card>
    </div>
  );
}

export function QualityTab({ campaignId }: { campaignId: string }) {
  const quality = useQuery({
    queryKey: benchKeys.quality(campaignId),
    queryFn: ({ signal }) => getQuality(campaignId, signal),
  });
  if (quality.isPending) {
    return (
      <p role="status" className="text-sm text-fg-muted">
        Loading the quality report…
      </p>
    );
  }
  if (quality.isError) {
    return <ErrorState title="Could not load the quality report" error={quality.error} onRetry={() => void quality.refetch()} />;
  }
  const result = quality.data;
  if (result.kind === "unavailable") {
    return (
      <EmptyState title="Quality report not available on this controller yet">
        This controller has no data quality report (<code className="font-mono">GET …/quality</code>, V0.9 roadmap
        0.9.8). Update the controller; the other tabs work without it.
      </EmptyState>
    );
  }
  if (result.kind === "unsupported") {
    return (
      <EmptyState title="Quality report format not supported">
        The controller sent quality report version {result.version === undefined ? "?" : JSON.stringify(result.version)}; this dashboard reads version 1.
        Rebuild the dashboard from the same release as the controller.
      </EmptyState>
    );
  }
  return <Report report={result.report} />;
}
