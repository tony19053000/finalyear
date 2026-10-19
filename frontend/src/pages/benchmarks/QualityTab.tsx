/**
 * Quality (D220, G5c's report): the campaign summary with the criterion
 * verdicts, then every configuration - flagged ones (CV over the threshold,
 * fewer than 3 repetitions, missing fields) first. Every record bucket of
 * the summary is shown with its total, so a reader can check they add up.
 * Read through `qualityApi.ts`; a controller without the route says so
 * (never numbers it did not send).
 */
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { Card } from "../../components/Card";
import { EmptyState, ErrorState } from "../../components/States";
import { Badge } from "../../components/Status";
import { Table, type Column } from "../../components/Table";
import { formatMs, shortId } from "../../lib/format";
import { benchKeys } from "./benchKeys";
import { formatFraction } from "./campaignView";
import {
  flaggedFirst,
  getQuality,
  missingFields,
  recordBuckets,
  type QualityConfig,
  type QualityMetric,
  type QualityReport,
} from "./qualityApi";

function cv(metric: QualityMetric | null): string {
  return metric?.cv == null ? "—" : `${(metric.cv * 100).toFixed(1)} %`;
}

function ci(metric: QualityMetric | null, format: (v: number) => string): string {
  if (metric === null) return "—";
  return `${format(metric.ci_low)} – ${format(metric.ci_high)}`;
}

function ns(value: number): string {
  return `${value.toPrecision(3)} ns`;
}

function verdict(value: boolean | null, pending: boolean) {
  if (value === null) return <Badge tone="neutral">{pending ? "pending" : "not reported"}</Badge>;
  return <Badge tone={value ? "ok" : "failed"}>{value ? (pending ? "ok so far" : "met") : "not met"}</Badge>;
}

function countList(counts: Readonly<Record<string, number>>): string {
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
        <span>
          {c.family} · {c.strategy} · <span title={c.node_id}>{shortId(c.node_id)}</span>
        </span>
        <span className="text-fg-muted">{`condition ${c.condition}`}</span>
        <span className="text-fg-muted">{`${String(c.done)} / ${String(c.planned)} done · ${String(c.records_checked)} records`}</span>
      </span>
    ),
  },
  { id: "n", header: "n", align: "right", mono: true, cell: (c) => c.n },
  { id: "median", header: "Median total", align: "right", mono: true, cell: (c) => formatMs(c.total_ms?.median ?? null) },
  { id: "cv", header: "CV", align: "right", mono: true, cell: (c) => cv(c.total_ms) },
  { id: "ci", header: "CI (total)", mono: true, cell: (c) => ci(c.total_ms, formatMs) },
  {
    id: "iter",
    header: "Per iteration",
    mono: true,
    cell: (c) => (c.compute_per_iter_ns !== null ? `${ns(c.compute_per_iter_ns.median)} (CV ${cv(c.compute_per_iter_ns)})` : "—"),
  },
  {
    id: "outliers",
    header: "Outliers",
    align: "right",
    mono: true,
    cell: (c) => <span title={c.outlier_attempt_ids.join(", ") || undefined}>{c.outlier_attempt_ids.length}</span>,
  },
  {
    id: "flags",
    header: "Flags",
    cell: (c) => {
      const missing = Object.keys(missingFields(c)).length > 0;
      return (
        <span className="flex flex-wrap gap-1">
          {c.state === "unsupported" && <Badge tone="neutral">unsupported</Badge>}
          {c.cv_flag && <Badge tone="degraded">high CV</Badge>}
          {c.reps_flag && <Badge tone="failed">n &lt; 3</Badge>}
          {missing && <Badge tone="degraded">missing fields</Badge>}
          {!c.flagged && c.state !== "unsupported" && <span className="text-xs text-fg-muted">—</span>}
        </span>
      );
    },
  },
  {
    id: "missing",
    header: "Missing fields",
    className: "text-xs",
    cell: (c) => countList(missingFields(c)).replace(/^none$/, "—"),
  },
];

function Term({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <div>
      <dt className="text-2xs font-medium tracking-wide text-fg-subtle uppercase">{label}</dt>
      <dd data-testid={testId}>{children}</dd>
    </div>
  );
}

/** Every record bucket, their sum and `records`: the reader can check they add up. */
function Records({ report }: { report: QualityReport }) {
  const s = report.summary;
  const buckets = recordBuckets(s);
  const total = buckets.reduce((a, b) => a + b.count, 0);
  return (
    <Card title="Run records">
      <table className="text-sm" aria-label="Run records by bucket">
        <tbody>
          {buckets.map((b) => (
            <tr key={b.id}>
              <th scope="row" className="pr-6 text-left font-normal">
                {b.label}
                {b.by !== undefined && b.count > 0 && <span className="block text-xs text-fg-muted">{countList(b.by)}</span>}
              </th>
              <td className="text-right align-top font-mono" data-testid={`quality-bucket-${b.id}`}>
                {b.count}
              </td>
            </tr>
          ))}
          <tr className="border-t border-border">
            <th scope="row" className="pr-6 text-left">
              Records (fillers and warm-ups excluded)
            </th>
            <td className="text-right font-mono font-medium" data-testid="quality-records">
              {s.records}
            </td>
          </tr>
        </tbody>
      </table>
      {total !== s.records && (
        <p role="alert" className="mt-2 text-sm text-status-failed">
          {`The buckets add up to ${String(total)}, not ${String(s.records)}: this report is inconsistent.`}
        </p>
      )}
      <p className="mt-2 text-xs text-fg-muted" data-testid="quality-fillers">
        {`Fillers / warm-ups, never in any statistic: ${countList(s.bench_role_records)}`}
      </p>
    </Card>
  );
}

function Report({ report }: { report: QualityReport }) {
  const s = report.summary;
  return (
    <div className="flex flex-col gap-4">
      {s.pending && (
        <p role="status" className="text-sm text-fg-muted">
          The campaign is still running: these numbers and verdicts are provisional.
        </p>
      )}
      <Card title="Summary">
        <dl className="grid grid-cols-[repeat(auto-fill,minmax(14rem,1fr))] gap-x-6 gap-y-3 text-sm">
          <Term label="Campaign state" testId="quality-campaign-state">
            {s.campaign_state}
          </Term>
          <Term label="Criterion 1: infra failures < 2 %" testId="quality-crit1">
            {verdict(s.crit1_infra_ok, s.pending)}
          </Term>
          <Term label="Criterion 3: every config n ≥ 3 with CV" testId="quality-crit3">
            {verdict(s.crit3_reps_ok, s.pending)}
          </Term>
          <Term label="Infra failure rate (owner pre-emptions excluded)" testId="quality-infra-rate">
            {formatFraction(s.infra_failure_rate)}
          </Term>
          <Term label="Runs by state" testId="quality-runs-by-state">
            {countList(s.runs_by_state)}
          </Term>
          <Term label="CV threshold">{`${(s.cv_threshold * 100).toFixed(0)} %`}</Term>
        </dl>
      </Card>
      <Records report={report} />
      <Card title={`Configurations (${String(s.configs)}, ${String(s.flagged_configs)} flagged)`} flush>
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
  if (result.kind === "invalid") {
    return (
      <EmptyState title="Quality report could not be read">
        The controller sent a version 1 quality report that does not have the expected fields, so no number is
        shown. Rebuild the dashboard from the same release as the controller.
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
